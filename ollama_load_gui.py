#!/usr/bin/env python3
"""Compact Tkinter dashboard for ollama-load-service."""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import threading
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import ttk
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw, ImageTk


BG = "#16191d"
FG = "#e6e6e6"
MUTED = "#9aa1a9"
GREEN = "#55d187"
BLUE = "#58a6ff"
YELLOW = "#f2c94c"
GRID = "#343a40"
RED = "#ff6b6b"
FAN_COLORS = ("#d78bff", "#ff9966", "#6ed6d0", "#ff7aa2", "#b5d96c", "#9db7ff")


def read_fan_config(path: str = "~/.ollama-load.conf") -> dict[str, str]:
    """Read displayed fan IDs and labels from simple fanN = label entries."""
    configured = {}
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as config_file:
            for line in config_file:
                line = line.split("#", 1)[0].strip()
                match = re.match(r"^(fan\d+)\s*=\s*(.+?)\s*$", line, re.IGNORECASE)
                if match and match.group(2):
                    configured[match.group(1).lower()] = match.group(2)
    except OSError:
        pass
    return configured


def human_bytes(value: int | float) -> str:
    value = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit in ("GiB", "TiB") else f"{value:.0f} {unit}"
        value /= 1024
    return "?"


def short_context(value: int) -> str:
    if value >= 1000:
        return f"{value / 1000:g}k"
    return str(value)


def temperature_color(value: float | None) -> str:
    """Return a cool/good/warm/hot color for a Celsius temperature."""
    if value is None:
        return MUTED
    if value < 45:
        return BLUE
    if value < 70:
        return GREEN
    if value < 85:
        return YELLOW
    return RED


def history_in_window(history: list[dict], minutes: float) -> list[dict]:
    """Keep only samples from the most recent graph time window."""
    timestamps = []
    for item in history:
        try:
            timestamps.append(datetime.fromisoformat(item["timestamp"]))
        except (KeyError, TypeError, ValueError):
            timestamps.append(None)
    valid = [timestamp for timestamp in timestamps if timestamp is not None]
    if not valid:
        return history
    cutoff = max(valid) - timedelta(minutes=minutes)
    return [item for item, timestamp in zip(history, timestamps) if timestamp is not None and timestamp >= cutoff]


