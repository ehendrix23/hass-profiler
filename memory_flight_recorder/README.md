# Memory Flight Recorder (POC)

Protected HAOS telemetry collector. Version 0.1.1 consumes existing resource sensors and stores a 30-minute rolling history in memory. Abnormal growth opens a bounded diagnostic journal outside the HA configuration repository. See DOCS.md for prerequisites and limitations. Internal heap and stack profiling are not included in this release.
