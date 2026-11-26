"""Native, bounded in-Core allocation tracing for Pyscript 2.1+.

Install only this file in /config/pyscript. Native functions run in an HA
executor, not the event loop. No object reprs, arbitrary object methods,
raw traceback files, forced collection, or remote code execution.
"""
import gc
import json
import os
import pathlib
import re
import threading
import time
import tracemalloc

OUTPUT_DIR = pathlib.Path("/share/Logs/memory-watch")
CAPTURE_LOCK = threading.Lock()
PROFILE_VERSION = "0.2.0"
MIB = 1024 * 1024
MAX_TRACER_BYTES = 48 * MIB
MAX_TRACED_BYTES = 128 * MIB
MAX_CENSUS_SECONDS = 4.0
MAX_CENSUS_OBJECTS = 4000000
MAX_REPORT_BYTES = 65536
MAX_PROFILES = 64

@pyscript_compile
def integration_owner(filename):
    normalized = filename.replace("\\", "/")
    for marker, prefix in (("/homeassistant/components/", ""), ("/custom_components/", "custom:")):
        if marker in normalized:
            domain = normalized.split(marker, 1)[1].split("/", 1)[0]
            if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", domain):
                return prefix + domain
    return None

@pyscript_compile
def class_owner(module):
    if not isinstance(module, str):
        return None
    if module.startswith("homeassistant.components."):
        domain = module.split(".")[2]
        prefix = ""
    elif module.startswith("custom_components."):
        domain = module.split(".")[1]
        prefix = "custom:"
    else:
        return None
    if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", domain):
        return prefix + domain
    return None

@pyscript_compile
def memory_headroom():
    values = {}
    for path, wanted in (("/proc/meminfo", {"MemAvailable", "SwapFree"}),
                         ("/proc/self/status", {"VmRSS", "VmSwap", "RssAnon", "RssFile"})):
        with open(path, encoding="ascii") as file:
            for line in file:
                key, _, value = line.partition(":")
                if key in wanted:
                    values[key] = int(value.split()[0])
    return {"available_kib": values["MemAvailable"], "swap_free_kib": values["SwapFree"],
            "rss_kib": values["VmRSS"], "process_swap_kib": values.get("VmSwap"),
            "anonymous_kib": values.get("RssAnon"), "file_kib": values.get("RssFile")}

@pyscript_compile
def can_start(headroom):
    return (headroom["available_kib"] >= 1800 * 1024
            and headroom["swap_free_kib"] >= 800 * 1024
            and 0 < headroom["rss_kib"] < 2800 * 1024)

@pyscript_compile
def can_continue(headroom, baseline):
    return (headroom["available_kib"] >= 1200 * 1024
            and headroom["swap_free_kib"] >= 600 * 1024
            and headroom["rss_kib"] < 3000 * 1024
            and headroom["rss_kib"] - baseline["rss_kib"] <= 220 * 1024)

@pyscript_compile
def direct_instance_census():
    """Shallow size of GC-tracked instances of integration-defined classes only.

    Not transitive retained size; excludes untracked objects, builtins owned by
    an integration, allocator overhead and native buffers. No custom sizeof.
    """
    started = time.monotonic()
    if not can_start(memory_headroom()):
        return {"status": "skipped_headroom", "integrations": []}
    # The list allocation is not bounded by Python's GC API. A healthy-headroom
    # preflight is required. Do not do this on automatic pressure captures.
    objects = gc.get_objects()
    totals = {}
    total_attributed = 0
    examined = 0
    status = "complete"
    for obj in objects:
        if examined >= MAX_CENSUS_OBJECTS:
            status = "object_cap"
            break
        if examined % 8192 == 0:
            if time.monotonic() - started >= MAX_CENSUS_SECONDS:
                status = "time_cap"
                break
            if memory_headroom()["available_kib"] < 1500 * 1024:
                status = "headroom_cap"
                break
        examined += 1
        cls = type(obj)
        try:
            module = type.__getattribute__(cls, "__module__")
            owner = class_owner(module)
            if owner is None:
                continue
            size = object.__sizeof__(obj)
        except (AttributeError, TypeError, ValueError):
            continue
        row = totals.setdefault(owner, [0, 0])
        row[0] += size
        row[1] += 1
        total_attributed += size
    total_objects = len(objects)
    del objects
    ranking = [{"integration": name, "bytes": size, "count": count}
               for name, (size, count) in sorted(totals.items(), key=lambda pair: (-pair[1][0], pair[0]))[:30]]
    return {"status": status, "method": "gc_tracked_integration_class_shallow",
            "objects_examined": examined, "gc_tracked_objects": total_objects,
            "attributed_bytes": total_attributed, "integrations": ranking,
            "integration_count": len(totals), "elapsed_seconds": round(time.monotonic() - started, 3)}

