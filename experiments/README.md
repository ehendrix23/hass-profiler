# Experimental process probe (not deployed)

This is a manual, standalone Linux diagnostic proof of concept. It verifies a caller-supplied PID and start time before and after collecting bounded numeric `/proc` data, including RSS, anonymous/file-backed memory, swap, threads, and counts of open file descriptors/sockets. It never writes live process memory, retrieves locals, or logs raw command lines. The experimental py-spy command wrapper samples nonblocking at 10 Hz for 5 seconds, checks host memory headroom and PID identity during the run, caps output at 256 KiB, kills the sampler at a 10-second deadline, and returns only result metadata. It discards raw stack files; safe stack-label persistence and automatic trigger integration are not implemented.

Run tests from this directory: `python -m unittest -v test_process_probe`. The process metadata test uses a real local PID; sampler tests use an executable stub, not real py-spy. The binary could not be downloaded into the test environment. Do not treat the wrapper as a validated production sampler.

Next gates: identify the actual Core process and verify host PID/start-time identity, use a separate experimental environment with no HA API token or Docker socket, confirm the minimum needed host PID and tracing permissions, validate real sampling of a disposable Python 3.14 process, then perform a short headroom-gated Core sampling POC only after explicit approval. This experiment is separate from the native integration; do not change production privileges. No automated stack profiling, privilege changes or heap attachment have been enabled by this experiment.

Stack sampling is activity evidence, not allocation provenance. Allocation attribution requires a separate controlled POC, e.g. tracemalloc or a supported native allocator tracer, including real memory/CPU overhead and capture of short-lived growth. Never automatically enumerate the entire heap when memory pressure is already critical.

Reference: https://github.com/benfred/py-spy (nonblocking stack sampling); https://contributors.home-assistant.io/apps/guides/security/ (host access and protection).
