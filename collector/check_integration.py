"""Developer functional check only; NEVER report its timings as two-machine S1/S2."""
import csv
import json
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
import uuid
import paho.mqtt.publish as publish


def wait_ready(process):
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic()+20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('child exited before READY')
        if selector.select(timeout=0.2):
            if process.stdout.readline().strip() == 'READY':
                return
    raise TimeoutError('child not READY')


def main():
    root = Path(__file__).resolve().parents[1]
    run = 'check_'+uuid.uuid4().hex[:8]
    out = root/'collector'/'.checks'/run
    collector = subprocess.Popen([sys.executable, str(root/'collector'/'collector.py'),
                                  '--run', run, '--output', str(out)], stdout=subprocess.PIPE, text=True)
    capture = None
    try:
        wait_ready(collector)
        capture = subprocess.Popen([sys.executable, str(root/'dumpcap'/'capture.py'),
                                    '--run', run, '--output', str(out), '--interface', 'lo',
                                    '--publisher-ip', '127.0.0.1'], stdout=subprocess.PIPE, text=True)
        wait_ready(capture)
        body = dict(v=1, run_id=run, device_id='pi01', seq=0, ts_us=time.time_ns()//1000,
                    temp=27.5, pad='')
        encode = lambda: json.dumps(body, separators=(',', ':')).encode()
        body['pad']='x'*(128-len(encode()))
        publish.single('quic4iot/pi01/data', encode(), hostname='127.0.0.1')
        publish.single('quic4iot/pi01/data', 'not json', hostname='127.0.0.1')
        body['run_id']='another_run'
        publish.single('quic4iot/pi01/data', encode(), hostname='127.0.0.1')
        time.sleep(0.5)
    finally:
        for process in [collector, capture]:
            if process and process.poll() is None:
                process.send_signal(signal.SIGINT)
                process.wait(timeout=15)
    assert collector.returncode == 0
    assert capture.returncode == 0
    with (out/f'{run}.csv').open() as source:
        rows = list(csv.DictReader(source))
    assert len(rows) == 2, rows
    assert rows[0]['valid'] == 'True'
    assert rows[1]['valid'] == 'False'
    meta = json.loads((out/'collector.meta.json').read_text())
    assert meta['foreign'] == 1
    assert not (out/'collector.ready').exists()
    assert not (out/'capture.ready').exists()
    subprocess.run([sys.executable, str(root/'dumpcap'/'export.py'), str(out/f'{run}.pcapng'),
                    '--publisher-ip','127.0.0.1','--transport','tcp','--output',str(out)], check=True)
    summary=json.loads((out/'capture_summary.json').read_text())
    assert summary['selected_frames'] > 0
    assert summary['n_pkt_to_connack'] is None
    print('PASS collector SUBACK, valid/invalid/foreign messages, clean shutdown, dumpcap capture and tshark export')
    print('Developer functional check, not S1/S2 measurement. Evidence:', out)


if __name__ == '__main__':
    main()
