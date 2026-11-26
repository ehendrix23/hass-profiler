"""Own native monitoring, profile dispatch, durable evidence, and thread lifecycle."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
import json
import logging
import os
from pathlib import Path
import re
import threading
import time

from .detector import Detector, Journal, Sample, STALE
from . import profiler
from .telemetry import LinuxSampler

LOG = logging.getLogger(__name__)
VERSION = "0.3.0"
GROWTH_FLAGS = {"rapid_60s", "rapid_5min", "rising_high_core"}
REPORT_NAME = re.compile(r"core-profile-[0-9]{1,24}\.json")
RATE_WINDOW = 300
MAX_ATTEMPTS = 3
CONTROL_CAP = 16384
INTERVAL = 15


@dataclass(frozen=True, slots=True)
class Settings:
    automatic_memory_profiling: bool = True
    profile_seconds: int = 15
    rapid_mib: int = 100
    rapid_5min_mib: int = 200
    rising_high_mib: int = 2300
    absolute_mib: int = 2500
    cooldown_seconds: int = 60

    def __post_init__(self):
        limits = (("profile_seconds", 5, 60), ("rapid_mib", 20, 1000),
                  ("rapid_5min_mib", 50, 2000), ("rising_high_mib", 500, 16000),
                  ("absolute_mib", 500, 16000), ("cooldown_seconds", 60, 3600))
        if type(self.automatic_memory_profiling) is not bool:
            raise ValueError("invalid automatic profiling option")
        for name, minimum, maximum in limits:
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError("invalid " + name)
        if self.absolute_mib < self.rising_high_mib:
            raise ValueError("absolute threshold must not be below rising-high threshold")


def atomic_json(directory: Path, name: str, value: dict, limit: int = CONTROL_CAP) -> None:
    raw = (json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n").encode()
    if len(raw) > limit:
        raise ValueError("diagnostic metadata exceeds storage limit")
    temp = directory / ("." + name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, directory / name)
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


def verified_report(directory: Path, result: dict) -> bool:
    name = result.get("file")
    if not isinstance(name, str) or not REPORT_NAME.fullmatch(name):
        return False
    try:
        fd = os.open(directory / name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as file:
            raw = file.read(profiler.MAX_REPORT_BYTES + 1)
        return (len(raw) <= profiler.MAX_REPORT_BYTES
                and json.loads(raw) == result and result.get("trace_stopped") is True)
    except (OSError, ValueError):
        return False


def completed(status: str) -> Future:
    future = Future()
    future.set_result({"status": status})
    return future


class Runtime:
    """One monitor thread, one profile worker; all journal/control writes serialized.

    All blocking calls are made by workers or by HA's executor, never by the HA
    event loop. stop() prevents further requests, cancels capture cooperatively
    and joins both workers before returning.
    """
    def __init__(self, directory: Path, settings: Settings):
        self.directory = directory
        self.settings = settings
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.stopped = threading.Event()
        self.sampler = LinuxSampler()
        self.detector = Detector(Journal(directory), rapid_mib=settings.rapid_mib,
                                 rapid_5min_mib=settings.rapid_5min_mib,
                                 rising_high_mib=settings.rising_high_mib,
                                 absolute_mib=settings.absolute_mib)
        self.detector.arm(time.time())
        self.attempts: list[float] = []
        self.last: dict = {"status": "idle"}
        self.high_attempted = False
        self.storage_ok = True
        self.active: Future | None = None
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hass-profiler-capture")
        self.monitor: threading.Thread | None = None
        self.last_status_write = 0.0
        self._restore()

    def _restore(self):
        path = self.directory / "profile-control.json"
        try:
            if not path.exists():
                return
            with path.open("rb") as file:
                raw = file.read(CONTROL_CAP + 1)
            if len(raw) > CONTROL_CAP:
                raise ValueError("control state too large")
            value = json.loads(raw)
            attempts, last = value["attempts"], value["last"]
            if (not isinstance(attempts, list) or len(attempts) > MAX_ATTEMPTS
                    or not all(type(t) in (float, int) and 0 <= t <= time.time() + 5 for t in attempts)
                    or not isinstance(last, dict)):
                raise ValueError("invalid control state")
            self.attempts = sorted(attempts)
            self.last = last
            self.high_attempted = value.get("high_attempted") is True
            if last.get("status") == "running":
                self.last = {**last, "status": "interrupted"}
        except (OSError, ValueError, TypeError, KeyError):
            self.storage_ok = False
            self.last = {"status": "control_state_unreadable"}
            LOG.warning("Profile control state unreadable; profiling disabled")

    def _persist(self):
        atomic_json(self.directory, "profile-control.json", {
            "attempts": self.attempts, "last": self.last, "high_attempted": self.high_attempted})

    def _status_locked(self) -> dict:
        sample = self.detector.ring[-1] if self.detector.ring else None
        age = time.time() - sample.ts if sample else None
        return {"version": VERSION, "ts": time.time(), "running": not self.stopping.is_set(),
                "pid": os.getpid(), "memory_metric": "core_process_rss_plus_swap_mib",
                "source_fresh": age is not None and -5 <= age <= STALE and not self.detector.gap,
                "source_age_s": round(age, 1) if age is not None else None,
                "sample": asdict(sample) if sample else None, "flags": sorted(self.detector.flags),
                "recording": self.detector.journal.active is not None,
                "buffer_samples": len(self.detector.ring),
                "profiling": "automatic" if self.settings.automatic_memory_profiling and self.storage_ok else "manual_or_disabled",
                "profile_last": dict(self.last), "storage_ok": self.storage_ok}

    def status(self) -> dict:
        with self.lock:
            return self._status_locked()

    def _write_status_locked(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_status_write < 60:
            return
        self.last_status_write = now
        try:
            atomic_json(self.directory, "status.json", self._status_locked())
        except (OSError, ValueError):
            LOG.warning("Health status write failed")

    def start(self):
        with self.lock:
            if self.monitor is not None or self.stopping.is_set():
                raise RuntimeError("runtime cannot be started twice")
            self.monitor = threading.Thread(target=self._monitor, name="hass-profiler-monitor", daemon=True)
            self.monitor.start()

    def _monitor(self):
        while not self.stopping.is_set():
            try:
                sample = self.sampler.read()
                self.observe(sample, time.time())
            except (OSError, ValueError, RuntimeError):
                with self.lock:
                    try:
                        self.detector.check_gap(time.time())
                        self._write_status_locked()
                    except (OSError, ValueError, RuntimeError):
                        self.storage_ok = False
                LOG.warning("Native monitoring sample or journal unavailable")
            self.stopping.wait(INTERVAL)

    def observe(self, sample: Sample, now: float) -> Future | None:
        with self.lock:
            if self.stopping.is_set():
                return None
            if not self.detector.feed(sample, now):
                return None
            flags = self.detector.flags
            if "absolute_core" not in flags:
                self.high_attempted = False
            reasons = flags & GROWTH_FLAGS
            if "absolute_core" in flags and not self.high_attempted:
                reasons.add("absolute_core")
            future = None
            if (self.settings.automatic_memory_profiling and reasons and self.storage_ok
                    and sample.available is not None and sample.available >= 1800
                    and sample.swap_free is not None and sample.swap_free >= 800
                    and sample.core is not None and "headroom_unknown" not in flags):
                future = self._request_locked(self.settings.profile_seconds, False, reasons, now, sample)
            self._write_status_locked()
            return future

    def request_manual(self, seconds: int, include_census: bool) -> Future:
        if type(seconds) is not int or not 5 <= seconds <= 60 or type(include_census) is not bool:
            return completed("invalid_duration")
        with self.lock:
            future = self._request_locked(seconds, include_census, {"manual_memory_profile"}, time.time(), None)
            return future if future is not None else completed(self._rejection(time.time()) or "not_dispatched")

    def _rejection(self, now: float) -> str | None:
        if self.stopping.is_set():
            return "stopped"
        if not self.storage_ok:
            return "storage_unavailable"
        if self.active is not None and not self.active.done():
            return "busy"
        self.attempts = [t for t in self.attempts if now - t < RATE_WINDOW]
        if (len(self.attempts) >= MAX_ATTEMPTS
                or self.attempts and now - self.attempts[-1] < self.settings.cooldown_seconds):
            return "cooldown"
        return None

    def _request_locked(self, seconds, include_census, reasons, now, sample) -> Future | None:
        if self._rejection(now):
            return None
        self.attempts.append(now)
        if "absolute_core" in self.detector.flags:
            self.high_attempted = True
        self.last = {"status": "running", "requested_at": now, "reasons": sorted(reasons),
                     "seconds": seconds, "sample": asdict(sample) if sample else None}
        try:
            self._persist()
            if self.detector.journal.active is None:
                self.detector.transition(now, reasons)
            self.detector.record({"type": "profile_start", **self.last})
            self._write_status_locked(force=True)
        except (OSError, ValueError, RuntimeError):
            self.storage_ok = False
            self.last = {"status": "control_storage_failed"}
            LOG.warning("Profile request not dispatched: persistence failed")
            return None
        LOG.info("Native memory profile requested: %s", ",".join(sorted(reasons)))
        self.active = self.worker.submit(self._execute, seconds, include_census)
        return self.active

    def _execute(self, seconds: int, include_census: bool) -> dict:
        try:
            result = profiler.capture_profile(seconds, include_census, self.directory, self.stopping)
            status = result.get("status")
            if result.get("file") and not verified_report(self.directory, result):
                result = {"status": "report_unverified"}
            elif status in {"complete", "partial_trace_budget"} and not result.get("file"):
                result = {"status": "report_unverified"}
        except (OSError, ValueError, RuntimeError, MemoryError) as exc:
            LOG.warning("Native profile failed: %s", type(exc).__name__)
            result = {"status": "capture_failed", "error_type": type(exc).__name__}
        outcome = {"status": result["status"], "finished_at": time.time()}
        if "file" in result:
            outcome.update(file=result["file"], pid=result.get("pid"))
        ranking = (result.get("allocation") or {}).get("integrations") or []
        if ranking:
            outcome.update(top_integration=ranking[0]["integration"], top_bytes=ranking[0]["bytes"])
        with self.lock:
            self.last = {**self.last, **outcome}
            try:
                self._persist()
                if self.detector.journal.active is not None:
                    self.detector.record({"type": "profile_result", **self.last})
                self._write_status_locked(force=True)
            except (OSError, ValueError, RuntimeError):
                self.storage_ok = False
                self.last = {**outcome, "status": "control_storage_failed"}
                LOG.warning("Profile result metadata persistence failed")
        LOG.info("Native memory profile outcome: %s", outcome["status"])
        return result

    def stop(self):
        with self.lock:
            first = not self.stopping.is_set()
            self.stopping.set()
            monitor = self.monitor
        if not first:
            self.stopped.wait()
            return
        try:
            if monitor is not None:
                monitor.join()
            self.worker.shutdown(wait=True, cancel_futures=False)
            with self.lock:
                self.detector.journal.close()
                self._write_status_locked(force=True)
        finally:
            self.stopped.set()
