"""Read inexpensive Linux metrics for this process; no HA entities or other Apps."""
from __future__ import annotations

from pathlib import Path
import time

from .detector import Sample

PROC = Path("/proc")
MEM_KEYS = {"MemTotal", "MemAvailable", "MemFree", "SwapTotal", "SwapFree"}
PROCESS_KEYS = {"VmRSS", "VmSwap", "RssAnon", "RssFile"}


def read_numbers(path: Path, keys: set[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    with path.open(encoding="ascii") as file:
        for line in file:
            key, _, value = line.partition(":")
            if key in keys:
                number = int(value.split()[0])
                if number < 0:
                    raise ValueError("negative procfs measurement")
                result[key] = number
    return result


def native_metrics(root: Path = PROC) -> tuple[dict[str, int], dict[str, int]]:
    host = read_numbers(root / "meminfo", MEM_KEYS)
    process = read_numbers(root / "self" / "status", PROCESS_KEYS)
    if (not MEM_KEYS.issubset(host) or not {"VmRSS", "VmSwap"}.issubset(process)
            or host["MemTotal"] <= 0 or host["MemAvailable"] > host["MemTotal"]
            or host["SwapFree"] > host["SwapTotal"]):
        raise ValueError("incomplete or invalid Linux memory measurements")
    return host, process


def memory_headroom(root: Path = PROC) -> dict[str, int | None]:
    host, process = native_metrics(root)
    return {"available_kib": host["MemAvailable"], "swap_free_kib": host["SwapFree"],
            "rss_kib": process["VmRSS"], "process_swap_kib": process["VmSwap"],
            "anonymous_kib": process.get("RssAnon"), "file_kib": process.get("RssFile")}


class LinuxSampler:
    """The monitored memory is this process's RSS plus anonymous swapped pages.

    This is deliberately not container working-set memory. The PID is always
    self, and the CPU rate is aggregate process CPU seconds / elapsed seconds.
    """
    def __init__(self, root: Path = PROC):
        self.root = root
        self.previous: tuple[float, float, dict[str, int]] | None = None

    def read(self) -> Sample:
        host, process = native_metrics(self.root)
        try:
            io = read_numbers(self.root / "self" / "io", {"read_bytes", "write_bytes"})
        except (OSError, ValueError):
            io = {}
        now = time.monotonic()
        cpu = time.process_time()
        cpu_percent = read_bps = write_bps = None
        if self.previous is not None:
            prev_time, prev_cpu, prev_io = self.previous
            elapsed = now - prev_time
            if 0 < elapsed <= 45 and cpu >= prev_cpu:
                cpu_percent = round(100 * (cpu - prev_cpu) / elapsed, 2)
                if "read_bytes" in io and "read_bytes" in prev_io and io["read_bytes"] >= prev_io["read_bytes"]:
                    read_bps = (io["read_bytes"] - prev_io["read_bytes"]) / elapsed
                if "write_bytes" in io and "write_bytes" in prev_io and io["write_bytes"] >= prev_io["write_bytes"]:
                    write_bps = (io["write_bytes"] - prev_io["write_bytes"]) / elapsed
        self.previous = now, cpu, io
        def mib(key: str) -> float | None:
            return process[key] / 1024 if key in process else None
        return Sample(
            ts=time.time(), core=(process["VmRSS"] + process["VmSwap"]) / 1024,
            available=host["MemAvailable"] / 1024, raw_free=host["MemFree"] / 1024,
            swap=(host["SwapTotal"] - host["SwapFree"]) / 1024,
            swap_free=host["SwapFree"] / 1024,
            host_percent=100 * (host["MemTotal"] - host["MemAvailable"]) / host["MemTotal"],
            core_cpu=cpu_percent, read_bps=read_bps, write_bps=write_bps,
            process_rss=mib("VmRSS"), process_swap=mib("VmSwap"),
            process_anonymous=mib("RssAnon"), process_file=mib("RssFile"),
        )