class Dashboard(tk.Tk):
    def __init__(self, url: str, token: str | None, poll_seconds: float, graph_minutes: float):
        super().__init__(className="OllamaLoad")
        self.url = url.rstrip("/") + "/api/status"
        self.token = token
        self.poll_ms = max(500, int(poll_seconds * 1000))
        self.graph_minutes = graph_minutes
        self.responses: queue.Queue = queue.Queue()
        self.fetch_running = False

        dashboard_title = f"Ollama Load: {urlparse(url).hostname or url}"
        self.title(dashboard_title)
        self.geometry("430x470")
        self.minsize(360, 390)
        self.bind("<Escape>", lambda _event: self.destroy())
        self.bind("q", lambda _event: self.destroy())
        self.configure(bg=BG)
        self.wm_attributes("-alpha", 0.5)
        self.option_add("*Font", "TkDefaultFont 9")
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)

        ttk.Label(self, text=dashboard_title, foreground=FG, font=("TkDefaultFont", 13, "bold")).pack(
            anchor="w", padx=9, pady=(7, 0)
        )
        top = ttk.Frame(self)
        top.pack(fill="x", padx=9, pady=(7, 2))
        self.load_label = ttk.Label(top, text="CPU --%", foreground=GREEN, font=("TkDefaultFont", 12, "bold"))
        self.load_label.pack(side="left")
        self.gpu_load_label = ttk.Label(top, text="GPU --%", foreground=GREEN, font=("TkDefaultFont", 12, "bold"))
        self.gpu_load_label.pack(side="left", padx=(16, 0))
        self.memory_label = ttk.Label(top, text="VRAM --", foreground=BLUE, font=("TkDefaultFont", 12, "bold"))
        self.memory_label.pack(side="left", padx=(16, 0))
        details = ttk.Frame(top)
        details.pack(side="right")
        self.cpu_temperature_label = ttk.Label(details, text="CPU --°C", foreground=MUTED)
        self.cpu_temperature_label.pack(side="left")
        self.gpu_temperature_label = ttk.Label(details, text="GPU --°C", foreground=MUTED)
        self.gpu_temperature_label.pack(side="left", padx=(8, 0))
        self.memory_detail_label = ttk.Label(details, text="")
        self.memory_detail_label.pack(side="left", padx=(8, 0))

        self.canvas = tk.Canvas(self, height=92, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="x", padx=8, pady=(0, 2))
        self.canvas.bind("<Configure>", lambda _event: self.redraw())

        self.temperature_canvas = tk.Canvas(self, height=72, bg=BG, highlightthickness=0)
        self.temperature_canvas.pack(fill="x", padx=8, pady=(0, 2))
        self.temperature_canvas.bind("<Configure>", lambda _event: self.redraw())

        self.fan_header = ttk.Frame(self)
        self.fan_header.pack(fill="x", padx=9)
        self.fan_label = ttk.Label(self.fan_header, text="Fans", foreground=MUTED)
        self.fan_label.pack(side="left")
        self.fan_speed_label = ttk.Label(self.fan_header, text="-- RPM", foreground=MUTED)
        self.fan_speed_label.pack(side="right")
        self.fan_speed_labels: list[ttk.Label] = []
        self.fan_canvas = tk.Canvas(self, height=72, bg=BG, highlightthickness=0)
        self.fan_canvas.pack(fill="x", padx=8, pady=(0, 2))
        self.fan_canvas.bind("<Configure>", lambda _event: self.redraw())

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=8)
        self.models_frame = ttk.Frame(self)
        self.models_frame.pack(fill="both", expand=True, padx=9, pady=4)
        self.models_label = ttk.Label(self.models_frame, text="Connecting…", justify="left", anchor="nw")
        self.models_label.pack(fill="both", expand=True)

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=9, pady=(0, 5))
        self.status_label = ttk.Label(bottom, text="", foreground=MUTED)
        self.status_label.pack(side="left")
        ttk.Label(bottom, text="CPU", foreground=YELLOW).pack(side="right")
        ttk.Label(bottom, text="  GPU", foreground="#8be28b").pack(side="right")
        ttk.Label(bottom, text="  VRAM", foreground=BLUE).pack(side="right")

        self.history: list[dict] = []
        self.fan_config = read_fan_config()
        self.fan_fields: tuple[tuple[str, str], ...] = ()
        self.fan_labels: dict[str, str] = {}
        self.fan_name_labels: list[ttk.Label] = []
        self.after(20, self.poll)

    def poll(self) -> None:
        if not self.fetch_running:
            self.fetch_running = True
            threading.Thread(target=self.fetch, daemon=True).start()
        self.consume_response()
        self.after(self.poll_ms, self.poll)

    def fetch(self) -> None:
        try:
            headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
            with urlopen(Request(self.url, headers=headers), timeout=3) as response:
                self.responses.put((json.load(response), None))
        except Exception as exc:  # network errors should be shown, not crash the widget
            self.responses.put((None, str(exc)))
        finally:
            self.fetch_running = False

    def consume_response(self) -> None:
        try:
            payload, error = self.responses.get_nowait()
        except queue.Empty:
            return
        if error:
            self.status_label.configure(text=error, foreground=RED)
            return

        summary = payload.get("gpu_summary", {})
        load = summary.get("cpu_percent")
        gpu_load = summary.get("gpu_percent")
        memory = summary.get("memory_percent")
        self.load_label.configure(text=f"CPU {load:.0f}%" if load is not None else "CPU --%")
        self.gpu_load_label.configure(text=f"GPU {gpu_load:.0f}%" if gpu_load is not None else "GPU --%")
        self.memory_label.configure(text=f"VRAM {memory:.0f}%" if memory is not None else "VRAM --%")
        used = (summary.get("memory_used_mib") or 0) * 1024 * 1024
        total = (summary.get("memory_total_mib") or 0) * 1024 * 1024
        cpu_temp = summary.get("cpu_temperature_c")
        gpu_temp = summary.get("gpu_temperature_c")
        self.cpu_temperature_label.configure(
            text=f"CPU {cpu_temp:.0f}°C" if cpu_temp is not None else "CPU --°C",
            foreground=temperature_color(cpu_temp),
        )
        self.gpu_temperature_label.configure(
            text=f"GPU {gpu_temp:.0f}°C" if gpu_temp is not None else "GPU --°C",
            foreground=temperature_color(gpu_temp),
        )
        self.memory_detail_label.configure(text=f"{human_bytes(used)} / {human_bytes(total)}")

        fans = {fan.get("id"): fan for fan in payload.get("fans", []) if fan.get("id")}
        fan_ids = list(self.fan_config) if self.fan_config else list(fans)
        self.fan_labels = {
            fan_id: self.fan_config.get(fan_id) or fans.get(fan_id, {}).get("label") or fan_id
            for fan_id in fan_ids
        }
        self.fan_fields = tuple(
            (fan_id, FAN_COLORS[index % len(FAN_COLORS)]) for index, fan_id in enumerate(fan_ids)
        )
        self.fan_speed_label.configure(text="-- RPM" if not fan_ids else "")
        for label in self.fan_speed_labels:
            label.destroy()
        self.fan_speed_labels = []
        for fan_id, color in reversed(self.fan_fields):
            rpm = fans.get(fan_id, {}).get("speed_rpm")
            text = f"{rpm:.0f} RPM" if isinstance(rpm, (int, float)) else "-- RPM"
            label = ttk.Label(self.fan_header, text=text, foreground=color)
            label.pack(side="right")
            self.fan_speed_labels.append(label)
        for label in self.fan_name_labels:
            label.destroy()
        self.fan_name_labels = [
            ttk.Label(self.fan_header, text=self.fan_labels[fan_id], foreground=color)
            for fan_id, color in self.fan_fields
        ]
        for label in self.fan_name_labels:
            label.pack(side="left", padx=(9, 0))

        lines = []
        for model in payload.get("models", []):
            gpu = model.get("gpu_percent")
            processor = f"{gpu}% GPU" if gpu is not None else "GPU ?"
            ctx = short_context(model.get("context_length") or 0)
            vram = human_bytes(model.get("size_vram_bytes") or 0)
            lines.append(f"{model['name']}\n  {processor}   ctx {ctx}   VRAM {vram}")
        self.models_label.configure(text="\n".join(lines) if lines else "No model loaded")

        errors = payload.get("errors", [])
        stamp = payload.get("timestamp", "")
        try:
            stamp = datetime.fromisoformat(stamp).astimezone().strftime("%H:%M:%S")
        except ValueError:
            pass
        self.status_label.configure(text="; ".join(errors) if errors else f"Updated {stamp}", foreground=RED if errors else MUTED)
        self.history = history_in_window(payload.get("history", []), self.graph_minutes)
        self.redraw()

    def redraw(self) -> None:
        self.draw_graph(
            self.canvas,
            (("memory_percent", BLUE), ("cpu_percent", YELLOW), ("gpu_percent", "#8be28b")),
            "graph_photo",
        )
        self.draw_graph(
            self.temperature_canvas,
            (("cpu_temperature_c", YELLOW), ("gpu_temperature_c", "#8be28b")),
            "temperature_graph_photo",
        )
        self.draw_graph(self.fan_canvas, self.fan_fields, "fan_graph_photo", fan_graph=True)

    def draw_graph(
        self,
        canvas: tk.Canvas,
        fields: tuple[tuple[str, str], ...],
        photo_attr: str,
        fan_graph: bool = False,
    ) -> None:
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        scale = 3
        render_width = width * scale
        render_height = height * scale
        image = Image.new("RGBA", (render_width, render_height), BG)
        image_draw = ImageDraw.Draw(image)
        value_max = 100
        if fan_graph:
            values = [
                value
                for item in self.history
                for field, _color in fields
                for value in (item.get("fan_speeds", {}).get(field),)
                if isinstance(value, (int, float))
            ]
            value_max = max(1000, ((max(values) + 499) // 500) * 500) if values else 1000
        for value in (25, 50, 75):
            y = render_height - value * render_height / 100
            image_draw.line((0, y, render_width, y), fill=GRID, width=scale)
        if len(self.history) < 2:
            image = image.resize((width, height), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(image)
            setattr(self, photo_attr, photo)
            canvas.create_image(0, 0, image=photo, anchor="nw")
            return

        def points_for(field: str) -> list[list[tuple[float, float]]]:
            segments = []
            points = []
            count = len(self.history)
            for index, item in enumerate(self.history):
                value = item.get("fan_speeds", {}).get(field) if fan_graph else item.get(field)
                if value is None:
                    if len(points) >= 2:
                        segments.append(points)
                    points = []
                    continue
                x = index * (width - 1) / max(1, count - 1)
                y = height - max(0, min(value_max, value)) * (height - 1) / value_max
                points.append((x, y))
            if len(points) >= 2:
                segments.append(points)
            return segments

        series = [(points_for(field), color) for field, color in fields]

        # Composite fills onto the accumulated image so overlapping series blend.
        for segments, color in series:
            rgb = tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))
            layer = Image.new("RGBA", (render_width, render_height), (0, 0, 0, 0))
            layer_draw = ImageDraw.Draw(layer)
            for points in segments:
                points = [(x * scale, y * scale) for x, y in points]
                polygon = points + [(x, render_height) for x, _y in reversed(points)]
                layer_draw.polygon(polygon, fill=(*rgb, 52))
            image = Image.alpha_composite(image, layer)

        image_draw = ImageDraw.Draw(image)
        for segments, color in series:
            for points in segments:
                points = [(x * scale, y * scale) for x, y in points]
                image_draw.line(points, fill=color, width=2 * scale, joint="curve")

        image = image.resize((width, height), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(image)
        setattr(self, photo_attr, photo)
        canvas.create_image(0, 0, image=photo, anchor="nw")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("OLLAMA_LOAD_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--token", default=os.environ.get("OLLAMA_LOAD_TOKEN"))
    parser.add_argument("--poll", type=float, default=2.0)
    parser.add_argument("--graph-minutes", type=float, default=5.0,
                        help="history displayed in the graph (default: 5)")
    args = parser.parse_args()
    Dashboard(args.url, args.token, args.poll, max(0.1, args.graph_minutes)).mainloop()


if __name__ == "__main__":
    main()
