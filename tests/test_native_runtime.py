"""Tests for native runtime, real CPython tracing, and durable automatic dispatch."""
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from custom_components.hass_profiler.detector import Sample
from custom_components.hass_profiler import profiler
from custom_components.hass_profiler.runtime import Runtime, Settings
from custom_components.hass_profiler.telemetry import LinuxSampler


def healthy():
    return {"available_kib": 3000000, "swap_free_kib": 1100000,
            "rss_kib": 1800000, "process_swap_kib": 100000,
            "anonymous_kib": 1700000, "file_kib": 100000}


def synthetic_allocator():
    namespace = {}
    exec(compile("def allocate():\n    return bytearray(524288)\n",
                 "/usr/src/homeassistant/homeassistant/components/example/sensor.py", "exec"), namespace)
    return namespace["allocate"]


class NativeProfilerTests(unittest.TestCase):
    def test_native_trace_retained_allocations(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(profiler, "memory_headroom", side_effect=healthy):
            retained = []
            result = []
            thread = threading.Thread(target=lambda: result.append(
                profiler.capture_profile(5, False, Path(tmp), threading.Event())))
            thread.start()
            try:
                deadline = time.monotonic() + 2
                while not profiler.tracemalloc.is_tracing() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(profiler.tracemalloc.is_tracing())
                retained.append(synthetic_allocator()())
            finally:
                thread.join(10)
            self.assertFalse(thread.is_alive())
            p = result[0]
            self.assertEqual(p["status"], "complete")
            self.assertTrue(p["trace_stopped"])
            row = next(x for x in p["allocation"]["integrations"] if x["integration"] == "example")
            self.assertGreaterEqual(row["bytes"], len(retained[0]))
            self.assertEqual(json.loads((Path(tmp)/p["file"]).read_text()), p)

    def test_cancellation_and_existing_tracer(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(profiler, "memory_headroom", side_effect=healthy):
            cancel = threading.Event()
            cancel.set()
            p = profiler.capture_profile(5, False, Path(tmp), cancel)
            self.assertEqual(p["status"], "cancelled")
            self.assertFalse(profiler.tracemalloc.is_tracing())
            profiler.tracemalloc.start(1)
            try:
                self.assertEqual(profiler.capture_profile(5, False, Path(tmp), threading.Event())["status"], "skipped_existing_tracer")
                self.assertTrue(profiler.tracemalloc.is_tracing())
            finally:
                profiler.tracemalloc.stop()

    def test_headroom_rejects(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(profiler, "memory_headroom", return_value={**healthy(), "available_kib": 10}):
            self.assertEqual(profiler.capture_profile(5, False, Path(tmp), threading.Event())["status"], "skipped_headroom")
            self.assertFalse(profiler.tracemalloc.is_tracing())


class TestNativeRuntime(unittest.TestCase):
    def sample(self, ts, core=1800, available=2300):
        return Sample(ts, core, available=available, swap=800, swap_free=1100,
                      process_rss=core-100, process_swap=100)

    def test_real_automatic_capture_through_native_runtime(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(profiler, "memory_headroom", side_effect=healthy):
            path = Path(tmp)
            runtime = Runtime(path, Settings(profile_seconds=5))
            now = time.time()
            retained = []
            try:
                runtime.observe(self.sample(now-15), now-15)
                future = runtime.observe(self.sample(now, 1920), now)
                self.assertIsNotNone(future)
                deadline = time.monotonic() + 2
                while not profiler.tracemalloc.is_tracing() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(profiler.tracemalloc.is_tracing())
                retained.append(synthetic_allocator()())
                result = future.result(timeout=12)
                self.assertEqual(result["status"], "complete")
                p = json.loads((path/result["file"]).read_text())
                self.assertEqual(p, result)
                self.assertGreaterEqual(next(x["bytes"] for x in p["allocation"]["integrations"] if x["integration"]=="example"), 524288)
                control = json.loads((path/"profile-control.json").read_text())
                self.assertEqual(control["last"]["file"], p["file"])
                self.assertEqual(control["last"]["status"], "complete")
                rows = [json.loads(line) for f in path.glob("incident-*.jsonl") for line in f.read_text().splitlines()]
                self.assertTrue(any(row["type"]=="profile_start" and "rapid_60s" in row["reasons"] for row in rows))
                self.assertTrue(any(row["type"]=="profile_result" and row["file"]==p["file"] for row in rows))
                self.assertIsNone(runtime.observe(self.sample(now+15, 2080), now+15))
            finally:
                runtime.stop()
            restarted = Runtime(path, Settings(profile_seconds=5))
            try:
                self.assertEqual(restarted.status()["profile_last"]["file"], p["file"])
                self.assertEqual(restarted.request_manual(5,False).result()["status"], "cooldown")
            finally:
                restarted.stop()

    def test_disabled_unsafe_stale_and_unload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            r = Runtime(path, Settings(automatic_memory_profiling=False, profile_seconds=5))
            try:
                t=time.time()
                r.observe(self.sample(t-15),t-15)
                self.assertIsNone(r.observe(self.sample(t, 1950),t))
                self.assertFalse((path/"profile-control.json").exists())
            finally:
                r.stop()
            r = Runtime(path, Settings(profile_seconds=5))
            try:
                t=time.time()
                r.observe(self.sample(t-15),t-15)
                self.assertIsNone(r.observe(self.sample(t, 2000, 800),t))
                self.assertIsNone(r.observe(self.sample(t-120, 2600),t))
            finally:
                r.stop()
            self.assertEqual(r.request_manual(5,False).result()["status"], "stopped")

    def test_procfs_sampler_uses_current_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/"self").mkdir()
            (root/"meminfo").write_text("MemTotal: 6000000 kB\nMemAvailable: 2400000 kB\nMemFree: 400000 kB\nSwapTotal: 2000000 kB\nSwapFree: 1100000 kB\n")
            (root/"self"/"status").write_text("VmRSS: 1800000 kB\nVmSwap: 120000 kB\nRssAnon: 1700000 kB\nRssFile: 100000 kB\n")
            (root/"self"/"io").write_text("read_bytes: 1000\nwrite_bytes: 2000\n")
            s=LinuxSampler(root)
            first=s.read()
            self.assertEqual(first.core,1920000/1024)
            self.assertEqual(first.process_rss,1800000/1024)
            self.assertEqual(first.process_swap,120000/1024)
            self.assertEqual(first.available,2400000/1024)
            self.assertEqual(first.swap,900000/1024)
            self.assertIsNone(first.core_cpu)
            (root/"self"/"status").write_text("VmRSS: 1800000 kB\n")
            self.assertRaises(ValueError,s.read)


if __name__ == "__main__":
    unittest.main()