@pyscript_compile
def summarize_traces(snapshot):
    """Attribute live traced allocations to nearest integration traceback frame."""
    totals = {}
    sites = {}
    total = attributed = count = attributed_count = 0
    for trace in snapshot.traces:
        total += trace.size
        count += 1
        for frame in reversed(trace.traceback):
            owner = integration_owner(frame.filename)
            if owner is None:
                continue
            attributed += trace.size
            attributed_count += 1
            row = totals.setdefault(owner, [0, 0])
            row[0] += trace.size
            row[1] += 1
            # Only the integration-relative file basename and line, not source,
            # arbitrary paths, user locals, or object content.
            name = frame.filename.replace("\\", "/").rsplit("/", 1)[-1]
            if re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", name):
                key = (owner, name, frame.lineno)
                if key in sites or len(sites) < 4096:
                    sites[key] = sites.get(key, 0) + trace.size
            break
    ranking = [{"integration": name, "bytes": size, "count": blocks}
               for name, (size, blocks) in sorted(totals.items(), key=lambda pair: (-pair[1][0], pair[0]))[:30]]
    top_sites = [{"integration": owner, "file": filename, "line": line, "bytes": size}
                 for (owner, filename, line), size in sorted(sites.items(), key=lambda pair: (-pair[1], pair[0]))[:30]]
    return {"method": "live_allocations_since_trace_start_nearest_integration_frame",
            "total_bytes": total, "total_blocks": count, "attributed_bytes": attributed,
            "attributed_blocks": attributed_count, "unattributed_bytes": total - attributed,
            "integrations": ranking, "integration_count": len(totals),
            "top_sites": top_sites}

@pyscript_compile
def persist_report(result):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    name = "core-profile-" + str(time.time_ns()) + ".json"
    result["file"] = name
    payload = (json.dumps(result, separators=(",", ":"), allow_nan=False) + "\n").encode()
    if len(payload) > MAX_REPORT_BYTES:
        raise ValueError("profile report exceeds cap")
    temp = OUTPUT_DIR / ("." + name + ".tmp")
    dest = OUTPUT_DIR / name
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, dest)
        fd = os.open(OUTPUT_DIR, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temp.unlink(missing_ok=True)
    # Only prune owned reports, never the recorder's incident journals.
    files = sorted((p for p in OUTPUT_DIR.glob("core-profile-*.json")
                    if not p.is_symlink() and p.is_file()), key=lambda p: p.stat().st_mtime)
    for path in files[:-MAX_PROFILES]:
        path.unlink()
    for path in files[-MAX_PROFILES:]:
        if path != dest and time.time() - path.stat().st_mtime > 30 * 86400:
            path.unlink(missing_ok=True)
    return result

@pyscript_executor
def capture_profile(seconds=60, include_census=True):
    """One real allocation capture inside HA Core; duration bounded to 5-60s."""
    if type(seconds) is not int or not 5 <= seconds <= 60 or type(include_census) is not bool:
        return {"status": "invalid_duration"}
    if not CAPTURE_LOCK.acquire(blocking=False):
        return {"status": "busy"}
    owns_trace = False
    try:
        if tracemalloc.is_tracing():
            return {"status": "skipped_existing_tracer"}
        before = memory_headroom()
        if not can_start(before):
            return {"status": "skipped_headroom", "before": before}
        census = direct_instance_census() if include_census else None
        before = memory_headroom()
        if not can_start(before):
            return {"status": "skipped_headroom_after_census", "before": before, "census": census}
        if tracemalloc.is_tracing():
            return {"status": "skipped_existing_tracer"}
        started_wall = time.time()
        started = time.monotonic()
        tracemalloc.start(3)
        owns_trace = True
        status = "complete"
        tracer_peak = 0
        sampled_peak = 0
        snapshot = None
        while True:
            now = time.monotonic()
            current, peak = tracemalloc.get_traced_memory()
            overhead = tracemalloc.get_tracemalloc_memory()
            tracer_peak = max(tracer_peak, overhead)
            sampled_peak = max(sampled_peak, peak)
            headroom = memory_headroom()
            if not can_continue(headroom, before):
                status = "aborted_headroom"
                break
            if overhead >= MAX_TRACER_BYTES or current >= MAX_TRACED_BYTES:
                status = "aborted_trace_budget"
                # If only the traced application bytes reached the cap, a
                # partial snapshot can still be safe: its copy cost depends
                # primarily on the tracer metadata, not application bytes.
                if overhead < MAX_TRACER_BYTES and headroom["available_kib"] >= 1800 * 1024:
                    snapshot = tracemalloc.take_snapshot()
                    status = "partial_trace_budget"
                break
            if now - started >= seconds:
                # Snapshot creation copies traces. A stricter headroom gate
                # ensures no heap-wide enumeration during critical pressure.
                if headroom["available_kib"] >= 1500 * 1024:
                    snapshot = tracemalloc.take_snapshot()
                else:
                    status = "skipped_snapshot_headroom"
                break
            time.sleep(min(0.25, max(0.0, seconds - (now - started))))
        elapsed = time.monotonic() - started
        # Stop before the heavier Python-level report aggregation.
        tracemalloc.stop()
        owns_trace = False
        allocation = summarize_traces(snapshot) if snapshot is not None else None
        del snapshot
        result = {"version": PROFILE_VERSION, "status": status, "pid": os.getpid(),
                  "started_at": started_wall, "finished_at": time.time(),
                  "requested_seconds": seconds, "elapsed_seconds": round(elapsed, 3),
                  "before": before, "after": memory_headroom(), "census": census,
                  "allocation": allocation, "tracer_overhead_peak_bytes": tracer_peak,
                  "traced_peak_bytes": sampled_peak, "trace_stopped": not tracemalloc.is_tracing()}
        return persist_report(result)
    finally:
        if owns_trace:
            tracemalloc.stop()
        CAPTURE_LOCK.release()

@service(supports_response="only")
def hass_profiler_memory(seconds=60, include_census=True):
    """Run a bounded memory profile inside Home Assistant Core."""
    return capture_profile(seconds, include_census)
