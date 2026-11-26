# hass-profiler

## Current release: standalone native integration (0.3.1)

Install **HASS Profiler** as a custom integration using HACS/Marketplace: add this repository (`https://github.com/ehendrix23/hass-profiler`) in the Integration category, download version 0.3.1, restart Core if required by the integration loader, then add HASS Profiler in Settings > Devices & Services. Configure automatic profiling, duration, growth thresholds and cooldown in its UI. The native integration needs neither the Supervisor App nor Pyscript, and reads Linux process/host measurements without Glances or REST polling.

The process memory metric is current Core RSS plus its swapped pages, in MiB, not container working set. CPU telemetry measures process CPU seconds over wall time (100% represents one logical CPU). Actual CPU stack profiling is not part of 0.3.1. The native worker runs inside Core, so it stops when Core exits; persisted reports survive. On HAOS it writes `/share/Logs/hass-profiler/` (otherwise `hass-profiler-diagnostics/` in the configuration directory).

**Actions:** `hass_profiler.profile_memory` accepts `seconds` (5–60) and `include_census` (false by default), returns the saved report, and shares the automatic profiler's safety and rate limits. `hass_profiler.status` returns current telemetry and the last result. Both require administrator privileges. Automatic captures do not enumerate GC objects. A single capture worker checks actual Linux memory headroom, stops tracing on budget/pressure limits, and only records a successful outcome after the saved report is read back. See `custom_components/hass_profiler/README.md` for the operational and evidence contract.

Version 0.3.1 defaults to 150 MiB/60 seconds and 300 MiB/5 minutes, with the existing high-memory safeguards unchanged. Housekeeping runs at startup and every 24 hours, even without an incident, and exposes its result in `hass_profiler.status` and `status.json`. Existing config-entry options are preserved. The native integration is the sole supported installation. Do not run competing automatic profilers.

## Updates

Commit tested native integration changes, keep the manifest and runtime/report versions aligned for releases, and publish a corresponding GitHub release for Marketplace. A source-only documentation or repository cleanup does not require a runtime version change. No tokens, credentials, live configuration, or real incident recordings belong in this repository.

## Experiments

The `experiments/` directory contains a separate, undeployed manual process-probe candidate for future investigation. It is not a runtime dependency or an alternative installation. Do not deploy privileged process profiling until target identity, permissions, and overhead are independently verified. See `experiments/README.md`.
