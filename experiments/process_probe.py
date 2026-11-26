"""Manual-only process diagnostic POC. Not imported by the production App.

Caller must establish target identity independently. No automatic process search,
service calls, full heap scans, local variable dumps, or in-process injection.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import resource
import subprocess
import tempfile
import time

LIMIT = 256 * 1024
DURATION = 5
RATE = 10
TIMEOUT = 10
MIN_AVAILABLE_KIB = 1500 * 1024
MIN_SWAP_FREE_KIB = 700 * 1024
MAX_RSS_KIB = 2500 * 1024


def start_ticks(pid: int, proc: Path = Path('/proc')) -> int:
    if type(pid) is not int or pid <= 1:
        raise ValueError('invalid target PID')
    with (proc / str(pid) / 'stat').open() as f:
        raw = f.read(8192)
    return int(raw[raw.rfind(')') + 2:].split()[19])


def read_numbers(path: Path, allowed: set[str]) -> dict[str, int]:
    result = {}
    with path.open() as f:
        for line in f.read(65536).splitlines():
            key, _, value = line.partition(':')
            if key in allowed:
                number = int(value.split()[0])
                if number < 0:
                    raise ValueError('negative measurement')
                result[key] = number
    return result


def capture_metadata(pid: int, expected: int, proc: Path = Path('/proc')) -> dict:
    if start_ticks(pid, proc) != expected:
        raise ValueError('target identity changed')
    folder = proc / str(pid)
    target = read_numbers(folder / 'status', {'VmRSS', 'RssAnon', 'RssFile', 'RssShmem', 'VmSwap', 'Threads'})
    if 'VmRSS' not in target:
        raise ValueError('target memory unavailable')
    count = sockets = 0
    with os.scandir(folder / 'fd') as items:
        for item in items:
            count += 1
            try:
                if os.readlink(item.path).startswith('socket:'):
                    sockets += 1
            except FileNotFoundError:
                pass
            if count >= 8192:
                break
    host = read_numbers(proc / 'meminfo', {'MemAvailable', 'MemFree', 'SwapFree', 'SwapTotal'})
    if start_ticks(pid, proc) != expected:
        raise ValueError('target identity changed')
    return {'pid': pid, 'start_ticks': expected, 'target': target, 'host': host,
            'fd_count': count, 'socket_count': sockets, 'fd_count_capped': count >= 8192}


def pressure_snapshot(pid: int, expected: int, proc: Path) -> dict:
    # Cheap repeat check during sampling: do not enumerate FDs each time.
    if start_ticks(pid, proc) != expected:
        raise ValueError('target identity changed')
    target = read_numbers(proc / str(pid) / 'status', {'VmRSS'})
    host = read_numbers(proc / 'meminfo', {'MemAvailable', 'SwapFree'})
    if start_ticks(pid, proc) != expected:
        raise ValueError('target identity changed')
    return {'target': target, 'host': host}


def safe_to_sample(snapshot: dict) -> bool:
    host, target = snapshot['host'], snapshot['target']
    return (host.get('MemAvailable', -1) >= MIN_AVAILABLE_KIB and
            host.get('SwapFree', -1) >= MIN_SWAP_FREE_KIB and
            0 < target.get('VmRSS', MAX_RSS_KIB) < MAX_RSS_KIB)


def bounded_stack_sample(pid: int, expected: int, binary: str = '/usr/local/bin/py-spy',
                         proc: Path = Path('/proc')) -> dict:
    """One bounded nonblocking session, no locals, no children, no persistent raw output.

    Experimental standalone function; preexec_fn is used only in the single-threaded
    CLI/test process, not the asyncio collector. Raw stack labels are withheld pending
    path and function-name privacy review. A successful capture here verifies sampling
    mechanics, not allocation attribution.
    """
    before = capture_metadata(pid, expected, proc)
    if not safe_to_sample(before):
        return {'status': 'skipped_headroom', 'before': before}
    with tempfile.TemporaryDirectory(prefix='stack-probe-') as work:
        output = Path(work) / 'sample.txt'
        def child_limits():
            resource.setrlimit(resource.RLIMIT_FSIZE, (LIMIT, LIMIT))
        argv = [binary, 'record', '--pid', str(pid), '--nonblocking', '--duration', str(DURATION),
                '--rate', str(RATE), '--format', 'raw', '--output', str(output)]
        if start_ticks(pid, proc) != expected:
            raise ValueError('target identity changed')
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, preexec_fn=child_limits,
                             start_new_session=True, env={'PATH': '/usr/local/bin:/usr/bin:/bin'})
        began = time.monotonic()
        status = 'complete'
        try:
            while p.poll() is None:
                if time.monotonic()-began >= TIMEOUT:
                    status = 'timeout'; break
                try:
                    if not safe_to_sample(pressure_snapshot(pid, expected, proc)):
                        status = 'aborted_headroom'; break
                except (OSError, ValueError):
                    status = 'aborted_identity_or_access'; break
                time.sleep(.25)
        finally:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=2)
        size = output.stat().st_size if output.is_file() else 0
        if status == 'complete' and (p.returncode != 0 or not 0 < size < LIMIT):
            status = 'sampler_failed_or_empty'
        after = None
        try:
            after = capture_metadata(pid, expected, proc)
        except (OSError, ValueError):
            status = 'target_lost'
        return {'status': status, 'bytes': size, 'returncode': p.returncode,
                'elapsed_seconds': round(time.monotonic()-began, 3), 'before': before, 'after': after}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--start-ticks', type=int, required=True)
    parser.add_argument('--sample', action='store_true', help='explicitly authorize one stack sampling session')
    parser.add_argument('--binary', default='/usr/local/bin/py-spy')
    args = parser.parse_args()
    result = (bounded_stack_sample(args.pid, args.start_ticks, args.binary) if args.sample
              else capture_metadata(args.pid, args.start_ticks))
    print(json.dumps(result, separators=(',', ':'), allow_nan=False))


if __name__ == '__main__':
    main()
