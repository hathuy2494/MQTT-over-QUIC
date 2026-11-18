"""Run on machine B: capture the physical A–B interface using Wireshark dumpcap."""
import argparse
import ipaddress
import json
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import threading
import time


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--interface', required=True)
    p.add_argument('--publisher-ip', type=ipaddress.IPv4Address, required=True)
    p.add_argument('--run', required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--duration', type=float, help='optional automatic stop after this many seconds')
    a = p.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_]{1,20}', a.run):
        p.error('invalid run_id')
    if a.duration is not None and a.duration <= 0:
        p.error('duration must be positive')
    a.output.mkdir(parents=True, exist_ok=True)
    path = a.output / f'{a.run}.pcapng'
    if path.exists():
        p.error('capture already exists; choose a new run_id/output')
    ready = a.output / 'capture.ready'
    ready.unlink(missing_ok=True)
    command = ['dumpcap', '-i', a.interface, '-f',
               f'host {a.publisher_ip} and (tcp port 1883 or tcp port 8883 or udp port 14567)',
               '-w', str(path)]
    if a.duration:
        command += ['-a', f'duration:{a.duration}']
    log = []
    messages = queue.Queue()
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               text=True, encoding='utf-8', errors='replace')
    def read():
        for line in process.stderr:
            messages.put(line)
        messages.put(None)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    stopped = False
    def stop(sig, frame):
        nonlocal stopped
        stopped = True
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    state = dict(run_id=a.run, command=command, started_realtime_us=time.time_ns()//1000,
                 ready=False, interface=a.interface, publisher_ip=str(a.publisher_ip))
    deadline = time.monotonic()+15
    try:
        while process.poll() is None:
            try:
                line = messages.get(timeout=0.2)
            except queue.Empty:
                line = ''
            if line:
                log.append(line)
                # dumpcap emits File: only once capture interface was opened and output created.
                if 'File:' in line and path.exists() and not state['ready']:
                    state['ready'] = True
                    ready.write_text(a.run, encoding='ascii')
                    print('READY', flush=True)
            if not state['ready'] and time.monotonic() > deadline:
                raise TimeoutError('dumpcap did not confirm capture start within 15 seconds')
        process.wait()
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        reader.join(timeout=5)
        while not messages.empty():
            line = messages.get_nowait()
            if line:
                log.append(line)
        state.update(exit_code=process.returncode, stopped_by_user=stopped,
                     stopped_realtime_us=time.time_ns()//1000)
        ready.unlink(missing_ok=True)
        (a.output / 'dumpcap.log').write_text(''.join(log), encoding='utf-8')
        (a.output / 'capture.meta.json').write_text(json.dumps(state, indent=2), encoding='utf-8')
    if not state['ready'] or process.returncode not in [0, -signal.SIGINT]:
        print('Capture failed; see dumpcap.log', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
