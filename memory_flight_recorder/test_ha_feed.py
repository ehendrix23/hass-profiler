import asyncio
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from flight_recorder import Detector,Journal,Sample
from ha_feed import EntityFeed,FIELDS,STAMP,SUBSCRIPTION,session,Heartbeat

class TestFeed(unittest.TestCase):
    def setUp(self):
        self.t=1795668000
        self.feed=EntityFeed()
    def iso(self,t): return dt.datetime.fromtimestamp(t,dt.timezone.utc).isoformat()
    def test_initial_delta_and_settle(self):
        self.feed.update({'a':{STAMP:{'s':self.iso(self.t),'lc':self.t},FIELDS['core']:{'s':'1800','lc':self.t},FIELDS['swap']:{'s':'700','lc':self.t-180},FIELDS['available']:{'s':'2100','lc':self.t-180}}},0)
        self.assertIsNone(self.feed.pop(1))
        self.feed.update({'c':{FIELDS['core']:{'+':{'s':'1950','lc':self.t+0.1}}}},1)
        s=self.feed.pop(2.1)
        self.assertEqual(s.core,1950);self.assertEqual(s.swap,700)
        self.assertIn('swap',s.carried_fields);self.assertIsNone(s.available)
        self.assertIsNone(self.feed.pop(3))
    def test_unavailable_and_removed(self):
        self.feed.update({'a':{STAMP:{'s':self.iso(self.t),'lc':self.t},FIELDS['core']:{'s':'unavailable','lc':self.t}}},0)
        self.assertIsNone(self.feed.pop(2).core)
        self.feed.update({'c':{STAMP:{'+':{'s':self.iso(self.t+15),'lc':self.t+15}}},'r':[FIELDS['core']]},15)
        self.assertIsNone(self.feed.pop(17).core)
    def test_first_sample_missing(self):
        with tempfile.TemporaryDirectory() as d:
            detector=Detector(Journal(Path(d)));detector.arm(100)
            self.assertTrue(detector.check_gap(146));self.assertFalse(detector.check_gap(147))
    def test_abrupt_process_exit_persists(self):
        with tempfile.TemporaryDirectory() as d:
            code='import os,sys;from pathlib import Path;from flight_recorder import *;j=Journal(Path(sys.argv[1]));det=Detector(j);det.feed(Sample(100,2600,available=2000,swap=700),100);os._exit(7)'
            p=subprocess.run([sys.executable,'-c',code,d],cwd=Path(__file__).parent)
            self.assertEqual(p.returncode,7)
            files=list(Path(d).glob('incident-*.jsonl'));self.assertEqual(len(files),1)
            rows=[json.loads(x) for x in files[0].read_text().splitlines()]
            self.assertEqual(rows[-1]['sample']['core'],2600)

    def test_atomic_heartbeat_fresh_and_stale(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d);det=Detector(Journal(path));h=Heartbeat(path)
            det.feed(Sample(100,1800,available=2000,swap=700),100)
            self.assertTrue(h.publish(det,True,100,0))
            status=json.loads((path/'status.json').read_text())
            self.assertTrue(status['source_fresh']);self.assertEqual(status['core_mib'],1800)
            self.assertFalse(h.publish(det,True,105,5))
            det.check_gap(146)
            self.assertTrue(h.publish(det,False,146,46,force=True))
            status=json.loads((path/'status.json').read_text())
            self.assertFalse(status['source_fresh']);self.assertTrue(status['telemetry_gap'])
            self.assertFalse(status['connected']);self.assertFalse((path/'.status.tmp').exists())
    def test_health_status_does_not_touch_incidents(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d);det=Detector(Journal(path));h=Heartbeat(path)
            det.feed(Sample(100,2600,available=2000,swap=700),100)
            f=det.journal.active;original=f.read_bytes()
            h.publish(det,True,100,0)
            self.assertEqual(f.read_bytes(),original)

class TestSocket(unittest.IsolatedAsyncioTestCase):
    async def test_mock_websocket_handshake_and_filtered_subscription(self):
        from websockets.asyncio.server import serve
        now=int(time.time());seen=[]
        async def server(ws):
            await ws.send(json.dumps({'type':'auth_required'}))
            auth=json.loads(await ws.recv());self.assertEqual(auth,{'type':'auth','access_token':'fixture'})
            await ws.send(json.dumps({'type':'auth_ok'}))
            sub=json.loads(await ws.recv());seen.append(sub)
            await ws.send(json.dumps({'id':1,'type':'result','success':True,'result':None}))
            ts=dt.datetime.fromtimestamp(now,dt.timezone.utc).isoformat()
            await ws.send(json.dumps({'id':1,'type':'event','event':{'a':{STAMP:{'s':ts,'lc':now},FIELDS['core']:{'s':'2600','lc':now},FIELDS['available']:{'s':'2000','lc':now},FIELDS['swap']:{'s':'700','lc':now}}}}))
            await asyncio.sleep(2.3)
        with tempfile.TemporaryDirectory() as d:
            detector=Detector(Journal(Path(d)))
            async with serve(server,'127.0.0.1',0) as srv:
                port=srv.sockets[0].getsockname()[1]
                try: await asyncio.wait_for(session(f'ws://127.0.0.1:{port}','fixture',detector),5)
                except Exception as exc:
                    from websockets.exceptions import ConnectionClosed
                    if not isinstance(exc,ConnectionClosed): raise
            self.assertEqual(seen,[{'id':1,'type':'subscribe_entities','entity_ids':SUBSCRIPTION}])
            self.assertTrue(any('absolute_core' in e['reasons'] for e in detector.events))
    async def test_two_real_mock_sessions_preserve_incident(self):
        from websockets.asyncio.server import serve
        from websockets.exceptions import ConnectionClosed
        from unittest.mock import patch
        now=int(time.time());seen=[]
        async def server(ws):
            await ws.send(json.dumps({'type':'auth_required'}))
            self.assertEqual(json.loads(await ws.recv())['type'],'auth')
            await ws.send(json.dumps({'type':'auth_ok'}))
            sub=json.loads(await ws.recv());seen.append(sub)
            n=len(seen)-1
            await ws.send(json.dumps({'id':1,'type':'result','success':True,'result':None}))
            stamp=now+n
            ts=dt.datetime.fromtimestamp(stamp,dt.timezone.utc).isoformat()
            states={STAMP:{'s':ts,'lc':stamp},FIELDS['core']:{'s':str(2600+100*n),'lc':stamp},FIELDS['available']:{'s':'2000','lc':stamp},FIELDS['swap']:{'s':'700','lc':stamp}}
            await ws.send(json.dumps({'id':1,'type':'event','event':{'a':states}}))
            await asyncio.sleep(.2)
        with tempfile.TemporaryDirectory() as d:
            detector=Detector(Journal(Path(d)))
            async with serve(server,'127.0.0.1',0) as srv:
                port=srv.sockets[0].getsockname()[1]
                with patch('ha_feed.SETTLE',.01):
                    for _ in range(2):
                        with self.assertRaises(ConnectionClosed):
                            await asyncio.wait_for(session(f'ws://127.0.0.1:{port}','fixture',detector),3)
            self.assertEqual(len(seen),2)
            self.assertTrue(all(s['entity_ids']==SUBSCRIPTION for s in seen))
            self.assertEqual(detector.last_ts,now+1)
            self.assertEqual(len(detector.journal.files()),1)
            rows=[json.loads(x) for x in detector.journal.files()[0].read_text().splitlines()]
            self.assertEqual([r['sample']['core'] for r in rows if r['type']=='sample'],[2600,2700])

if __name__=='__main__': unittest.main(verbosity=2)
