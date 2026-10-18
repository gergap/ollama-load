#!/usr/bin/env python3
"""Small local operating-system load dashboard."""

from __future__ import annotations

import argparse
import glob
import os
import re
import tkinter as tk
from collections import deque
from tkinter import ttk

from PIL import Image, ImageDraw, ImageTk


BG = "#16191d"
FG = "#e6e6e6"
MUTED = "#9aa1a9"
GREEN = "#55d187"
BLUE = "#58a6ff"
YELLOW = "#f2c94c"
GRID = "#343a40"
RED = "#ff6b6b"
PURPLE = "#d78bff"
CORE_COLORS = ("#f2c94c", "#58a6ff", "#55d187", "#ff6b6b", "#d78bff", "#6ed6d0", "#ff9966", "#b5d96c")
TEMP_LINE = BLUE
RAM_COLORS = (YELLOW, PURPLE, RED, BLUE, GREEN)


def read_cpu_times() -> tuple[int, int] | None:
    try:
        with open("/proc/stat", encoding="ascii") as proc_stat:
            fields = proc_stat.readline().split()
        if fields[0] != "cpu" or len(fields) < 5:
            return None
        values = [int(value) for value in fields[1:]]
        return sum(values), values[3] + (values[4] if len(values) > 4 else 0)
    except (IndexError, OSError, ValueError):
        return None


def read_cpu_info() -> tuple[str | None, int | None]:
    model = None
    core_count = 0
    try:
        with open("/proc/cpuinfo", encoding="ascii") as cpuinfo:
            for line in cpuinfo:
                key, _, value = line.partition(":")
                key = key.strip()
                if key == "model name" and model is None:
                    model = value.strip()
                elif key == "processor":
                    core_count += 1
    except (OSError, UnicodeError):
        pass
    return model, core_count or os.cpu_count()


def read_cpu_core_times() -> dict[str, tuple[int, int]]:
    """Return total and idle jiffies for each logical CPU."""
    cores = {}
    try:
        with open("/proc/stat", encoding="ascii") as proc_stat:
            for line in proc_stat:
                fields = line.split()
                if len(fields) < 5 or not re.fullmatch(r"cpu\d+", fields[0]):
                    continue
                values = [int(value) for value in fields[1:]]
                cores[fields[0]] = (sum(values), values[3] + (values[4] if len(values) > 4 else 0))
    except (OSError, ValueError):
        return {}
    return cores


def cpu_load(previous: tuple[int, int] | None) -> tuple[float | None, tuple[int, int] | None]:
    current = read_cpu_times()
    if not previous or not current:
        return None, current
    total_delta = current[0] - previous[0]
    idle_delta = current[1] - previous[1]
    if total_delta <= 0:
        return None, current
    return max(0.0, min(100.0, 100 * (total_delta - idle_delta) / total_delta)), current


def cpu_core_loads(previous: dict[str, tuple[int, int]] | None) -> tuple[dict[str, float], dict[str, tuple[int, int]]]:
    current = read_cpu_core_times()
    if not previous:
        return {}, current
    loads = {}
    for name, times in current.items():
        old = previous.get(name)
        if not old:
            continue
        total_delta = times[0] - old[0]
        idle_delta = times[1] - old[1]
        if total_delta > 0:
            loads[name] = max(0.0, min(100.0, 100 * (total_delta - idle_delta) / total_delta))
    return loads, current


def read_process_times() -> dict[int, tuple[int, int, str]]:
    """Return each process's CPU jiffies, start time, and command name."""
    processes = {}
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", encoding="ascii") as stat_file:
                    stat = stat_file.read()
                end_comm = stat.rfind(")")
                fields = stat[end_comm + 2 :].split()
                if len(fields) < 20:
                    continue
                pid = int(entry)
                processes[pid] = (int(fields[11]) + int(fields[12]), int(fields[19]), stat[stat.index("(") + 1 : end_comm])
            except (OSError, ValueError):
                continue
    except OSError:
        return {}
    return processes


