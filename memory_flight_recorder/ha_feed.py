"""Filtered Home Assistant WebSocket telemetry adapter. No HA writes/services.

Uses the existing Glances snapshot timestamp. Unchanged state values are
carried forward explicitly; independent System Monitor available RAM is
never treated as synchronized to Glances. No automatic profiler is enabled.
"""
from __future__ import annotations
import asyncio
import datetime as dt
import json
import logging
import math
import os
from pathlib import Path
import time
from flight_recorder import Detector,Journal,Sample,STALE

LOG=logging.getLogger('memory_watch')
STAMP='sensor.haos_glances_snapshot_timestamp'
FIELDS={
    'core':'sensor.ha_component_home_assistant_core_memory',
    'available':'sensor.system_monitor_memory_free',
    'raw_free':'sensor.haos_glances_memory_free',
    'swap':'sensor.haos_glances_swap_used',
    'swap_free':'sensor.haos_glances_swap_free',
    'host_percent':'sensor.haos_glances_memory_percent',
    'core_cpu':'sensor.ha_component_home_assistant_core_cpu',
    'read_bps':'sensor.ha_component_home_assistant_core_disk_io_read',
    'write_bps':'sensor.ha_component_home_assistant_core_disk_io_write',
    'nodered':'sensor.ha_component_node_red_memory',
    'supervisor':'sensor.ha_component_supervisor_memory',
    'glances':'sensor.ha_component_glances_memory',
    'esphome':'sensor.ha_component_esphome_memory',
    'matter':'sensor.ha_component_matter_server_memory',
    'mosquitto':'sensor.ha_component_mosquitto_memory',
    'zwave':'sensor.ha_component_z_wave_js_ui_memory',
}
SUBSCRIPTION=[STAMP,*FIELDS.values()]
SETTLE=2.0

class EntityFeed:
    def __init__(self):
        self.states={}
        self.pending_stamp=None
        self.due=None

    def update(self,event,now_mono):
        old=self.states.get(STAMP,{}).get('s')
        for entity,item in event.get('a',{}).items():
            if entity in SUBSCRIPTION and isinstance(item,dict):
                self.states[entity]={k:item[k] for k in ('s','lc','lu') if k in item}
        for entity,change in event.get('c',{}).items():
            if entity not in SUBSCRIPTION or entity not in self.states: continue
            diff=change.get('+',{})
            state=self.states[entity]
            if 'lc' in diff and 'lu' not in diff: state.pop('lu',None)
            state.update({k:diff[k] for k in ('s','lc','lu') if k in diff})
        for entity in event.get('r',[]):
            self.states.pop(entity,None)
        value=self.states.get(STAMP,{}).get('s')
        if value!=old:
            try:
                parsed=dt.datetime.fromisoformat(value)
                if parsed.tzinfo is None: raise ValueError('missing zone')
                stamp=parsed.timestamp()
            except (TypeError,ValueError,OverflowError):
                self.pending_stamp=None;self.due=None;return
            self.pending_stamp=stamp; self.due=now_mono+SETTLE

    def pop(self, now_mono):
        if self.due is None or now_mono<self.due: return None
        stamp=self.pending_stamp
        self.due=None; self.pending_stamp=None
        vals={};carried=[]
        for field,entity in FIELDS.items():
            s=self.states.get(entity,{})
            try:
                v=float(s.get('s'))
                if not math.isfinite(v) or v<0: raise ValueError
            except (ValueError,TypeError): v=None
            update=s.get('lu',s.get('lc'))
            if not isinstance(update,(int,float)) or not math.isfinite(update):
                v=None
            elif field=='available':
                # Independent System Monitor cadence; never silently use old data.
                if abs(stamp-update)>STALE: v=None
            elif update<stamp-2:
                # subscribe_entities emits changes, not unchanged reports.
                # Carry-forward must remain distinguishable from a fresh report.
                if v is not None: carried.append(field)
            elif update>stamp+SETTLE+2:
                # Obvious cross-cycle / clock mismatch; fail closed.
                v=None
            vals[field]=v
        return Sample(stamp,**vals,carried_fields=tuple(sorted(carried)))

