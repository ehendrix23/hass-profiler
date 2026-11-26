import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from flight_recorder import Sample,Detector,Journal,proc_metadata

class TestFlightRecorder(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)
        self.d=Detector(Journal(self.path))
    def tearDown(self): self.tmp.cleanup()
    def sample(self,t,core=1800,**kwargs):
        return Sample(t,core,**({'available':2000,'swap':700,'swap_free':1235}|kwargs))
    def feed(self,t,core=1800,**kwargs): return self.d.feed(self.sample(t,core,**kwargs),t)
    def replay(self,name):
        fixture=json.loads(Path(__file__).with_name('replay.json').read_text())[name]
        base=dt.datetime.fromisoformat(fixture['start']).timestamp()
        # Unknown headroom is deliberately retained as unknown; test growth
        # logic separately from the absence-of-headroom-data diagnostic.
        for offset,v in fixture['points']:
            s=Sample(base+offset,v)
            self.d.feed(s,s.ts)
        return base
    def test_oom_replay(self):
        base=self.replay('incident')
        e=[e for e in self.d.events if 'rapid_60s' in e['reasons']]
        self.assertAlmostEqual(e[0]['ts']-base,120.0,places=2)
        self.assertTrue(any('rapid_5min' in e['reasons'] for e in self.d.events))
        self.assertEqual(len(self.d.journal.files()),1)
        rows=[json.loads(x) for x in self.d.journal.files()[0].read_text().splitlines()]
        self.assertTrue(any(x.get('sample',{}).get('core')==3800 for x in rows))
        print('Incident replay: rapid growth detected at',dt.datetime.fromtimestamp(e[0]['ts'],dt.timezone.utc).isoformat(),'records',len(rows))
    def test_healthy_control(self):
        self.replay('control')
        self.assertFalse(any(any(r!='headroom_unknown' for r in e['reasons']) for e in self.d.events))
    def test_reject_duplicate_out_of_order_and_stale(self):
        self.assertTrue(self.feed(100,1800))
        self.assertFalse(self.feed(100,3000)); self.assertFalse(self.feed(99,3000))
        self.assertFalse(self.d.feed(self.sample(110,3000),200))
        self.assertFalse(self.d.events)
    def test_stale_one_event_then_reconnect(self):
        self.feed(100,1800)
        self.assertTrue(self.d.check_gap(146)); self.assertFalse(self.d.check_gap(147))
        self.feed(160,2800)
        self.assertEqual(len(self.d.journal.files()),1)
        self.assertFalse(any('rapid_60s' in e['reasons'] for e in self.d.events))
        rows=[json.loads(x) for x in self.d.journal.files()[0].read_text().splitlines()]
        self.assertTrue(any(x['type']=='telemetry_resumed' for x in rows))
    def test_critical(self):
        self.feed(100,1800,available=400,swap_free=100)
        self.assertIn('critical_available',self.d.events[0]['reasons'])
        self.assertIn('critical_swap_free',self.d.events[0]['reasons'])
    def test_nonfinite_rejected(self): self.assertFalse(self.feed(100,float('nan')))
    def test_rolling_buffer_and_capture_timeout(self):
        for t in range(0,3600,15): self.feed(t)
        self.assertLessEqual(len(self.d.ring),128)
        self.feed(3600,2600)
        for t in range(3615,4516,15): self.feed(t,2600)
        self.assertIsNotNone(self.d.journal.active)
        self.assertEqual(len(self.d.journal.files()),1)
        for t in range(4530,5416,15): self.feed(t,1800)
        self.assertIsNone(self.d.journal.active)
        self.feed(5430,2800)
        self.assertIsNotNone(self.d.journal.active)
    def test_persistence_and_rotation(self):
        j=Journal(self.path,max_total=1300,max_file=700)
        d=Detector(j); d.feed(self.sample(100,2600),100)
        for t in range(115,280,15): d.feed(self.sample(t,2600),t)
        self.assertLessEqual(sum(p.stat().st_size for p in j.files()),1300)
        self.assertTrue(all(p.stat().st_size<=700 for p in j.files()))
        # Reopen after simulated process loss; prior records remain parseable.
        j2=Journal(self.path,max_total=1300,max_file=700)
        self.assertTrue(j2.files())
        for p in j2.files():
            for line in p.read_text().splitlines(): json.loads(line)
    def test_own_pid_metadata(self):
        p=proc_metadata(os.getpid())
        self.assertGreater(p['VmRSS'],0)
        self.assertIn('VmSwap',p); self.assertIn('fd_count',p)
        with self.assertRaises(ValueError): proc_metadata(os.getpid(),p['start_ticks']+1)

if __name__=='__main__': unittest.main(verbosity=2)
