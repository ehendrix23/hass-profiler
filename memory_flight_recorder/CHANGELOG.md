# Changelog

## 0.2.0
- Add an in-Core Pyscript companion providing bounded tracemalloc allocation profiles and optional manual shallow GC census.
- Dispatch actual profiling on qualified memory-growth/absolute-high flags with separate WebSocket request, persisted attempt budget, native headroom checks and one active capture.
- Verify Core-written reports before recording successful attribution; retain report filenames and top integration in incident/health metadata.
- Add configurable automatic profiling and duration (5–60 seconds, default 15). Protection mode remains enabled.

## 0.1.1
- Extend active incidents until 15 minutes after abnormal flags clear.
- Add bounded atomic health-status report and improved reconnect backoff.
- Git repository packaging; no runtime behavior changes from the local 0.1.1 release.

## 0.1.0
- Initial protected WebSocket telemetry collector and diagnostic journal.
