"""Machine-B subscriber using the same transport as the publisher on both legs."""
import argparse
import csv
import json
import re
import signal
import sys
import time
from pathlib import Path

FIELDS = ['recv_ts_us', 'topic', 'run_id', 'seq', 'ts_us', 'latency_us',
          'valid', 'error', 'bytes', 'retained']


def validate_message(payload, topic, received, validator):
    row = dict.fromkeys(FIELDS, '')
    row.update(recv_ts_us=received, topic=topic, bytes=len(payload), valid=False)
    body = None
    try:
        body = json.loads(payload)
        if not isinstance(body, dict):
            raise ValueError('JSON body must be an object')
        row.update(run_id=body.get('run_id', ''), seq=body.get('seq', ''), ts_us=body.get('ts_us', ''))
        validator.validate(body)
        if len(payload) != 128:
            raise ValueError('payload must be exactly 128 bytes')
        if topic != f"quic4iot/{body['device_id']}/data":
            raise ValueError('topic/device_id mismatch')
        row.update(valid=True, latency_us=received-body['ts_us'])
    except Exception as error:
        row['error'] = str(error).splitlines()[0]
    return row


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run', required=True)
    p.add_argument('--output', type=Path, required=True, help='directory, not CSV filename')
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--transport',choices=['tcp','tls','quic'],default='tcp')
    p.add_argument('--port', type=int)
    p.add_argument('--ca',type=Path,default=Path(__file__).resolve().parents[1]/'emqx/certs/ca.crt')
    p.add_argument('--server-name',default='emqx.local')
    a = p.parse_args()
    a.port=a.port or {'tcp':1883,'tls':8883,'quic':14567}[a.transport]
    if a.transport=='quic' and a.port!=14567:
        p.error('Native QUIC receiver uses UDP14567')
    if not re.fullmatch(r'[A-Za-z0-9_]{1,20}', a.run):
        p.error('run must have 1–20 letters, digits or underscores')
    import paho.mqtt.client as mqtt
    import jsonschema
    schema = json.loads((Path(__file__).parent / 'schema.json').read_text(encoding='utf-8'))
    validator = jsonschema.Draft202012Validator(schema)
    a.output.mkdir(parents=True, exist_ok=True)
    csvpath = a.output / f'{a.run}.csv'
    ready = a.output / 'collector.ready'
    if csvpath.exists():
        p.error('run CSV already exists; choose a NEW run_id/output to avoid mixing measurements')
    ready.unlink(missing_ok=True)
    state = dict(ready=False, failed=False, stopping=False, rows=0, foreign=0,
                 started_realtime_us=time.time_ns()//1000, run_id=a.run, host=a.host, port=a.port,
                 transport=a.transport, mqtt='3.1.1', subscribe_qos=1)
    output = csvpath.open('x', newline='', encoding='utf-8')
    writer = csv.DictWriter(output, fieldnames=FIELDS)
    writer.writeheader()
    output.flush()

    def connected(client, userdata, flags, reason, properties):
        state['connect_to_connack_us']=(time.monotonic_ns()-state['connect_start_ns'])//1000
        if reason.is_failure:
            state['failed'] = True
            print('ERROR CONNACK', reason, file=sys.stderr, flush=True)
        else:
            if a.transport=='tls':
                cipher=client.socket().cipher()
                if cipher[:2]!=('TLS_AES_128_GCM_SHA256','TLSv1.3'):
                    state.update(failed=True,error='Collector TLS cipher mismatch')
                    return
                state['tls_cipher']=cipher[0]
            client.subscribe('quic4iot/+/data', qos=1)

    def subscribed(client, userdata, mid, reasons, properties):
        if not reasons or any(r.is_failure for r in reasons):
            state['failed'] = True
            return
        state['ready'] = True
        if a.transport!='quic':
            state['connect_to_suback_us']=(time.monotonic_ns()-state['connect_start_ns'])//1000
        ready.write_text(a.run, encoding='ascii')
        print('READY', flush=True)

    def receive(client, userdata, message):
        received = getattr(message,'recv_ts_us',None)
        if received is None:
            received=time.time_ns()//1000  # before JSON parsing or disk IO
        row = validate_message(message.payload, message.topic, received, validator)
        # Foreign valid JSON belongs to another run; malformed input is retained as an error row.
        if (isinstance(row['run_id'], str)
                and re.fullmatch(r'[A-Za-z0-9_]{1,20}', row['run_id'])
                and row['run_id'] != a.run):
            state['foreign'] += 1
            return
        row['retained'] = message.retain
        if message.retain:
            row.update(valid=False, error='retained message excluded')
        writer.writerow(row)
        output.flush()
        state['rows'] += 1

    def disconnected(client, userdata, flags, reason, properties):
        if not state['stopping']:
            state['failed'] = True
        ready.unlink(missing_ok=True)

    def stop(sig, frame):
        state['stopping'] = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    if a.transport=='quic':
        from quic_receiver import run
        return run(a,state,output,ready,receive,subscribed)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='sub_collector_01',
                         clean_session=True, protocol=mqtt.MQTTv311, reconnect_on_failure=False)
    client.on_connect = connected
    client.on_subscribe = subscribed
    client.on_message = receive
    client.on_disconnect = disconnected
    try:
        if a.transport=='tls':
            import ssl
            ctx=ssl.create_default_context(cafile=str(a.ca))
            ctx.minimum_version=ctx.maximum_version=ssl.TLSVersion.TLSv1_3
            ctx.set_ecdh_curve('X25519')
            ctx.keylog_filename=str((a.output/'collector.keys.log').resolve())
            # Python's SSL API cannot restrict TLS1.3 ciphers on all versions.
            # EMQX TLS listener restricts AES128; verify the negotiated cipher above.
            client.tls_set_context(ctx)
        state['connect_start_ns']=time.monotonic_ns()
        client.connect(a.host, a.port, keepalive=30)
        deadline = time.monotonic()+10
        while not state['stopping'] and not state['failed']:
            rc = client.loop(timeout=0.2)
            if rc != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError(f'MQTT loop failed: {rc}')
            if not state['ready'] and time.monotonic() > deadline:
                raise TimeoutError('no SUBACK within 10 seconds')
    except Exception as error:
        state.update(failed=True, error=str(error))
        print('ERROR', error, file=sys.stderr, flush=True)
    finally:
        state['stopping'] = True
        client.disconnect()
        ready.unlink(missing_ok=True)
        state['stopped_realtime_us'] = time.time_ns()//1000
        output.close()
        (a.output / 'collector.meta.json').write_text(json.dumps(state, indent=2), encoding='utf-8')
    return int(state['failed'])


if __name__ == '__main__':
    raise SystemExit(main())
