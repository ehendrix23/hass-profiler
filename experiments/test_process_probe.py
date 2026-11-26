import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from process_probe import capture_metadata, start_ticks, safe_to_sample, bounded_stack_sample, LIMIT

class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pid = 4242
        self.p = self.root/str(self.pid); self.p.mkdir()
        (self.p/'fd').mkdir(); (self.p/'fd'/'1').symlink_to('socket:[42]')
        self.ticks = 98765
        (self.p/'stat').write_text(f'{self.pid} (test process) S '+'0 '*18+f'{self.ticks} 0\n')
        (self.p/'status').write_text('VmRSS: 1700000 kB\nRssAnon: 1600000 kB\nVmSwap: 100000 kB\nThreads: 3\n')
        (self.root/'meminfo').write_text('MemAvailable: 2200000 kB\nMemFree: 200000 kB\nSwapFree: 1000000 kB\nSwapTotal: 2000000 kB\n')
    def tearDown(self): self.temp.cleanup()
    def test_numeric_metadata(self):
        self.assertEqual(start_ticks(self.pid,self.root), self.ticks)
        m = capture_metadata(self.pid,self.ticks,self.root)
        self.assertEqual(m['socket_count'],1); self.assertEqual(m['target']['VmRSS'],1700000)
        self.assertTrue(safe_to_sample(m))
    def test_real_self_metadata(self):
        pid = os.getpid(); m = capture_metadata(pid,start_ticks(pid))
        self.assertGreater(m['target']['VmRSS'],0)
    def test_wrong_pid_generation(self):
        with self.assertRaises(ValueError): capture_metadata(self.pid,self.ticks+1,self.root)
    def test_no_headroom_never_starts(self):
        (self.root/'meminfo').write_text('MemAvailable: 400000 kB\nSwapFree: 100000 kB\n')
        with patch('process_probe.subprocess.Popen') as p:
            self.assertEqual(bounded_stack_sample(self.pid,self.ticks,proc=self.root)['status'],'skipped_headroom')
            p.assert_not_called()
    def fake_sampler(self, body):
        path=self.root/'fake-py-spy';path.write_text('#!/bin/sh\n'+body+'\n');path.chmod(0o700);return str(path)
    def test_bounded_command_and_output(self):
        b=self.fake_sampler('test "$1" = record || exit 2; test "$4" = --nonblocking || exit 3; for last; do :; done; printf "synthetic_stack 1\\n" > "$last"')
        r=bounded_stack_sample(self.pid,self.ticks,b,self.root)
        self.assertEqual(r['status'],'complete');self.assertGreater(r['bytes'],0)
        self.assertNotIn('synthetic_stack',str(r))
    def test_output_limit(self):
        b=self.fake_sampler('for last; do :; done; head -c 300000 /dev/zero > "$last"')
        r=bounded_stack_sample(self.pid,self.ticks,b,self.root)
        self.assertLessEqual(r['bytes'],LIMIT);self.assertNotEqual(r['status'],'complete')
    def test_timeout(self):
        b=self.fake_sampler('exec sleep 30')
        with patch('process_probe.TIMEOUT',.2):
            r=bounded_stack_sample(self.pid,self.ticks,b,self.root)
        self.assertEqual(r['status'],'timeout');self.assertLess(r['elapsed_seconds'],3)

if __name__=='__main__': unittest.main()
