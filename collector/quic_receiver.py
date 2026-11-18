"""QUIC receiver adapter. Use receive timestamps from native C, never pipe-read time."""
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import uuid


def run(args, state, output, ready, receive, subscribed):
    root=Path(__file__).resolve().parents[1]
    env=dict(os.environ,MQTT_CA_FILE=str(args.ca.resolve()),MQTT_SERVER_NAME=args.server_name)
    env['SSLKEYLOGFILE']=str(args.output/'collector.keys.log')
    messages=queue.Queue()
    child=None
    error_log=(args.output/'collector.quic.stderr').open('w')
    raw=(args.output/'collector.quic.jsonl').open('w')
    def event(line):
        raw.write(line)
        raw.flush()
        item=json.loads(line)
        if item['event']=='connect':
            state['connect_to_connack_us']=item['connect_to_connack_us']
            if not item['success']:
                raise RuntimeError('QUIC collector CONNACK failed')
        elif item['event']=='tls':
            state['tls']=item
        elif item['event']=='subscribed':
            state['connect_to_suback_us']=item['connect_to_suback_us']
            subscribed(None,None,None,[SimpleNamespace(is_failure=False)],None)
        elif item['event']=='receive':
            # C encodes every byte >=128 as a JSON unicode escape; latin1 reverses it.
            receive(None,None,SimpleNamespace(payload=item['payload'].encode('latin1'),
                    topic=item['topic'],retain=item['retained'],recv_ts_us=item['recv_ts_us']))
    try:
        child=subprocess.Popen([str(root/'pub/quic-client'),args.host,'sub_'+uuid.uuid4().hex[:12],
                '0','100','10','--sub','quic4iot/+/data'],env=env,
                stdout=subprocess.PIPE,stderr=error_log,text=True)
        def read():
            for line in child.stdout:
                messages.put(line)
            messages.put(None)
        reader=threading.Thread(target=read,daemon=True)
        reader.start()
        deadline=time.monotonic()+15
        while not state['stopping']:
            try:
                line=messages.get(timeout=.2)
            except queue.Empty:
                if not state['ready'] and time.monotonic()>deadline:
                    raise TimeoutError('QUIC collector did not receive SUBACK')
                continue
            if line is None:
                if state['stopping']:
                    break
                raise RuntimeError('QUIC collector connection ended unexpectedly')
            event(line)
    except Exception as error:
        state.update(failed=True,error=str(error))
        print('ERROR',error,file=sys.stderr,flush=True)
    finally:
        state['stopping']=True
        if child is not None:
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill();child.wait()
            state['child_exit_code']=child.returncode
            if child.returncode:
                state['failed']=True
            reader.join(timeout=2)
            while not messages.empty():
                line=messages.get_nowait()
                if line:
                    try:
                        event(line)
                    except Exception as error:
                        state.update(failed=True,error=str(error))
            child.stdout.close()
        ready.unlink(missing_ok=True)
        state['stopped_realtime_us']=time.time_ns()//1000
        output.close();raw.close();error_log.close()
        (args.output/'collector.meta.json').write_text(json.dumps(state,indent=2),encoding='utf-8')
    return int(state['failed'])
