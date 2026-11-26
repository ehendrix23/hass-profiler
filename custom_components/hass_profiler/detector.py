"""Bounded incident detection and durable journal for native Core metrics."""
from __future__ import annotations
from collections import deque
from dataclasses import asdict, dataclass, fields
import json
import math
import os
import re
from pathlib import Path
import time
from typing import Callable

INTERVAL = 15
STALE = 45
PRE = 1800
POST = 900
MAX_ROWS = 128
MAX_FILE = 1024 * 1024
MAX_TOTAL = 100 * 1024 * 1024
RETENTION = 30 * 86400
MAX_FILES = 256
INCIDENT_NAME = re.compile(r"incident-[0-9]+-[0-9]+-[0-9]+\.jsonl")

@dataclass(frozen=True, slots=True)
class Sample:
    ts: float
    core: float | None
    available: float | None = None
    raw_free: float | None = None
    swap: float | None = None
    swap_free: float | None = None
    host_percent: float | None = None
    core_cpu: float | None = None
    read_bps: float | None = None
    write_bps: float | None = None
    process_rss: float | None = None
    process_swap: float | None = None
    process_anonymous: float | None = None
    process_file: float | None = None

    def valid(self):
        return (isinstance(self.ts, (int,float)) and math.isfinite(self.ts)
                and all(v is None or (isinstance(v,(int,float)) and not isinstance(v,bool)
                                     and math.isfinite(v) and v >= 0)
                        for f in fields(self) if f.name != 'ts' for v in [getattr(self,f.name)]))

