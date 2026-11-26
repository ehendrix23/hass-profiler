# hass-profiler

Home Assistant OS App repository for a lightweight memory flight recorder and experimental profiling tools.

[![Add App repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fehendrix23%2Fhass-profiler)

## Installable release

Memory Flight Recorder 0.1.1 is a protected, telemetry-only App. It uses a filtered Home Assistant WebSocket subscription and the existing 15-second Glances snapshot timestamp. It does not perform heap scans, issue service calls, or attach to Core. The current implementation expects the entity IDs documented in `memory_flight_recorder/ha_feed.py`; it is not yet a generic out-of-the-box profiler for arbitrary HA installations.

Install using Settings > Apps > Install app > Repositories. Add the repository URL. Leave Protection mode on. For a migration from a locally installed recorder, install the repository-sourced App without starting it, stop the local App and turn off its boot/watchdog, then start and validate the new App before enabling boot/watchdog. Both versions use the same share directory; never run them concurrently. Retain the stopped local App until the new version is accepted. Roll back by stopping/disabling the new App before starting the local App again. Do not delete the diagnostic directory.

## Updates

Commit tested source changes and bump `memory_flight_recorder/config.yaml` and the health-status version in `ha_feed.py` together. Update CHANGELOG.md. The Supervisor builds from Dockerfile; no container registry is needed. Keep automatic updates disabled during development. No tokens, credentials, live configuration, or real incident recordings belong in this repository.

## Experiments

The `experiments/` directory is not an installable App. It contains a separate, manual process-probe candidate; this does not modify the stable collector. Do not deploy privileged process profiling until target identity, permissions, and overhead are independently verified. See `experiments/README.md`.