class Heartbeat:
    """Atomic, bounded local health report. Never polls HA or stores raw events."""
    def __init__(self, directory, interval=60):
        self.directory=Path(directory)
        self.interval=interval
        self.last_mono=None
    def publish(self, detector, connected, now, monotonic, force=False):
        if not force and self.last_mono is not None and monotonic-self.last_mono<self.interval:
            return False
        sample=detector.ring[-1] if detector.ring else None
        age=now-detector.last_ts if detector.last_ts is not None else None
        record={"version":"0.1.1","ts":now,"connected":bool(connected),
                "source_ts":detector.last_ts,"source_age_s":round(age,1) if age is not None else None,
                "source_fresh":bool(age is not None and -5<=age<=STALE and not detector.gap),
                "core_mib":sample.core if sample else None,
                "available_mib":sample.available if sample else None,
                "swap_mib":sample.swap if sample else None,
                "carried_fields":list(sample.carried_fields) if sample else [],
                "recording":detector.journal.active is not None,
                "flags":sorted(detector.flags),"buffer_samples":len(detector.ring),
                "telemetry_gap":detector.gap,"profiling":"disabled"}
        # Only one writer; do not follow links or leave partial status files.
        fd=None;temp=self.directory/".status.tmp";target=self.directory/"status.json"
        try:
            fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|getattr(os,"O_NOFOLLOW",0),0o600)
            with os.fdopen(fd,"w") as f:
                fd=None;json.dump(record,f,separators=(",",":"),allow_nan=False)
                f.write("\n");f.flush();os.fsync(f.fileno())
            os.replace(temp,target)
            self.last_mono=monotonic
        except OSError:
            if fd is not None: os.close(fd)
            # Keep retry rate bounded, including disk-full/read-only conditions.
            self.last_mono=monotonic
            LOG.warning("health status write failed")
            return False
        return True

async def session(url,token,detector,heartbeat=None):
    from websockets.asyncio.client import connect
    async with connect(url,open_timeout=5,close_timeout=2,ping_interval=20,ping_timeout=5,max_size=262144,max_queue=8,compression=None) as ws:
        async def receive(timeout=5): return json.loads(await asyncio.wait_for(ws.recv(),timeout))
        if (await receive()).get('type')!='auth_required': raise RuntimeError('unexpected_handshake')
        await ws.send(json.dumps({'type':'auth','access_token':token}))
        if (await receive()).get('type')!='auth_ok': raise RuntimeError('auth_failed')
        await ws.send(json.dumps({'id':1,'type':'subscribe_entities','entity_ids':SUBSCRIPTION}))
        response=await receive()
        if response.get('id')!=1 or not response.get('success'): raise RuntimeError('subscription_failed')
        feed=EntityFeed()
        LOG.info("telemetry subscription connected")
        while True:
            now=time.monotonic()
            timeout=min(1.0,max(0.01,feed.due-now)) if feed.due is not None else 1.0
            try: message=await receive(timeout)
            except asyncio.TimeoutError: message=None
            if message and message.get('id')==1 and message.get('type')=='event':
                event=message.get('event')
                if isinstance(event,dict): feed.update(event,time.monotonic())
            s=feed.pop(time.monotonic())
            if s: detector.feed(s,time.time())
            detector.check_gap(time.time())
            if heartbeat: heartbeat.publish(detector,True,time.time(),time.monotonic())

async def main():
    token=os.environ['SUPERVISOR_TOKEN']
    url='ws://supervisor/core/websocket'
    j=Journal(Path('/share/Logs/memory-watch'))
    def notice(event): LOG.warning('memory-watch %s',json.dumps(event,separators=(',',':')))
    detector=Detector(j,notice);detector.arm(time.time())
    heartbeat=Heartbeat(j.directory)
    delay=2
    while True:
        connection_started=time.monotonic()
        try:
            await session(url,token,detector,heartbeat)
        except (OSError,RuntimeError,ValueError,asyncio.TimeoutError) as exc:
            # Never log the exception text, URL headers, auth token, or HA payloads.
            LOG.warning('telemetry disconnected: %s',type(exc).__name__)
        except Exception as exc:
            LOG.warning('telemetry error: %s',type(exc).__name__)
        detector.check_gap(time.time())
        heartbeat.publish(detector,False,time.time(),time.monotonic(),force=True)
        if time.monotonic()-connection_started >= 60:
            delay=2
        await asyncio.sleep(delay);delay=min(delay*2,15)

if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    asyncio.run(main())