class Journal:
    """Small append-only records; fsync at each incident record. Own files only."""
    def __init__(self, directory: Path, max_total=MAX_TOTAL, max_file=MAX_FILE, retention=RETENTION):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.max_total = max_total
        self.max_file = min(max_file,max_total)
        self.retention = retention
        self.active: Path | None = None
        self._counter = 0
        self.prune(time.time())

    def files(self):
        return sorted((p for p in self.directory.glob('incident-*.jsonl')
                       if INCIDENT_NAME.fullmatch(p.name) and not p.is_symlink() and p.is_file()), key=lambda p:p.stat().st_mtime)

    def prune(self, now, reserve=0):
        paths = self.files()
        sizes = {p:p.stat().st_size for p in paths}
        total = sum(sizes.values())
        count = len(paths)
        for p in paths:
            if p == self.active:
                continue
            if p.stat().st_mtime < now-self.retention or total+reserve > self.max_total or count>MAX_FILES:
                p.unlink(); total-=sizes[p]; count-=1
        return total + reserve <= self.max_total

    def open(self):
        self.close()
        self._counter += 1
        name = f'incident-{time.time_ns()}-{os.getpid()}-{self._counter}.jsonl'
        p = self.directory/name
        fd = os.open(p, os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
        os.close(fd)
        directory_fd = os.open(self.directory,os.O_RDONLY|getattr(os,"O_DIRECTORY",0))
        try: os.fsync(directory_fd)
        finally: os.close(directory_fd)
        self.active = p
        return p

    def append(self, record):
        line = (json.dumps(record,separators=(',',':'),allow_nan=False)+'\n').encode()
        if len(line) > 16384 or self.active is None:
            return False
        if self.active.stat().st_size + len(line) > self.max_file:
            return False
        if not self.prune(time.time(),len(line)):
            return False
        fd = os.open(self.active, os.O_WRONLY|os.O_APPEND|getattr(os,'O_NOFOLLOW',0))
        try:
            with os.fdopen(fd,'ab') as f:
                f.write(line); f.flush(); os.fsync(f.fileno())
        except OSError:
            raise
        return True

    def close(self):
        self.active = None

class Detector:
    """Bounded state machine. Distinguishes new growth from existing high memory."""
    def __init__(self, journal: Journal, notify: Callable[[dict],None] | None = None, *, rapid_mib=150, rapid_5min_mib=300, rising_high_mib=2300, absolute_mib=2500):
        self.journal = journal
        self.rapid_mib = rapid_mib
        self.rapid_5min_mib = rapid_5min_mib
        self.rising_high_mib = rising_high_mib
        self.absolute_mib = absolute_mib
        self.notify = notify or (lambda _:None)
        self.ring = deque(maxlen=MAX_ROWS)
        self.last_ts = None
        self.last_seen_wall = None
        self.flags = set()
        self.deadline = None
        self.gap = False
        self.events = []  # bounded diagnostic metadata, never raw logs

    def record(self, record):
        if not self.journal.append(record):
            # Rotate at the hard cap, retaining one active capture only.
            self.journal.open()
            if not self.journal.append(record):
                raise RuntimeError('diagnostic storage limit too small')

    def transition(self, ts, reasons):
        if self.journal.active is None:
            self.journal.open()
            self.record({'type':'start','ts':ts,'reasons':sorted(reasons)})
            for s in self.ring:
                if s.ts < ts: self.record({'type':'pre','sample':asdict(s)})
            kind='start'
        else:
            self.record({'type':'escalate','ts':ts,'reasons':sorted(reasons)})
            kind='escalate'
        self.deadline = ts+POST
        event={'type':kind,'ts':ts,'reasons':sorted(reasons)}
        self.events=(self.events+[event])[-64:]
        self.notify(event)

    def arm(self, now):
        if self.last_seen_wall is None: self.last_seen_wall=now

    def check_gap(self, now):
        if self.last_seen_wall is not None and now-self.last_seen_wall > STALE and not self.gap:
            self.gap = True
            self.transition(now, {'stale_telemetry'})
            self.ring.clear(); self.flags.clear()
            return True
        return False

    def feed(self, s:Sample, now:float):
        if not s.valid() or not -5 <= now-s.ts <= STALE:
            return False
        if self.last_ts is not None and s.ts <= self.last_ts:
            return False
        self.check_gap(now)
        if self.last_ts is not None and s.ts-self.last_ts > STALE:
            self.ring.clear(); self.flags.clear()
        if self.gap and self.journal.active:
            self.record({'type':'telemetry_resumed','ts':s.ts})
        self.gap=False
        self.last_ts=s.ts
        self.last_seen_wall=now
        while self.ring and s.ts-self.ring[0].ts > PRE:
            self.ring.popleft()
        prev = list(self.ring)
        self.ring.append(s)
        def rise(name, sec):
            v=getattr(s,name)
            past=[getattr(p,name) for p in prev if 0 < s.ts-p.ts <= sec and getattr(p,name) is not None]
            return None if v is None or not past else v-min(past)
        d60=rise('core',60); d300=rise('core',300); ds=rise('swap',300)
        new=set()
        if s.core is None: new.add('core_unavailable')
        else:
            if s.core>self.absolute_mib: new.add('absolute_core')
            if s.core>self.rising_high_mib and d60 is not None and d60>=20: new.add('rising_high_core')
            if d60 is not None and d60>=self.rapid_mib: new.add('rapid_60s')
            if d300 is not None and d300>=self.rapid_5min_mib: new.add('rapid_5min')
        if s.available is not None and s.available<500: new.add('critical_available')
        if s.swap_free is not None and s.swap_free<200: new.add('critical_swap_free')
        if s.swap is not None:
            if s.swap>1400: new.add('absolute_swap')
            if s.swap>1200 and ds is not None and ds>=20: new.add('rising_swap')
        if s.available is None or s.swap is None: new.add('headroom_unknown')
        # New reasons, including escalations, extend the same capture.
        entered=new-self.flags
        self.flags=new
        if entered:
            self.transition(s.ts,entered)
        if self.journal.active:
            self.record({'type':'sample','sample':asdict(s),'flags':sorted(new)})
            # Maintain a single incident while any abnormal condition persists.
            # The post-trigger window begins after the last abnormal sample.
            if new:
                self.deadline = s.ts + POST
            if not new and self.deadline is not None and s.ts>=self.deadline:
                self.record({'type':'end','ts':s.ts,'reason':'window_complete'})
                self.journal.close(); self.deadline=None
        return True
