## Native HASS Profiler

Version 0.3.1 executes its own bounded telemetry and allocation capture inside Home Assistant Core. It does not use Pyscript, an App, a remote socket, privileged process access, additional REST queries or HA sensor dependencies. It requires Linux procfs and Home Assistant 2026.11 or newer. The two internal threads are owned by the config entry; unload and shutdown stop scheduling, cooperatively cancel any owned capture and join both threads before cleanup.

### Migration from the App

1. Install the native integration through Marketplace with automatic profiling disabled. Keep the legacy installation available for rollback.
2. Stop the old App after any in-flight capture has completed. Verify the native status and a real manual memory capture. Never run competing tracemalloc sessions.
3. Enable automatic profiling in integration options. Verify a real growth-triggered profile, its saved report, and the incident linkage before retiring the App and its Pyscript companion.
4. The legacy reports in `/share/Logs/memory-watch/` remain separate from native reports under `/share/Logs/hass-profiler/`.

### Measurement and safeguards

The native 15-second sampler reads `/proc/self/status`, `/proc/meminfo` and optional process I/O counters. Trigger memory is RSS plus process swap, not container working set; RSS, swap, anonymous/file-backed RSS and CPU are reported separately. CPU attribution is not included in this release. Default memory growth thresholds are 150 MiB in 60 seconds and 300 MiB in 5 minutes; the 2300 MiB rising and 2500 MiB absolute thresholds are unchanged. Existing config-entry options are preserved. One capture at a time, 60 seconds minimum cooldown (configurable upwards), maximum three requests in five minutes, persisted before dispatch. Automatic capture requires at least 1800 MiB available memory and 800 MiB free swap; the capture engine independently checks direct headroom and RSS throughout. No forced heap scan at a pressure trigger. Native tracing may abort on memory pressure or its tracing budget; it never takes ownership of an already-running tracer. Unload cooperatively cancels a capture. File-system failures disable dispatch rather than creating uncontrolled evidence.

Allocation attribution groups newly allocated Python memory still live at capture end by the nearest matching integration traceback frame. It is not total existing retained memory, total allocated volume or native RSS. The optional manual shallow GC census measures only directly attributed object sizes, not ownership of the entire heap. Paths are reduced to integration and source basename, and no object values or locals are stored. Native process and tracing metrics are reported separately. The profiler itself can appear in the rankings.

Output: `status.json`, `profile-control.json`, `incident-*.jsonl` (bounded 30-minute pre-trigger buffer, 15 minutes after flags clear, 100 MiB total/30-day retention), and `core-profile-*.json` (64 KiB each, 64 files, 4 MiB total, 30-day retention). Maintenance runs at startup and every 24 hours independently of incident activity. It preserves the active journal, defers while a capture worker is active, removes orphaned report temporary files when idle, and only deletes matching profiler-owned regular files. Symlinks and unrelated files are left alone. The housekeeping result, timestamp, and storage counts appear in status. Failures disable new profiling until reload and retry housekeeping after 60 seconds. Historical evidence is not uploaded to GitHub. No automatic remediation or Core restart is initiated.

### Tests

Run native tests against the actual supported HA release: `uv run --no-project --python 3.14 --with 'homeassistant==2026.11.3' python -m unittest discover -s tests -v`. The native runtime tests use procfs fixtures where required on macOS, real CPython tracemalloc, actual file writes, and the HA lifecycle test exercises the real config-entry and service APIs. Production acceptance still requires live Linux HAOS validation.
