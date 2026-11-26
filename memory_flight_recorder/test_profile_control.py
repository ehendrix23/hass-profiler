"""Production-feed-to-profile orchestration tests with a simulated HA transport."""
import asyncio
import datetime as dt
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from flight_recorder import Detector, Journal, Sample
from ha_feed import FIELDS, STAMP, SUBSCRIPTION, session
from profile_control import ProfileController


def sample(t, core=1800, available=2200, swap_free=1050):
    return Sample(t, core, available=available, swap=850, swap_free=swap_free)


class TestController(unittest.IsolatedAsyncioTestCase):
    async def test_unsafe_and_stale_samples_do_not_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            det = Detector(Journal(Path(tmp)))
            ctrl = ProfileController("ws://127.0.0.1:1", "fixture", det)
            now = time.time()
            self.assertFalse(ctrl.consider(sample(now, 2600, 900), {"absolute_core"}, now))
            self.assertFalse(ctrl.consider(sample(now-60, 2400), {"rapid_60s"}, now))
            self.assertFalse(ctrl.consider(sample(now, 1800), {"stale_telemetry"}, now))
            self.assertIsNone(ctrl.task)
            self.assertFalse((Path(tmp)/"profile-control.json").exists())

    async def test_automatic_full_feed_and_real_service_protocol(self):
        from websockets.asyncio.server import serve
        now = int(time.time())
        requests = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            det = Detector(Journal(path))
            filename = "core-profile-12345.json"
            report = {"version": "0.2.0", "status": "complete", "pid": 73,
                      "requested_seconds": 15, "trace_stopped": True,
                      "file": filename, "allocation": {"integrations": [
                          {"integration": "fixture", "bytes": 1024, "count": 2}]}}

            async def server(ws):
                await ws.send(json.dumps({"type": "auth_required"}))
                self.assertEqual(json.loads(await ws.recv()), {"type": "auth", "access_token": "fixture"})
                await ws.send(json.dumps({"type": "auth_ok"}))
                msg = json.loads(await ws.recv())
                if msg["type"] == "subscribe_entities":
                    self.assertEqual(msg["entity_ids"], SUBSCRIPTION)
                    await ws.send(json.dumps({"id": msg["id"], "type": "result", "success": True}))
                    def event(stamp, core):
                        states = {STAMP: {"s": dt.datetime.fromtimestamp(stamp, dt.timezone.utc).isoformat(), "lc": stamp}}
                        for k, v in {"core": core, "available": 2200, "swap": 850, "swap_free": 1050}.items():
                            states[FIELDS[k]] = {"s": str(v), "lc": stamp}
                        return {"id": msg["id"], "type": "event", "event": {"a": states}}
                    await ws.send(json.dumps(event(now-15, 1800)))
                    await asyncio.sleep(.08)
                    await ws.send(json.dumps(event(now, 1920)))
                    await asyncio.sleep(.1)
                    await ws.send(json.dumps(event(now+1, 2050)))
                    await asyncio.sleep(.3)
                else:
                    requests.append(msg)
                    self.assertEqual(msg["type"], "call_service")
                    self.assertEqual(msg["domain"], "pyscript")
                    self.assertEqual(msg["service"], "hass_profiler_memory")
                    self.assertTrue(msg["return_response"])
                    self.assertEqual(msg["service_data"], {"seconds": 15, "include_census": False})
                    (path/filename).write_text(json.dumps(report))
                    await ws.send(json.dumps({"id": msg["id"], "type": "result",
                                              "success": True, "result": {"response": report}}))

            async with serve(server, "127.0.0.1", 0) as srv:
                url = "ws://127.0.0.1:" + str(srv.sockets[0].getsockname()[1])
                ctrl = ProfileController(url, "fixture", det)
                from websockets.exceptions import ConnectionClosed
                with patch("ha_feed.SETTLE", .01):
                    with self.assertRaises(ConnectionClosed):
                        await asyncio.wait_for(session(url, "fixture", det, profiler=ctrl), 4)
                await asyncio.wait_for(ctrl.task, 2)
                self.assertEqual(len(requests), 1)
                status = json.loads((path/"profile-control.json").read_text())
                self.assertEqual(status["last"]["status"], "complete")
                self.assertEqual(status["last"]["file"], filename)
                self.assertEqual(status["last"]["top_integration"], "fixture")
                self.assertEqual(status["last"]["top_bytes"], 1024)
                rows = [json.loads(line) for f in det.journal.files() for line in f.read_text().splitlines()]
                self.assertTrue(any(r["type"] == "profile_start" and "rapid_60s" in r["reasons"] for r in rows))
                self.assertTrue(any(r["type"] == "profile_result" and r["file"] == filename for r in rows))
                restored = ProfileController(url, "fixture", det)
                self.assertFalse(restored.consider(sample(now+2, 2200), {"rapid_60s"}, now+2))
                self.assertEqual(restored.last["file"], filename)

    async def test_service_response_without_persisted_evidence_is_not_success(self):
        from websockets.asyncio.server import serve
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            det = Detector(Journal(Path(tmp)))
            async def server(ws):
                await ws.send(json.dumps({"type": "auth_required"}))
                await ws.recv()
                await ws.send(json.dumps({"type": "auth_ok"}))
                request = json.loads(await ws.recv())
                await ws.send(json.dumps({"id": request["id"], "type": "result", "success": True,
                    "result": {"response": {"version": "0.2.0", "status": "complete",
                    "file": "core-profile-123.json", "trace_stopped": True, "pid": 73,
                    "allocation": {"integrations": []}}}}))
            async with serve(server, "127.0.0.1", 0) as srv:
                url = "ws://127.0.0.1:" + str(srv.sockets[0].getsockname()[1])
                ctrl = ProfileController(url, "fixture", det)
                self.assertTrue(ctrl.consider(sample(now, 2400), {"rising_high_core"}, now))
                await asyncio.wait_for(ctrl.task, 2)
                self.assertEqual(ctrl.last["status"], "report_unverified")


if __name__ == "__main__":
    unittest.main()
