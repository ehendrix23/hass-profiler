"""Run the actual HA 2026.11 config flow, service registry, and entry unload."""
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntries, ConfigEntryState
from homeassistant import loader
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.hass_profiler import DOMAIN, profiler
from custom_components.hass_profiler.detector import Sample
from custom_components.hass_profiler.runtime import Runtime, Settings
from custom_components.hass_profiler.telemetry import LinuxSampler


class HomeAssistantLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_entry_actions_and_unload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "diagnostics"
            hass = HomeAssistant(tmp)
            loader.async_setup(hass)
            hass.config_entries = ConfigEntries(hass, {})
            await hass.config_entries.async_initialize()
            await hass.async_start()
            headroom = {"available_kib": 2500000, "swap_free_kib": 1000000,
                        "rss_kib": 1800000, "process_swap_kib": 100000}
            try:
                with patch("custom_components.hass_profiler._create_runtime",
                           side_effect=lambda _config, settings: Runtime(path, settings)), \
                     patch.object(LinuxSampler, "read", side_effect=lambda: Sample(
                         time.time(), 1800, available=2300, swap=800, swap_free=1000)), \
                     patch.object(profiler, "memory_headroom", return_value=headroom):
                    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
                    self.assertEqual(flow["type"], FlowResultType.FORM)
                    result = await hass.config_entries.flow.async_configure(
                        flow["flow_id"], asdict(Settings(automatic_memory_profiling=False, profile_seconds=5)))
                    self.assertEqual(result["type"], FlowResultType.CREATE_ENTRY)
                    entry = result["result"]
                    self.assertEqual(entry.state, ConfigEntryState.LOADED)
                    self.assertTrue(hass.services.has_service(DOMAIN, "profile_memory"))
                    self.assertTrue(hass.services.has_service(DOMAIN, "status"))
                    runtime = entry.runtime_data
                    state = await hass.services.async_call(DOMAIN, "status", {}, blocking=True, return_response=True)
                    self.assertEqual(state["memory_metric"], "core_process_rss_plus_swap_mib")
                    self.assertEqual(state["profiling"], "manual_or_disabled")
                    self.assertEqual(state["pid"], __import__("os").getpid())
                    response = await hass.services.async_call(DOMAIN, "profile_memory", {
                        "seconds": 5, "include_census": False}, blocking=True, return_response=True)
                    self.assertEqual(response["status"], "complete")
                    self.assertTrue(response["trace_stopped"])
                    self.assertEqual(json.loads((path/response["file"]).read_text()), response)
                    duplicate = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
                    self.assertEqual(duplicate["type"], FlowResultType.ABORT)
                    self.assertTrue(await hass.config_entries.async_unload(entry.entry_id))
                    self.assertFalse(hass.services.has_service(DOMAIN, "profile_memory"))
                    self.assertFalse(hass.services.has_service(DOMAIN, "status"))
                    self.assertTrue(runtime.stopped.is_set())
                    self.assertFalse(runtime.monitor.is_alive())
                    self.assertFalse(profiler.tracemalloc.is_tracing())
            finally:
                await hass.async_stop(force=True)


if __name__ == "__main__":
    unittest.main()
