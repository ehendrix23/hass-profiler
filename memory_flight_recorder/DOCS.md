# Memory Flight Recorder

Requires HAOS amd64 and the existing 15-second Glances telemetry entity IDs listed in `ha_feed.py`. System Monitor available memory is an independent measurement. Verify these entities exist before starting. A single filtered WebSocket subscription uses the Supervisor's HA API proxy; its administrator-capable token is used only for authentication. No additional Glances or Recorder polling or HA service calls. No exposed network ports, Docker socket, host PID access or full access. Keep Protection mode on.

The recorder settles sequential updates for two seconds and labels unchanged carried-forward values. This does not guarantee that each component value is from precisely the same source cycle. The independent available-memory reading is rejected if too old. Check `/share/Logs/memory-watch/status.json` for source freshness, connection, active recording and buffer count. The health report is written approximately every 60 seconds.

The rolling buffer is capped at 128 samples/30 minutes. An incident records while abnormal flags persist and for 15 minutes afterward. Journals are fsynced per record, rotate at 1 MiB, and have a 100 MiB total incident cap, 30-day retention and 256-file limit. The App never restarts Core or performs automated remediation. Previous evidence remains under `/share/Logs/memory-watch/`; it may be included in system backups. Do not run two recorder instances with the same output directory.

## Tests

From this directory: `python -m pip install websockets==16.0`, then `python -m unittest -v test_flight_recorder test_ha_feed`. Replay fixtures are synthetic. The currently installable App has no automatic profiling feature. Additional process diagnostics require separate development, permissions and validation.
