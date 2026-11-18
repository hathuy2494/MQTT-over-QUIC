"""Algorithm fixtures only, not S1/S2 measurements."""
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'measure'))
from analyze import analyze, percentile
from common import validate_clocks, validate_listeners


class AnalysisTests(unittest.TestCase):
    def test_disabled_listener_rejected_even_with_port_present(self):
        text=''.join(f'{tp}:default\n  listen_on : 0.0.0.0:{port}\n  running : true\n' for tp,port in [('tcp',1883),('ssl',8883),('quic',14567)])
        validate_listeners(text)
        with self.assertRaises(ValueError):
            validate_listeners(text.replace('quic:default','quic:other'))
        with self.assertRaises(ValueError):
            validate_listeners(text.replace('running : true','running : false'))
    def test_clock_gate(self):
        good=dict(synchronized=True,offset_s=.0001)
        self.assertAlmostEqual(validate_clocks(good,good),.0002)
        with self.assertRaises(ValueError):
            validate_clocks(good,dict(synchronized=False,offset_s=0))
        with self.assertRaises(ValueError):
            validate_clocks(good,dict(synchronized=True,offset_s=.001))

    def fixture(self,root,clock_good=True):
        clock=dict(synchronized=clock_good,offset_s=.0001)
        meta=dict(run_id='test',scenario='s2',transport='tls',level=5,warmup=False,
                  count=4,period_ms=100,qos=0,exit_code=1,clock_a=clock,clock_b=clock,clock_a_end=clock)
        (root/'test.meta.json').write_text(json.dumps(meta))
        (root/'receiver_end.json').write_text(json.dumps(dict(clock=clock)))
        events=[dict(event='connect',success=True,connect_to_connack_us=100)]
        events += [dict(event='send',seq=i,ts_us=1000+i*100,rc=0) for i in range(3)]
        (root/'test.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
        with (root/'test.csv').open('w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=['valid','seq','ts_us','recv_ts_us'])
            w.writeheader()
            for seq,ts,received,valid in [(1,1100,1200,True),(1,1100,1250,True),
                                        (0,1000,1300,True),(2,1200,10001201,True),
                                        (2,1200,1400,False)]:
                w.writerow(dict(valid=valid,seq=seq,ts_us=ts,recv_ts_us=received))
        # Warmup must be excluded completely.
        meta['warmup']=True
        (root/'warmup.meta.json').write_text(json.dumps(meta))

    def test_duplicate_deadline_outoforder_and_failure_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            self.fixture(root)
            rows=analyze(root)
            self.assertEqual(len(rows),1)
            r=rows[0]
            self.assertFalse(r['success'])
            self.assertEqual(r['delivery_ratio'],.5)
            self.assertEqual(r['attempted'],3)
            self.assertEqual(r['duplicates'],1)
            self.assertEqual(r['out_of_order'],1)
            self.assertEqual(r['invalid'],1)
            self.assertEqual(r['late'],1)
            self.assertEqual(r['missing'],2)
            self.assertEqual(r['p50_us'],200)
            self.assertEqual(r['p95_us'],290)
            self.assertAlmostEqual(r['observation_s'],10.0002)

    def test_unsynchronized_clocks_suppress_latency(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            self.fixture(root,False)
            r=analyze(root)[0]
            self.assertIsNone(r['p50_us'])
            self.assertIsNone(r['goodput_kbps'])
            self.assertEqual(r['delivery_ratio'],.5)

    def test_missing_client_log_keeps_failure(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/'broken.meta.json').write_text(json.dumps(dict(run_id='broken',scenario='s1',
                transport='quic',level=0,warmup=False,exit_code=125)))
            r=analyze(root)[0]
            self.assertFalse(r['success'])
            self.assertIsNone(r['connect_to_connack_us'])

    def test_percentiles(self):
        self.assertIsNone(percentile([], .95))
        self.assertEqual(percentile([10,20,30],.95),29)


if __name__=='__main__':
    unittest.main()
