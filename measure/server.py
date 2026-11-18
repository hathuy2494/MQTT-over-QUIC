"""Machine B per-run receiver/capture supervisor, controlled via SSH stdin."""
import argparse
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from common import clock_status, command, identity, write_json, validate_listeners, host_info

ROOT = Path(__file__).resolve().parents[1]


def wait_ready(child):
    selector = selectors.DefaultSelector()
    selector.register(child.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + 25
    try:
        while time.monotonic() < deadline:
            if selector.select(.2):
                line = child.stdout.readline()
                if line.strip() == 'READY':
                    return
                if not line:
                    raise RuntimeError('Receiver or capture process exited before READY')
            if child.poll() is not None:
                raise RuntimeError('Receiver or capture process failed to start')
        raise TimeoutError('Receiver or capture readiness timed out')
    finally:
        selector.close()


def main():
    def interrupt(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGHUP, interrupt)
    p = argparse.ArgumentParser()
    p.add_argument('--run', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--interface', required=True)
    p.add_argument('--publisher-ip', required=True)
    p.add_argument('--transport', required=True, choices=['tcp', 'tls', 'quic'])
    args = p.parse_args()
    os.environ['LC_ALL'] = 'C'
    import re
    if not re.fullmatch(r'[A-Za-z0-9_]{1,20}', args.run):
        p.error('Invalid run_id')
    route = json.loads(command(['ip','-j','route','get',args.publisher_ip]))[0]
    if route['dev'] != args.interface or route['dev']=='lo':
        p.error('Machine B capture interface does not match route to publisher A')
    out = (ROOT / args.output).resolve()
    if not out.is_relative_to(ROOT / 'results'):
        p.error('Output must be under QUIC1/results')
    out.mkdir(parents=True, exist_ok=False)
    lock = ROOT / 'results/.receiver.lock'
    lock_file = lock.open('a')
    import fcntl
    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    metadata = dict(machine_id=identity(), host=host_info(), clock=clock_status(),
                    listeners=command(['docker', 'exec', 'quic1-emqx', 'emqx', 'ctl', 'listeners'], timeout=20))
    validate_listeners(metadata['listeners'])
    write_json(out / 'receiver.json', metadata)
    children, handles = [], []
    try:
        for folder, script, extra in [
            ('dumpcap', 'capture.py', ['--interface', args.interface, '--publisher-ip', args.publisher_ip,'--include-local-collector']),
            ('collector', 'collector.py', ['--host', '127.0.0.1','--transport',args.transport])]:
            err = (out / f'{folder}.supervisor.log').open('w')
            handles.append(err)
            child = subprocess.Popen([sys.executable, '-u', str(ROOT / folder / script),
                                      '--run', args.run, '--output', str(out), *extra],
                                     stdout=subprocess.PIPE, stderr=err, text=True)
            children.append(child)
            wait_ready(child)
        print(json.dumps(dict(event='READY', **metadata)), flush=True)
        selector = selectors.DefaultSelector()
        selector.register(sys.stdin, selectors.EVENT_READ)
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            if any(c.poll() is not None for c in children):
                raise RuntimeError('Collector or capture exited during measurement')
            if selector.select(.2):
                if sys.stdin.readline().strip() == 'STOP':
                    break
                raise RuntimeError('SSH controller disconnected without STOP')
        else:
            raise TimeoutError('Receiver safety deadline exceeded')
        selector.close()
    finally:
        for c in reversed(children):
            if c.poll() is None:
                c.send_signal(signal.SIGINT)
                try:
                    c.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    c.kill()
                    c.wait()
        for handle in handles:
            handle.close()
        write_json(out / 'receiver_end.json', dict(clock=clock_status(),
                    child_exit_codes=[child.returncode for child in children]))
    print(json.dumps(dict(event='STOPPED', run_id=args.run)), flush=True)
    if any(child.returncode != 0 for child in children):
        raise RuntimeError('Receiver or capture failed; inspect supervisor logs')
    with (out / 'tshark.log').open('w') as log:
        subprocess.run([sys.executable, str(ROOT/'dumpcap/export.py'),
                        str(out/f'{args.run}.pcapng'),'--publisher-ip',args.publisher_ip,
                        '--transport',args.transport,'--output',str(out)],
                       stdout=log,stderr=subprocess.STDOUT,check=True,timeout=30)
        subprocess.run([sys.executable,str(ROOT/'dumpcap/export.py'),
                        str(out/f'{args.run}.pcapng'),'--publisher-ip','127.0.0.1',
                        '--transport',args.transport,'--output',str(out/'collector_capture')],
                       stdout=log,stderr=subprocess.STDOUT,check=True,timeout=30)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Receiver interrupted; inspect per-run logs.',file=sys.stderr)
        sys.exit(130)
    except Exception as error:
        print(f'ERROR receiver: {error}',file=sys.stderr)
        sys.exit(1)