def process_cpu_loads(
    previous: dict[int, tuple[int, int, str]],
    previous_cpu: tuple[int, int] | None,
    current_cpu: tuple[int, int] | None,
) -> tuple[list[tuple[float, int, str]], dict[int, tuple[int, int, str]]]:
    current = read_process_times()
    if not previous_cpu or not current_cpu:
        return [], current
    total_delta = current_cpu[0] - previous_cpu[0]
    if total_delta <= 0:
        return [], current

    loads = []
    for pid, (cpu_time, start_time, name) in current.items():
        old = previous.get(pid)
        if not old or old[1] != start_time:
            continue
        process_delta = cpu_time - old[0]
        if process_delta > 0:
            loads.append((100 * process_delta / total_delta, pid, name))
    loads.sort(reverse=True)
    return loads, current


def read_memory() -> tuple[float | None, int | None, int | None, dict[str, float]]:
    values = {}
    try:
        with open("/proc/meminfo", encoding="ascii") as meminfo:
            for line in meminfo:
                match = re.match(r"^(MemTotal|MemAvailable|MemFree|Buffers|Cached|Slab):\s+(\d+)", line)
                if match:
                    values[match.group(1)] = int(match.group(2)) * 1024
    except OSError:
        return None, None, None, {}
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if not total or available is None or "MemFree" not in values:
        return None, None, None, {}
    used = total - available
    parts = {
        "used": max(0, total - values["MemFree"] - values.get("Buffers", 0) - values.get("Cached", 0) - values.get("Slab", 0)),
        "buff": values.get("Buffers", 0),
        "slab": values.get("Slab", 0),
        "cache": values.get("Cached", 0),
        "free": values["MemFree"],
    }
    return 100 * used / total, used, total, {name: 100 * value / total for name, value in parts.items()}


def read_temperatures() -> float | None:
    candidates = []
    for input_path in glob.glob("/sys/class/hwmon/hwmon*/temp*_input"):
        sensor_dir = os.path.dirname(input_path)
        sensor_file = os.path.basename(input_path)
        try:
            with open(os.path.join(sensor_dir, "name"), encoding="ascii") as name_file:
                sensor_name = name_file.read().strip().lower()
            label_path = os.path.join(sensor_dir, sensor_file.replace("_input", "_label"))
            try:
                with open(label_path, encoding="ascii") as label_file:
                    label = label_file.read().strip().lower()
            except OSError:
                label = ""
            if not any(word in sensor_name or word in label for word in ("cpu", "core", "package", "k10", "zen")):
                continue
            with open(input_path, encoding="ascii") as temp_file:
                value = float(temp_file.read().strip()) / 1000
            if -100 <= value <= 200:
                candidates.append(value)
        except (OSError, ValueError):
            continue

    fallback = []
    for type_path in glob.glob("/sys/class/thermal/thermal_zone*/type"):
        zone = os.path.dirname(type_path)
        try:
            with open(type_path, encoding="ascii") as type_file:
                zone_type = type_file.read().strip().lower()
            with open(os.path.join(zone, "temp"), encoding="ascii") as temp_file:
                value = float(temp_file.read().strip()) / 1000
            if -100 <= value <= 200:
                (candidates if any(word in zone_type for word in ("cpu", "x86", "package", "core")) else fallback).append(value)
        except (OSError, ValueError):
            continue
    values = candidates or fallback
    return max(values) if values else None


def read_disk_bytes() -> int:
    total = 0
    try:
        with open("/proc/diskstats", encoding="ascii") as diskstats:
            for line in diskstats:
                fields = line.split()
                if len(fields) < 10 or fields[2].startswith(("loop", "ram", "fd")):
                    continue
                # Linux reports sectors read/written in fields 5 and 9; sectors are 512 bytes.
                total += (int(fields[5]) + int(fields[9])) * 512
    except (OSError, ValueError):
        return 0
    return total


