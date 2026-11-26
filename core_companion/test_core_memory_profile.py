"""Focused tests of the native Python implementation, with Pyscript decorators adapted locally."""
import importlib.util
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch
import tracemalloc

PATH = pathlib.Path(__file__).with_name("hass_profiler.py")


def load():
    spec = importlib.util.spec_from_file_location("core_companion_test", PATH)
    module = importlib.util.module_from_spec(spec)
    module.pyscript_compile = lambda f: f
    module.pyscript_executor = lambda f: f
    module.service = lambda **kw: lambda f: f
    spec.loader.exec_module(module)
    return module


class ProfileTests(unittest.TestCase):
    def test_attribution(self):
        m = load()
        self.assertEqual(m.integration_owner("/usr/src/homeassistant/homeassistant/components/recorder/core.py"), "recorder")
        self.assertEqual(m.integration_owner("/config/custom_components/aarlo/camera.py"), "custom:aarlo")
        self.assertIsNone(m.integration_owner("/usr/local/lib/python3.14/site-packages/aiohttp/client.py"))
        self.assertIsNone(m.integration_owner("/tmp/not_homeassistant/components/recorder/core.py"))
    def test_real_allocation_attribution(self):
        m = load()
        code = compile("def allocate():\n    return bytearray(524288)\n", "/usr/src/homeassistant/homeassistant/components/test_integration/synthetic.py", "exec")
        namespace = {}
        exec(code, namespace)
        self.assertFalse(tracemalloc.is_tracing())
        tracemalloc.start(3)
        try:
            retained = namespace["allocate"]()
            snapshot = tracemalloc.take_snapshot()
        finally:
            tracemalloc.stop()
        report = m.summarize_traces(snapshot)
        self.assertEqual(report["integrations"][0]["integration"], "test_integration")
        self.assertGreaterEqual(report["integrations"][0]["bytes"], len(retained))
        self.assertGreaterEqual(report["total_bytes"], report["attributed_bytes"])
    def test_existing_tracer_is_not_stopped(self):
        m = load()
        tracemalloc.start(1)
        try:
            with tempfile.TemporaryDirectory() as tmp, patch.object(m, "OUTPUT_DIR", pathlib.Path(tmp)):
                result = m.capture_profile(15)
                self.assertEqual(result["status"], "skipped_existing_tracer")
                self.assertTrue(tracemalloc.is_tracing())
        finally:
            tracemalloc.stop()
    def test_rejected_duration(self):
        m = load()
        self.assertEqual(m.capture_profile(3600)["status"], "invalid_duration")
    def test_low_headroom_does_not_start_tracing(self):
        m = load()
        with patch.object(m, "memory_headroom", return_value={"available_kib": 200, "swap_free_kib": 100, "rss_kib": 1800000}):
            self.assertEqual(m.capture_profile(15)["status"], "skipped_headroom")
        self.assertFalse(tracemalloc.is_tracing())
    def test_concurrency(self):
        m = load()
        self.assertTrue(m.CAPTURE_LOCK.acquire(blocking=False))
        try:
            self.assertEqual(m.capture_profile(15)["status"], "busy")
        finally:
            m.CAPTURE_LOCK.release()
    def test_current_instances(self):
        m = load()
        cls = type("Owned", (), {"__module__": "homeassistant.components.test_integration.sensor"})
        obj = cls()
        with patch.object(m, "memory_headroom", return_value={"available_kib": 3000000, "swap_free_kib": 1000000, "rss_kib": 1800000}):
            report = m.direct_instance_census()
        self.assertTrue(any(row["integration"] == "test_integration" and row["count"] >= 1 for row in report["integrations"]))
        self.assertIsInstance(obj, cls)

    def test_trace_budget_preserves_partial_attribution_and_stops(self):
        m = load()
        headroom = {"available_kib": 3000000, "swap_free_kib": 1100000, "rss_kib": 1800000}
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, "OUTPUT_DIR", pathlib.Path(tmp)), patch.object(m, "memory_headroom", return_value=headroom), patch.object(m, "MAX_TRACED_BYTES", 1):
            result = m.capture_profile(15, False)
            self.assertEqual(result["status"], "partial_trace_budget")
            self.assertTrue(result["trace_stopped"])
            self.assertIsInstance(result["allocation"], dict)
            persisted = __import__("json").loads((pathlib.Path(tmp) / result["file"]).read_text())
            self.assertEqual(persisted, result)
            self.assertFalse(tracemalloc.is_tracing())

if __name__ == "__main__":
    unittest.main()
