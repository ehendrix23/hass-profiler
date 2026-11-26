# hass-profiler

## Current release: standalone native integration (0.3.1)

Install **HASS Profiler** as a custom integration using HACS/Marketplace: add this repository (`https://github.com/ehendrix23/hass-profiler`) in the Integration category, download version 0.3.1, restart Core if required by the integration loader, then add HASS Profiler in Settings > Devices & Services. Configure automatic profiling, duration, growth thresholds and cooldown in its UI. The native integration needs neither the Supervisor App nor Pyscript, and reads Linux process/host measurements without Glances or REST polling.

The process memory metric is current Core RSS plus its swapped pages, in MiB, not the old container working set. CPU telemetry measures process CPU seconds over wall time (100% represents one logical CPU). Actual CPU stack profiling is not part of 0.3.0. The native worker runs inside Core, so it stops when Core exits; persisted reports survive. On HAOS it writes `/share/Logs/hass-profiler/` (otherwise `hass-profiler-diagnostics/` in the configuration directory).

**Actions:** `hass_profiler.profile_memory` accepts `seconds` (5–60) and `include_census` (false by default), returns the saved report, and shares the automatic profiler's safety and rate limits. `hass_profiler.status` returns current telemetry and the last result. Both require administrator privileges. Automatic captures do not enumerate GC objects. A single capture worker checks actual Linux memory headroom, stops tracing on budget/pressure limits, and only records a successful outcome after the saved report is read back. See `custom_components/hass_profiler/README.md` for the migration and evidence contract.

Version 0.3.1 defaults to 150 MiB/60 seconds and 300 MiB/5 minutes, with the existing high-memory safeguards unchanged. Housekeeping runs at startup and every 24 hours, even without an incident, and exposes its result in `hass_profiler.status` and `status.json`. Existing config-entry options are preserved. The older 0.2.0 App and Pyscript sources below are retained for historical rollback only, not dependencies or the recommended installation method. Do not run competing automatic profilers.

---

Home Assistant OS App repository for a lightweight memory flight recorder and experimental profiling tools.

[![Add App repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fehendrix23%2Fhass-profiler)

## Installable release

Memory Flight Recorder 0.2.0 is a protected App that uses existing 15-second resource telemetry to trigger actual Python allocation tracing inside Core through its Pyscript companion. The current implementation expects the entity IDs documented in `memory_flight_recorder/ha_feed.py`; it is not yet a generic out-of-the-box profiler for arbitrary HA installations.

Install `core_companion/hass_profiler.py` into `/config/pyscript/` on a working Pyscript installation with `allow_all_imports` enabled. Verify the `pyscript.hass_profiler_memory` action is present before enabling automatic profiling. The companion runs in Core's native executor, owns a bounded tracemalloc session, and writes sanitized allocation rankings under `/share/Logs/memory-watch/`. The App invokes that action only when the memory detector qualifies and headroom is sufficient. It verifies the persisted report before claiming a successful profile. Manual profiling is available through the same action (5–60 seconds; `include_census` optionally measures existing GC-tracked integration-defined objects).

Install using Settings > Apps > Install app > Repositories. Add the repository URL. Leave Protection mode on. For a migration from a locally installed recorder, install the repository-sourced App without starting it, stop the local App and turn off its boot/watchdog, then start and validate the new App before enabling boot/watchdog. Both versions use the same share directory; never run them concurrently. Retain the stopped local App until the new version is accepted. Roll back by stopping/disabling the new App before starting the local App again. Do not delete the diagnostic directory.

## Updates

Commit tested source changes and bump `memory_flight_recorder/config.yaml`, the health-status version in `ha_feed.py`, and the companion version as applicable. Update CHANGELOG.md. The Supervisor builds from Dockerfile; no container registry is needed. The companion is intentionally installed separately: the protected App has no write access to Core's configuration. Update and verify it before an App release that depends on a changed companion contract. Keep automatic updates disabled during development. No tokens, credentials, live configuration, or real incident recordings belong in this repository.

## Experiments

The `experiments/` directory is not an installable App. It contains a separate, manual process-probe candidate; this does not modify the stable collector. Do not deploy privileged process profiling until target identity, permissions, and overhead are independently verified. See `experiments/README.md`.
