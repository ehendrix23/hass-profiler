"""Dispatch bounded in-Core allocation profiles without blocking telemetry."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import re
import time

from flight_recorder import Detector, Sample, STALE

LOG = logging.getLogger("memory_watch")
GROWTH_FLAGS = {"rapid_60s", "rapid_5min", "rising_high_core"}
MIN_AVAILABLE = 1800
MIN_SWAP_FREE = 800
COOLDOWN = 60
RATE_WINDOW = 300
MAX_ATTEMPTS = 3
MAX_REPORT_BYTES = 65536
REPORT_NAME = re.compile(r"core-profile-[0-9]{1,24}\.json")
OWNER_NAME = re.compile(r"(?:custom:)?[a-z][a-z0-9_]{0,63}")


async def invoke_profile(url: str, token: str, seconds: int) -> dict:
    """Use a short-lived service connection; the telemetry socket never waits for profiling."""
    from websockets.asyncio.client import connect

    async with connect(url, open_timeout=5, close_timeout=2, ping_interval=20,
                       ping_timeout=5, max_size=262144, max_queue=4,
                       compression=None) as ws:
        async def receive(timeout: float = 5):
            return json.loads(await asyncio.wait_for(ws.recv(), timeout))

        if (await receive()).get("type") != "auth_required":
            raise RuntimeError("profile_handshake_failed")
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        if (await receive()).get("type") != "auth_ok":
            raise RuntimeError("profile_auth_failed")
        await ws.send(json.dumps({
            "id": 1, "type": "call_service", "domain": "pyscript",
            "service": "hass_profiler_memory",
            "service_data": {"seconds": seconds, "include_census": False},
            "return_response": True,
        }))
        response = await receive(seconds + 25)
        if response.get("id") != 1 or response.get("success") is not True:
            raise RuntimeError("profile_service_failed")
        result = response.get("result", {}).get("response")
        if not isinstance(result, dict):
            raise ValueError("invalid_profile_response")
        return result


def read_report(directory: Path, response: dict) -> dict:
    """Verify the actual Core-written report before recording success."""
    status = response.get("status")
    if not isinstance(status, str) or not re.fullmatch(r"[a-z_]{1,48}", status):
        return {"status": "invalid_response"}
    filename = response.get("file")
    if filename is None and status != "complete":
        return {"status": status}
    if not isinstance(filename, str) or not REPORT_NAME.fullmatch(filename):
        return {"status": "report_unverified"}
    try:
        fd = os.open(directory / filename, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as file:
            raw = file.read(MAX_REPORT_BYTES + 1)
        if len(raw) > MAX_REPORT_BYTES:
            return {"status": "report_unverified"}
        saved = json.loads(raw)
    except (OSError, ValueError):
        return {"status": "report_unverified"}
    if saved != response or saved.get("trace_stopped") is not True:
        return {"status": "report_unverified"}
    outcome = {"status": status, "file": filename, "pid": saved.get("pid")}
    allocation = saved.get("allocation")
    if status == "complete" and not isinstance(allocation, dict):
        return {"status": "report_unverified"}
    if isinstance(allocation, dict):
        ranked = allocation.get("integrations")
        if not isinstance(ranked, list):
            return {"status": "report_unverified"}
        if ranked:
            top = ranked[0]
            if (not isinstance(top, dict)
                    or not isinstance(top.get("integration"), str)
                    or not OWNER_NAME.fullmatch(top["integration"])
                    or type(top.get("bytes")) is not int or top["bytes"] < 0):
                return {"status": "report_unverified"}
            outcome["top_integration"] = top["integration"]
            outcome["top_bytes"] = top["bytes"]
    return outcome


class ProfileController:
    """One active profile and a restart-persistent attempt budget.

    Dispatch only on fresh growth/pressure observations with safe headroom.
    Core independently checks live headroom, owns the tracer and stops it.
    Never retries an uncertain service response immediately.
    """
    def __init__(self, url: str, token: str, detector: Detector, seconds: int = 15):
        if type(seconds) is not int or not 5 <= seconds <= 60:
            raise ValueError("profile duration must be 5-60 seconds")
        self.url = url
        self.token = token
        self.detector = detector
        self.seconds = seconds
        self.directory = detector.journal.directory
        self.path = self.directory / "profile-control.json"
        self.task: asyncio.Task | None = None
        self.attempts: list[float] = []
        self.last: dict = {"status": "idle"}
        self.high_attempted = False
        self.storage_ok = True
        self._restore()

    def _restore(self):
        try:
            if not self.path.exists():
                return
            with self.path.open("rb") as file:
                raw = file.read(16385)
            if len(raw) > 16384:
                raise ValueError("profile control exceeds limit")
            state = json.loads(raw)
            attempts = state["attempts"]
            last = state["last"]
            if (not isinstance(attempts, list) or len(attempts) > MAX_ATTEMPTS
                    or not all(type(t) in (float, int) and 0 <= t <= time.time() + 5 for t in attempts)
                    or not isinstance(last, dict)):
                raise ValueError("invalid profile control state")
            self.attempts = sorted(attempts)
            self.last = last
            self.high_attempted = state.get("high_attempted") is True
            if self.last.get("status") == "running":
                self.last = {**self.last, "status": "interrupted_response_unknown"}
        except (OSError, ValueError, KeyError, TypeError):
            self.storage_ok = False
            self.last = {"status": "control_state_unreadable"}
            LOG.warning("profile control state unavailable; automatic profiling disabled")

    def _persist(self):
        state = {"attempts": self.attempts, "last": self.last,
                 "high_attempted": self.high_attempted}
        raw = (json.dumps(state, separators=(",", ":"), allow_nan=False) + "\n").encode()
        if len(raw) > 16384:
            raise ValueError("profile control state exceeds limit")
        temp = self.directory / ".profile-control.tmp"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                     | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(raw)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp, self.path)
            fd = os.open(self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temp.unlink(missing_ok=True)

    def _record(self, record: dict):
        if self.detector.journal.active is not None:
            self.detector.record(record)

    def consider(self, sample: Sample, flags: set[str], now: float) -> bool:
        if not self.storage_ok or not sample.valid() or not -5 <= now - sample.ts <= STALE:
            return False
        if "absolute_core" not in flags:
            self.high_attempted = False
        reasons = set(flags) & GROWTH_FLAGS
        if "absolute_core" in flags and not self.high_attempted:
            reasons.add("absolute_core")
        if (not reasons or sample.core is None
                or sample.available is None or sample.available < MIN_AVAILABLE
                or sample.swap_free is None or sample.swap_free < MIN_SWAP_FREE
                or "core_unavailable" in flags or "headroom_unknown" in flags):
            return False
        if self.task is not None and not self.task.done():
            return False
        self.attempts = [t for t in self.attempts if now - t < RATE_WINDOW]
        if (len(self.attempts) >= MAX_ATTEMPTS
                or self.attempts and now - self.attempts[-1] < COOLDOWN):
            return False
        self.attempts.append(now)
        self.high_attempted = "absolute_core" in flags
        self.last = {"status": "running", "requested_at": now, "source_ts": sample.ts,
                     "reasons": sorted(reasons), "core_mib": sample.core,
                     "available_mib": sample.available, "swap_free_mib": sample.swap_free,
                     "seconds": self.seconds}
        try:
            # Reserve the attempt before the service call, including across restart.
            self._persist()
            self._record({"type": "profile_start", **self.last})
        except (OSError, ValueError, RuntimeError):
            self.storage_ok = False
            self.last = {"status": "control_storage_failed"}
            LOG.warning("profile control persistence failed; not dispatching")
            return False
        self.task = asyncio.create_task(self._run())
        LOG.warning("core profile requested: %s", ",".join(sorted(reasons)))
        return True

    async def _run(self):
        try:
            response = await invoke_profile(self.url, self.token, self.seconds)
            outcome = read_report(self.directory, response)
        except Exception as exc:
            # A lost response does not prove the native capture stopped.
            # Core's lock and the persisted cooldown prevent immediate overlap.
            outcome = {"status": "service_response_unknown", "error_type": type(exc).__name__}
        self.last = {**self.last, **outcome, "finished_at": time.time()}
        try:
            self._persist()
            self._record({"type": "profile_result", **self.last})
        except (OSError, ValueError, RuntimeError):
            self.storage_ok = False
            LOG.warning("profile result metadata persistence failed")
        LOG.warning("core profile outcome: %s", self.last["status"])
