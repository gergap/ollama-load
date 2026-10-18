#!/usr/bin/env python3
"""Small HTTP monitoring service for an NVIDIA-backed Ollama host."""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.request import Request, urlopen


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def query_gpus() -> list[dict]:
    fields = (
        "index,name,utilization.gpu,memory.used,memory.total,"
        "temperature.gpu,power.draw"
    )
    result = subprocess.run(
        [
            "nvidia-smi",
            f"--query-gpu={fields}",
            "--format=csv,noheader,nounits",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    gpus = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        values = [value.strip() for value in line.split(",")]
        if len(values) != 7:
            continue

        def number(value: str) -> float | None:
            try:
                return float(value)
            except ValueError:
                return None

        gpus.append(
            {
                "index": int(values[0]),
                "name": values[1],
                "utilization_percent": number(values[2]),
                "memory_used_mib": number(values[3]),
                "memory_total_mib": number(values[4]),
                "temperature_c": number(values[5]),
                "power_w": number(values[6]),
            }
        )
    return gpus


def query_ollama(base_url: str) -> list[dict]:
    request = Request(base_url.rstrip("/") + "/api/ps")
    with urlopen(request, timeout=5) as response:
        payload = json.load(response)

    models = []
    for item in payload.get("models", []):
        size = int(item.get("size") or 0)
        size_vram = int(item.get("size_vram") or 0)
        gpu_percent = max(0, min(100, round(100 * size_vram / size))) if size else None
        models.append(
            {
                "name": item.get("name") or item.get("model") or "unknown",
                "size_bytes": size,
                "size_vram_bytes": size_vram,
                "gpu_percent": gpu_percent,
                "context_length": int(item.get("context_length") or 0),
                "expires_at": item.get("expires_at"),
            }
        )
    return models


def summarize_gpus(gpus: list[dict]) -> dict:
    loads = [g["utilization_percent"] for g in gpus if g["utilization_percent"] is not None]
    temperatures = [g.get("temperature_c") for g in gpus if g.get("temperature_c") is not None]
    used = sum(g["memory_used_mib"] or 0 for g in gpus)
    total = sum(g["memory_total_mib"] or 0 for g in gpus)
    return {
        "gpu_percent": round(max(loads), 1) if loads else None,
        "memory_percent": round(100 * used / total, 1) if total else None,
        "memory_used_mib": round(used, 1),
        "memory_total_mib": round(total, 1),
        "gpu_temperature_c": round(max(temperatures), 1) if temperatures else None,
    }


def read_cpu_times() -> tuple[int, int] | None:
    """Return total and idle CPU jiffies from Linux's aggregate CPU line."""
    with open("/proc/stat", encoding="ascii") as proc_stat:
        fields = proc_stat.readline().split()
    if not fields or fields[0] != "cpu" or len(fields) < 5:
        return None
    values = [int(value) for value in fields[1:]]
    return sum(values), values[3] + (values[4] if len(values) > 4 else 0)


def cpu_load_percent(previous: tuple[int, int] | None) -> tuple[float | None, tuple[int, int] | None]:
    current = read_cpu_times()
    if previous is None or current is None:
        return None, current
    total_delta = current[0] - previous[0]
    idle_delta = current[1] - previous[1]
    if total_delta <= 0:
        return None, current
    return round(max(0, min(100, 100 * (total_delta - idle_delta) / total_delta)), 1), current


def query_cpu_temperature() -> float | None:
    """Read the hottest CPU sensor exposed by Linux, in degrees Celsius."""
    candidates = []

    for input_path in glob.glob("/sys/class/hwmon/hwmon*/temp*_input"):
        sensor_dir = os.path.dirname(input_path)
        sensor_file = os.path.basename(input_path)
        label_path = os.path.join(sensor_dir, sensor_file.replace("_input", "_label"))
        try:
            with open(os.path.join(sensor_dir, "name"), encoding="ascii") as name_file:
                sensor_name = name_file.read().strip().lower()
            try:
                with open(label_path, encoding="ascii") as label_file:
                    label = label_file.read().strip().lower()
            except OSError:
                label = ""
            if not (
                any(name in label for name in ("cpu", "core", "package", "tdie", "tctl"))
                or any(name in sensor_name for name in ("cpu", "coretemp", "k10temp", "zenpower"))
            ):
                continue
            with open(input_path, encoding="ascii") as temp_file:
                value = float(temp_file.read().strip()) / 1000
        except (OSError, ValueError):
            continue
        if -100 <= value <= 200:
            candidates.append(value)

    fallback = []
    for type_path in glob.glob("/sys/class/thermal/thermal_zone*/type"):
        zone = os.path.dirname(type_path)
        try:
            with open(type_path, encoding="ascii") as type_file:
                zone_type = type_file.read().strip().lower()
            with open(os.path.join(zone, "temp"), encoding="ascii") as temp_file:
                value = float(temp_file.read().strip()) / 1000
        except (OSError, ValueError):
            continue
        if not -100 <= value <= 200:
            continue
        if any(name in zone_type for name in ("cpu", "x86", "package", "core")):
            candidates.append(value)
        else:
            fallback.append(value)
    temperatures = candidates or fallback
    return round(max(temperatures), 1) if temperatures else None


def query_fans() -> list[dict]:
    """Read fan speeds from Linux hwmon sensors, in RPM."""
    fans = []
    for input_path in sorted(glob.glob("/sys/class/hwmon/hwmon*/fan*_input")):
        sensor_dir = os.path.dirname(input_path)
        sensor_file = os.path.basename(input_path)
        label_path = os.path.join(sensor_dir, sensor_file.replace("_input", "_label"))
        try:
            with open(input_path, encoding="ascii") as fan_file:
                speed = float(fan_file.read().strip())
            with open(os.path.join(sensor_dir, "name"), encoding="ascii") as name_file:
                sensor_name = name_file.read().strip()
            try:
                with open(label_path, encoding="ascii") as label_file:
                    sensor_label = label_file.read().strip()
            except OSError:
                sensor_label = ""
        except (OSError, ValueError):
            continue
        if speed < 0:
            continue
        fans.append({
            "id": sensor_file.removesuffix("_input"),
            "speed_rpm": round(speed, 1),
            "sensor": sensor_name,
            "label": sensor_label,
        })
    return fans


class Collector:
    def __init__(self, ollama_url: str, interval: float, history_size: int):
        self.ollama_url = ollama_url
        self.interval = interval
        self.history: deque[dict] = deque(maxlen=history_size)
        self.current: dict = {"timestamp": utc_now(), "gpus": [], "fans": [], "models": [], "gpu_summary": {}, "errors": ["No sample yet"]}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.cpu_times: tuple[int, int] | None = None

    def collect(self) -> dict:
        errors = []
        try:
            gpus = query_gpus()
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            gpus = []
            detail = getattr(exc, "stderr", None)
            detail = detail.strip() if isinstance(detail, str) else ""
            errors.append(f"nvidia-smi: {detail or exc}")
        try:
            models = query_ollama(self.ollama_url)
        except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
            models = []
            errors.append(f"ollama: {exc}")
        fans = query_fans()

        timestamp = utc_now()
        summary = summarize_gpus(gpus)
        try:
            summary["cpu_percent"], self.cpu_times = cpu_load_percent(self.cpu_times)
        except (OSError, ValueError) as exc:
            summary["cpu_percent"] = None
            errors.append(f"cpu: {exc}")
        summary["cpu_temperature_c"] = query_cpu_temperature()
        sample = {
            "timestamp": timestamp,
            "gpus": gpus,
            "fans": fans,
            "gpu_summary": summary,
            "models": models,
            "errors": errors,
        }
        history_item = {
            "timestamp": timestamp,
            **summary,
            "fan_speeds": {fan["id"]: fan["speed_rpm"] for fan in fans},
        }
        with self.lock:
            self.current = sample
            self.history.append(history_item)
        return sample

    def run(self) -> None:
        while not self.stop_event.is_set():
            started = time.monotonic()
            self.collect()
            self.stop_event.wait(max(0.1, self.interval - (time.monotonic() - started)))

    def snapshot(self) -> dict:
        with self.lock:
            return {**self.current, "history": list(self.history)}


def make_handler(collector: Collector, token: str | None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            if token and self.headers.get("Authorization") != f"Bearer {token}":
                self.send_error(401)
                return
            if self.path == "/health":
                payload = {"status": "ok"}
            elif self.path == "/api/status":
                payload = collector.snapshot()
            else:
                self.send_error(404)
                return
            body = json.dumps(payload, separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            if os.environ.get("OLLAMA_LOAD_ACCESS_LOG") == "1":
                super().log_message(fmt, *args)

    return Handler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default=os.environ.get("OLLAMA_LOAD_BIND", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("OLLAMA_LOAD_PORT", "8765")))
    parser.add_argument("--interval", type=float, default=float(os.environ.get("OLLAMA_LOAD_INTERVAL", "2")))
    parser.add_argument("--history", type=int, default=int(os.environ.get("OLLAMA_LOAD_HISTORY", "300")))
    parser.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    collector = Collector(args.ollama_url, max(0.5, args.interval), max(2, args.history))
    collector.collect()
    thread = threading.Thread(target=collector.run, name="collector", daemon=True)
    thread.start()
    server = ThreadingHTTPServer((args.bind, args.port), make_handler(collector, os.environ.get("OLLAMA_LOAD_TOKEN")))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        collector.stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