def read_network_bytes() -> tuple[int, int]:
    received = sent = 0
    try:
        with open("/proc/net/dev", encoding="ascii") as netdev:
            for line in netdev:
                if ":" not in line:
                    continue
                interface, values = line.split(":", 1)
                if interface.strip() == "lo":
                    continue
                fields = values.split()
                if len(fields) >= 9:
                    received += int(fields[0])
                    sent += int(fields[8])
    except (OSError, ValueError):
        return 0, 0
    return received, sent


def human_bytes(value: int | float | None) -> str:
    if value is None:
        return "--"
    value = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit in ("GiB", "TiB") else f"{value:.0f} {unit}"
        value /= 1024
    return "--"


class Dashboard(tk.Tk):
    def __init__(self, poll_seconds: float, graph_minutes: float):
        super().__init__(className="OSLoad")
        self.title("OS load")
        self.geometry("430x720")
        self.minsize(360, 600)
        self.configure(bg=BG)
        self.bind("<Escape>", lambda _event: self.destroy())
        self.bind("q", lambda _event: self.destroy())
        self.wm_attributes("-alpha", 0.5)
        self.poll_ms = max(500, int(poll_seconds * 1000))
        self.graph_minutes = graph_minutes
        self.previous_cpu = None
        self.previous_cpu_cores = None
        self.previous_processes = {}
        self.previous_disk = None
        self.previous_network = None
        self.history: deque[dict] = deque(maxlen=max(2, int(graph_minutes * 60 / poll_seconds) + 2))
        self.last_sample_time = None

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        top = ttk.Frame(self)
        top.pack(fill="x", padx=9, pady=(7, 2))
        self.cpu_label = ttk.Label(top, text="CPU --%", foreground=YELLOW, font=("TkDefaultFont", 12, "bold"))
        self.cpu_label.pack(side="left")
        self.memory_label = ttk.Label(top, text="RAM --", foreground=BLUE, font=("TkDefaultFont", 12, "bold"))
        self.memory_label.pack(side="left", padx=(16, 0))
        self.temp_label = ttk.Label(top, text="CPU -- C", foreground=TEMP_LINE)
        self.temp_label.pack(side="right")
        self.cpu_info_label = ttk.Label(self, text="CPU -- | -- cores", foreground=MUTED)
        self.cpu_info_label.pack(anchor="w", padx=9)
        self.cpu_model, self.cpu_core_count = read_cpu_info()
        cpu_name = self.cpu_model or "Unknown CPU"
        core_text = f"{self.cpu_core_count} cores" if self.cpu_core_count else "-- cores"
        self.cpu_info_label.configure(text=f"{cpu_name} | {core_text}")

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=8, pady=(3, 0))
        ttk.Label(self, text="CPU load (all cores)", foreground=YELLOW).pack(anchor="w", padx=9)
        self.cpu_graph = tk.Canvas(self, height=120, bg=BG, highlightthickness=0)
        self.cpu_graph.pack(fill="x", padx=8, pady=(0, 2))
        self.bind_graph_events(self.cpu_graph)
        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=8, pady=(3, 0))
        ram_heading = ttk.Frame(self)
        ram_heading.pack(fill="x", padx=9)
        ttk.Label(ram_heading, text="RAM usage", foreground=FG).pack(side="left")
        self.memory_part_labels = {}
        for name, color in zip(("used", "buff", "slab", "cache", "free"), RAM_COLORS):
            label = ttk.Label(ram_heading, text=f"{name} --", foreground=color)
            label.pack(side="left", padx=(9, 0))
            self.memory_part_labels[name] = label
        self.memory_graph = tk.Canvas(self, height=72, bg=BG, highlightthickness=0)
        self.memory_graph.pack(fill="x", padx=8, pady=(0, 2))
        self.bind_graph_events(self.memory_graph)
        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=8, pady=(3, 0))
        io_heading = ttk.Frame(self)
        io_heading.pack(fill="x", padx=9)
        ttk.Label(io_heading, text="Disk and network I/O", foreground=FG).pack(side="left")
        self.disk_label = ttk.Label(io_heading, text="Disk --/s", foreground=PURPLE)
        self.disk_label.pack(side="left", padx=(9, 0))
        self.rx_label = ttk.Label(io_heading, text="Net --/s in", foreground=GREEN)
        self.rx_label.pack(side="left", padx=(9, 0))
        self.tx_label = ttk.Label(io_heading, text="--/s out", foreground=RED)
        self.tx_label.pack(side="left", padx=(9, 0))
        self.io_graph = tk.Canvas(self, height=72, bg=BG, highlightthickness=0)
        self.io_graph.pack(fill="x", padx=8, pady=(0, 2))
        self.bind_graph_events(self.io_graph)
        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=8, pady=(3, 0))
        ttk.Label(self, text="CPU temperature", foreground=TEMP_LINE).pack(anchor="w", padx=9)
        self.temperature_canvas = tk.Canvas(self, height=62, bg=BG, highlightthickness=0)
        self.temperature_canvas.pack(fill="x", padx=8, pady=(0, 2))
        self.bind_graph_events(self.temperature_canvas)
        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=8)
        self.status_label = ttk.Label(self, text="Reading local counters...", foreground=MUTED)
        self.status_label.pack(anchor="w", padx=9, pady=4)
        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=8)
        ttk.Label(self, text="Most active processes", foreground=YELLOW).pack(anchor="w", padx=9, pady=(3, 0))
        self.process_label = ttk.Label(self, text="Waiting for CPU sample...", justify="left", anchor="nw", font="TkFixedFont")
        self.process_label.pack(fill="x", padx=9, pady=(1, 5))
        self.after(20, self.poll)

    def bind_graph_events(self, canvas: tk.Canvas) -> None:
        canvas.bind("<Configure>", lambda _event: self.redraw())
        canvas.bind("<Enter>", lambda event: self.show_graph_tooltip(canvas, event))
        canvas.bind("<Motion>", lambda event: self.show_graph_tooltip(canvas, event))
        canvas.bind("<Leave>", lambda _event: self.hide_graph_tooltip())

    def show_graph_tooltip(self, canvas: tk.Canvas, event: tk.Event | None = None) -> None:
        if event is not None:
            self.tooltip_canvas = canvas
            self.tooltip_x = event.x_root
            self.tooltip_y = event.y_root
        if not self.history:
            return
        sample = self.history[-1]
        lines = []
        if canvas is self.memory_graph:
            total = sample.get("memory_total")
            for name, _color in zip(("used", "buff", "slab", "cache", "free"), RAM_COLORS):
                percent = sample["memory_parts"].get(name)
                value = total * percent / 100 if total is not None and percent is not None else None
                if value is not None:
                    lines.append(f"{name}: {human_bytes(value)} ({percent:.0f}%)")
        elif canvas is self.cpu_graph:
            for field, _color in (("cpu", YELLOW),):
                value = sample.get(field)
                if value is not None:
                    lines.append(f"{field}: {value:.1f}%")
            for name in sorted(sample["cores"], key=lambda name: int(name[3:])):
                lines.append(f"{name}: {sample['cores'][name]:.1f}%")
        elif canvas is self.io_graph:
            for name in ("disk", "rx", "tx"):
                value = sample.get(name)
                lines.append(f"{name}: {human_bytes(value)}/s")
        else:
            value = sample.get("temperature")
            if value is not None:
                lines.append(f"temperature: {value:.1f} C")
        if not lines:
            return
        if not hasattr(self, "graph_tooltip") or not self.graph_tooltip.winfo_exists():
            self.graph_tooltip = tk.Toplevel(self)
            self.graph_tooltip.wm_overrideredirect(True)
            self.graph_tooltip.configure(bg=MUTED)
            tooltip_contents = tk.Frame(self.graph_tooltip, bg="#252a30", padx=1, pady=1)
            tooltip_contents.pack()
            self.graph_tooltip_label = tk.Label(
                tooltip_contents,
                justify="left",
                anchor="w",
                bg="#252a30",
                fg=FG,
                padx=7,
                pady=4,
                font="TkFixedFont",
            )
            self.graph_tooltip_label.pack()
        self.graph_tooltip_label.configure(text="\n".join(lines))
        self.graph_tooltip.update_idletasks()
        self.graph_tooltip.geometry(f"+{self.tooltip_x + 12}+{self.tooltip_y + 12}")
        self.graph_tooltip.deiconify()
        if not getattr(self, "tooltip_refresh_pending", False):
            self.tooltip_refresh_pending = True
            self.after(250, self.refresh_graph_tooltip)

    def refresh_graph_tooltip(self) -> None:
        self.tooltip_refresh_pending = False
        canvas = getattr(self, "tooltip_canvas", None)
        if canvas is None:
            return
        self.show_graph_tooltip(canvas)

    def hide_graph_tooltip(self) -> None:
        self.tooltip_canvas = None
        if hasattr(self, "graph_tooltip") and self.graph_tooltip.winfo_exists():
            self.graph_tooltip.withdraw()

    def poll(self) -> None:
        now = os.times().elapsed
        previous_cpu = self.previous_cpu
        load, self.previous_cpu = cpu_load(self.previous_cpu)
        processes, self.previous_processes = process_cpu_loads(self.previous_processes, previous_cpu, self.previous_cpu)
        memory, used, total, memory_parts = read_memory()
        temperature = read_temperatures()
        disk = read_disk_bytes()
        network = read_network_bytes()
        elapsed = now - self.last_sample_time if self.last_sample_time is not None else None
        disk_rate = (disk - self.previous_disk) / elapsed if elapsed and self.previous_disk is not None else None
        rx_rate = (network[0] - self.previous_network[0]) / elapsed if elapsed and self.previous_network else None
        tx_rate = (network[1] - self.previous_network[1]) / elapsed if elapsed and self.previous_network else None
        self.previous_disk, self.previous_network, self.last_sample_time = disk, network, now
        core_loads, self.previous_cpu_cores = cpu_core_loads(self.previous_cpu_cores)
        sample = {
            "cpu": load,
            "cores": core_loads,
            "memory": memory,
            "memory_total": total,
            "memory_parts": memory_parts,
            "temperature": temperature,
            "disk": disk_rate,
            "rx": rx_rate,
            "tx": tx_rate,
        }
        self.history.append(sample)
        self.cpu_label.configure(text=f"CPU {load:.0f}%" if load is not None else "CPU --%")
        self.memory_label.configure(text=f"RAM {memory:.0f}%" if memory is not None else "RAM --%")
        for name, label in self.memory_part_labels.items():
            percent = memory_parts.get(name)
            value = total * percent / 100 if total is not None and percent is not None else None
            label.configure(text=f"{name} {human_bytes(value)} ({percent:.0f}%)" if value is not None else f"{name} --")
        self.temp_label.configure(text=f"CPU {temperature:.0f} C" if temperature is not None else "CPU -- C", foreground=TEMP_LINE)
        self.disk_label.configure(text=f"Disk {human_bytes(disk_rate)}/s")
        self.rx_label.configure(text=f"Net {human_bytes(rx_rate)}/s in")
        self.tx_label.configure(text=f"{human_bytes(tx_rate)}/s out")
        self.status_label.configure(text=f"RAM {human_bytes(used)} / {human_bytes(total)}   Local counters")
        if processes:
            rows = ["CPU%   PID      COMMAND"]
            rows.extend(f"{load:<5.1f}  {pid:<7d}  {name[:28]}" for load, pid, name in processes[:8])
            self.process_label.configure(text="\n".join(rows))
        else:
            self.process_label.configure(text="Waiting for CPU sample...")
        self.redraw()
        self.after(self.poll_ms, self.poll)

    def redraw(self) -> None:
        core_names = sorted({name for item in self.history for name in item["cores"]})
        cpu_fields = [("cpu", YELLOW)]
        cpu_fields.extend((name, CORE_COLORS[index % len(CORE_COLORS)]) for index, name in enumerate(core_names))
        self.draw_graph(self.cpu_graph, tuple(cpu_fields), "cpu_graph_photo")
        self.draw_graph(
            self.memory_graph,
            tuple(zip(("used", "buff", "slab", "cache", "free"), RAM_COLORS)),
            "memory_graph_photo",
            stack=True,
        )
        self.draw_graph(self.io_graph, (("disk", PURPLE), ("rx", GREEN), ("tx", RED)), "io_graph_photo")
        self.draw_graph(self.temperature_canvas, (("temperature", TEMP_LINE),), "temperature_graph_photo", temperature=True)

    def draw_graph(
        self,
        canvas: tk.Canvas,
        fields: tuple[tuple[str, str], ...],
        photo_attr: str,
        temperature: bool = False,
        stack: bool = False,
    ) -> None:
        canvas.delete("all")
        width, height, scale = max(1, canvas.winfo_width()), max(1, canvas.winfo_height()), 3
        render_width, render_height = width * scale, height * scale
        image = Image.new("RGBA", (render_width, render_height), BG)
        image_draw = ImageDraw.Draw(image)
        def field_value(item: dict, field: str):
            if stack:
                return item["memory_parts"].get(field)
            return item["cores"].get(field) if field.startswith("cpu") and field != "cpu" else item.get(field)

        values = [field_value(item, field) for item in self.history for field, _color in fields if isinstance(field_value(item, field), (int, float))]
        value_max = 100 if not temperature else max(100, ((max(values) + 9) // 10) * 10) if values else 100
        if not temperature and any(field in ("disk", "rx", "tx") for field, _color in fields):
            rate_values = [item[field] for item in self.history for field in ("disk", "rx", "tx") if isinstance(item[field], (int, float))]
            if rate_values:
                value_max = max(value_max, max(rate_values) * 1.15)
        if temperature:
            # Temperature zones stay behind the grid and trace, with enough transparency
            # to preserve the dark dashboard background.
            zone_layer = Image.new("RGBA", (render_width, render_height), (0, 0, 0, 0))
            zone_draw = ImageDraw.Draw(zone_layer)
            zones = ((0, 60, "#55d187"), (60, 80, "#f2c94c"), (80, value_max, "#ff6b6b"))
            for low, high, color in zones:
                top = render_height - min(value_max, high) * render_height / value_max
                bottom = render_height - min(value_max, low) * render_height / value_max
                rgb = tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))
                zone_draw.rectangle((0, top, render_width, bottom), fill=(*rgb, 42))
            image = Image.alpha_composite(image, zone_layer)
            image_draw = ImageDraw.Draw(image)
        for value in (25, 50, 75):
            y = render_height - value * render_height / 100
            image_draw.line((0, y, render_width, y), fill=GRID, width=scale)
        if len(self.history) >= 2:
            series = []
            temperature_points = []
            if stack:
                stacked = [0.0] * len(self.history)
                for field, color in fields:
                    top_points = []
                    bottom_points = []
                    for index, item in enumerate(self.history):
                        value = field_value(item, field) or 0
                        x = index * (width - 1) / max(1, len(self.history) - 1)
                        bottom = stacked[index]
                        stacked[index] += value
                        top_points.append((x, height - stacked[index] * (height - 1) / value_max))
                        bottom_points.append((x, height - bottom * (height - 1) / value_max))
                    series.append((top_points, bottom_points, color))
            else:
                for field, color in fields:
                    points = []
                    for index, item in enumerate(self.history):
                        value = field_value(item, field)
                        if not isinstance(value, (int, float)):
                            continue
                        x = index * (width - 1) / max(1, len(self.history) - 1)
                        y = height - max(0, min(value_max, value)) * (height - 1) / value_max
                        points.append((x, y))
                        if temperature:
                            temperature_points.append((x, y, value))
                    if len(points) >= 2:
                        series.append((points, color))
            for series_item in series:
                if stack:
                    points, bottom_points, color = series_item
                else:
                    points, color = series_item
                rgb = tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))
                layer = Image.new("RGBA", (render_width, render_height), (0, 0, 0, 0))
                layer_draw = ImageDraw.Draw(layer)
                scaled = [(x * scale, y * scale) for x, y in points]
                if stack:
                    scaled_bottom = [(x * scale, y * scale) for x, y in reversed(bottom_points)]
                    layer_draw.polygon(scaled + scaled_bottom, fill=(*rgb, 130))
                else:
                    layer_draw.polygon(scaled + [(x, render_height) for x, _y in reversed(scaled)], fill=(*rgb, 52))
                image = Image.alpha_composite(image, layer)
                ImageDraw.Draw(image).line(scaled, fill=color, width=scale if stack else 2 * scale, joint="curve")
            if temperature and len(temperature_points) >= 2:
                fill_layer = Image.new("RGBA", (render_width, render_height), (0, 0, 0, 0))
                fill_draw = ImageDraw.Draw(fill_layer)
                image_draw = ImageDraw.Draw(image)
                for first, second in zip(temperature_points, temperature_points[1:]):
                    x1, y1, value1 = first
                    x2, y2, value2 = second
                    split_points = [0.0, 1.0]
                    for limit in (60, 80):
                        if (value1 - limit) * (value2 - limit) < 0:
                            split_points.append((limit - value1) / (value2 - value1))
                    split_points.sort()
                    for start, end in zip(split_points, split_points[1:]):
                        midpoint = (start + end) / 2
                        midpoint_value = value1 + (value2 - value1) * midpoint
                        if midpoint_value < 60:
                            continue
                        color = RED if midpoint_value >= 80 else YELLOW
                        start_point = ((x1 + (x2 - x1) * start) * scale, (y1 + (y2 - y1) * start) * scale)
                        end_point = ((x1 + (x2 - x1) * end) * scale, (y1 + (y2 - y1) * end) * scale)
                        rgb = tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))
                        fill_draw.polygon(
                            [start_point, end_point, (end_point[0], render_height), (start_point[0], render_height)],
                            fill=(*rgb, 52),
                        )
                        image_draw.line((*start_point, *end_point), fill=color, width=2 * scale)
                image = Image.alpha_composite(image, fill_layer)
                image_draw = ImageDraw.Draw(image)
                for first, second in zip(temperature_points, temperature_points[1:]):
                    x1, y1, value1 = first
                    x2, y2, value2 = second
                    split_points = [0.0, 1.0]
                    for limit in (60, 80):
                        if (value1 - limit) * (value2 - limit) < 0:
                            split_points.append((limit - value1) / (value2 - value1))
                    split_points.sort()
                    for start, end in zip(split_points, split_points[1:]):
                        midpoint = (start + end) / 2
                        midpoint_value = value1 + (value2 - value1) * midpoint
                        if midpoint_value < 60:
                            continue
                        color = RED if midpoint_value >= 80 else YELLOW
                        start_point = ((x1 + (x2 - x1) * start) * scale, (y1 + (y2 - y1) * start) * scale)
                        end_point = ((x1 + (x2 - x1) * end) * scale, (y1 + (y2 - y1) * end) * scale)
                        image_draw.line((*start_point, *end_point), fill=color, width=2 * scale)
        image = image.resize((width, height), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(image)
        setattr(self, photo_attr, photo)
        canvas.create_image(0, 0, image=photo, anchor="nw")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--poll", type=float, default=2.0, help="sample interval in seconds")
    parser.add_argument("--graph-minutes", type=float, default=5.0, help="history displayed in the graphs")
    args = parser.parse_args()
    Dashboard(max(0.5, args.poll), max(0.1, args.graph_minutes)).mainloop()


if __name__ == "__main__":
    main()
