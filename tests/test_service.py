import json
import io
import unittest
from unittest.mock import MagicMock, patch

import ollama_load_service as service


class ServiceTests(unittest.TestCase):
    @patch("ollama_load_service.subprocess.run")
    def test_query_gpus(self, run):
        run.return_value = MagicMock(stdout="0, NVIDIA RTX 5090, 87, 20300, 32607, 61, 410.5\n")
        self.assertEqual(
            service.query_gpus()[0]["memory_total_mib"],
            32607.0,
        )

    @patch("ollama_load_service.urlopen")
    def test_query_ollama_and_offload_percentage(self, open_url):
        payload = {
            "models": [{
                "name": "qwen3.8:27b",
                "size": 20_000,
                "size_vram": 17_000,
                "context_length": 128_000,
                "expires_at": "tomorrow",
            }]
        }
        response = MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        open_url.return_value = response
        with patch("ollama_load_service.json.load", return_value=payload):
            model = service.query_ollama("http://localhost:11434")[0]
        self.assertEqual(model["gpu_percent"], 85)
        self.assertEqual(model["context_length"], 128_000)

    def test_summary_uses_total_vram(self):
        result = service.summarize_gpus([
            {"utilization_percent": 40, "memory_used_mib": 10, "memory_total_mib": 20},
            {"utilization_percent": 60, "memory_used_mib": 30, "memory_total_mib": 60},
        ])
        self.assertEqual(result["gpu_percent"], 60)
        self.assertEqual(result["memory_percent"], 50)

    def test_summary_includes_gpu_temperature(self):
        result = service.summarize_gpus([
            {"utilization_percent": 40, "memory_used_mib": 10, "memory_total_mib": 20, "temperature_c": 61},
            {"utilization_percent": 60, "memory_used_mib": 30, "memory_total_mib": 60, "temperature_c": 72},
        ])
        self.assertEqual(result["gpu_temperature_c"], 72)

    def test_cpu_load(self):
        with patch("ollama_load_service.read_cpu_times", return_value=(200, 50)):
            load, current = service.cpu_load_percent((100, 20))
        self.assertEqual(load, 70)
        self.assertEqual(current, (200, 50))

    @patch("ollama_load_service.glob.glob")
    @patch("ollama_load_service.open", create=True)
    def test_cpu_temperature_from_hwmon(self, open_file, glob):
        glob.side_effect = lambda pattern: (
            ["/sys/class/hwmon/hwmon0/temp1_input"]
            if "hwmon" in pattern else []
        )
        files = {
            "/sys/class/hwmon/hwmon0/name": "coretemp\n",
            "/sys/class/hwmon/hwmon0/temp1_label": "Package id 0\n",
            "/sys/class/hwmon/hwmon0/temp1_input": "63500\n",
        }
        open_file.side_effect = lambda path, **_kwargs: io.StringIO(files[path])
        self.assertEqual(service.query_cpu_temperature(), 63.5)

    @patch("ollama_load_service.glob.glob")
    @patch("ollama_load_service.open", create=True)
    def test_cpu_temperature_from_thermal_zone(self, open_file, glob):
        glob.side_effect = lambda pattern: (
            ["/sys/class/thermal/thermal_zone0/type"]
            if "thermal_zone" in pattern else []
        )
        files = {
            "/sys/class/thermal/thermal_zone0/type": "x86_pkg_temp\n",
            "/sys/class/thermal/thermal_zone0/temp": "71000\n",
        }
        open_file.side_effect = lambda path, **_kwargs: io.StringIO(files[path])
        self.assertEqual(service.query_cpu_temperature(), 71.0)

    @patch("ollama_load_service.glob.glob")
    @patch("ollama_load_service.open", create=True)
    def test_query_fans(self, open_file, glob):
        glob.return_value = [
            "/sys/class/hwmon/hwmon0/fan1_input",
            "/sys/class/hwmon/hwmon0/fan3_input",
        ]
        files = {
            "/sys/class/hwmon/hwmon0/fan1_input": "1200\n",
            "/sys/class/hwmon/hwmon0/fan1_label": "CPU fan\n",
            "/sys/class/hwmon/hwmon0/fan3_input": "800\n",
            "/sys/class/hwmon/hwmon0/fan3_label": "Case fan\n",
            "/sys/class/hwmon/hwmon0/name": "it8613\n",
        }
        open_file.side_effect = lambda path, **_kwargs: io.StringIO(files[path])
        self.assertEqual(service.query_fans(), [
            {"id": "fan1", "speed_rpm": 1200.0, "sensor": "it8613", "label": "CPU fan"},
            {"id": "fan3", "speed_rpm": 800.0, "sensor": "it8613", "label": "Case fan"},
        ])

    @patch("ollama_load_service.query_gpus")
    @patch("ollama_load_service.query_ollama", return_value=[])
    def test_nvidia_error_includes_stderr(self, _query_ollama, query_gpus):
        error = service.subprocess.CalledProcessError(9, "nvidia-smi", stderr="driver failed\n")
        query_gpus.side_effect = error
        sample = service.Collector("http://localhost:11434", 2, 2).collect()
        self.assertEqual(sample["errors"], ["nvidia-smi: driver failed"])


if __name__ == "__main__":
    unittest.main()
