"""Machine A: coordinate real machine B over SSH and run one full matrix."""
import argparse
import ipaddress
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from common import clock_status, command, identity, validate_clocks, write_json, host_info

ROOT = Path(__file__).resolve().parents[1]


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('scenario', choices=['s1', 's2'])
    p.add_argument('--transport', required=True, choices=['tcp', 'tls', 'quic'])
    for name in ['broker', 'ssh', 'remote-dir', 'interface', 'capture-interface']:
        p.add_argument('--' + name, required=True)
    p.add_argument('--conditions', nargs='+', type=int)
    p.add_argument('--repeats', type=int, default=5)
    p.add_argument('--count', type=int, default=1000)
    p.add_argument('--period-ms', type=int, default=100)
    return p


def read_remote_ready(proc):
    import selectors
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    try:
        if not sel.select(55):
            raise TimeoutError('Machine B did not become READY; inspect server.log')
        line = proc.stdout.readline()
        data = json.loads(line)
        if data.get('event') != 'READY':
            raise RuntimeError(f'Unexpected remote startup: {line}')
        return data
    finally:
        sel.close()


def main():
    p = parser()
    args = p.parse_args()
    ip = ipaddress.IPv4Address(args.broker)
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast:
        p.error('Broker must be the IPv4 address of physical machine B')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*@'+re.escape(args.broker),args.ssh):
        p.error('--ssh must be user@BROKER_IP; machine B must also host EMQX')
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+',args.remote_dir):
        p.error('--remote-dir must be an absolute Linux path without spaces')
    levels = args.conditions if args.conditions is not None else ([0, 50, 200] if args.scenario == 's1' else [0, 1, 5, 10])
    if min(args.repeats, args.count, args.period_ms) < 1 or any(n < 0 or (args.scenario == 's2' and n > 100) for n in levels):
        p.error('Invalid repeats/count/period/conditions')
    route = json.loads(command(['ip', '-j', 'route', 'get', args.broker]))[0]
    if route['dev'] != args.interface or route['dev'] == 'lo':
        p.error('Wrong publisher interface; use ip route get BROKER_IP')
    addresses = json.loads(command(['ip', '-j', 'address']))
    local_ips = {a['local'] for interface in addresses for a in interface.get('addr_info', [])}
    if args.broker in local_ips:
        p.error('Two different machines are required; broker IP belongs to this machine')
    command(['sudo', '-n', 'true'])
    qdisc_before = command(['tc', '-j', 'qdisc', 'show', 'dev', args.interface])
    if any(q['kind'] not in {'noqueue','mq','fq_codel','fq','pfifo_fast'} for q in json.loads(qdisc_before)):
        p.error('Custom qdisc already installed; restore baseline before starting')
    stop = threading.Event()
    sudo_error = []
    def keep_sudo():
        while not stop.wait(45):
            if subprocess.run(['sudo','-n','-v'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
                sudo_error.append('sudo credential expired')
                break
    keepalive = threading.Thread(target=keep_sudo, daemon=True)
    keepalive.start()
    batch = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '_' + uuid.uuid4().hex[:4]
    out = ROOT / 'results' / batch
    out.mkdir(parents=True)
    baseline = subprocess.run(['ping','-n','-c','10','-i','.2','-W','2',args.broker],capture_output=True,text=True)
    (out/'ping_baseline.txt').write_text(baseline.stdout+baseline.stderr)
    ssh = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', args.ssh]
    command(ssh + ['true'], timeout=15)
    command(['scp', '-q', '-o', 'BatchMode=yes', f'{args.ssh}:{args.remote_dir}/emqx/certs/ca.crt', str(out / 'ca.crt')], timeout=30)
    env = dict(os.environ, MQTT_CA_FILE=str(out / 'ca.crt'), MQTT_SERVER_NAME='emqx.local')
    write_json(out / 'batch.json', dict(**vars(args), levels=levels, batch=batch,
                                       qdisc_before=json.loads(qdisc_before), machine_a_id=identity(),
                                       host_a=host_info(),
                                       environment='two physical Linux machines; EMQX on machine B',
                                       warmups=1, timeout_s=10, mqtt='3.1.1', qos=0, payload_bytes=128))
    active_qdisc = False
    try:
        for level in levels:
            for repeat in range(args.repeats + 1):
                if sudo_error:
                    raise RuntimeError(sudo_error[0])
                run = f'{args.scenario}{args.transport}{level}r{repeat}_{uuid.uuid4().hex[:4]}'
                if len(run)>20:
                    raise ValueError('run_id too long; choose smaller repeat count')
                local = out / run
                local.mkdir()
                remote_rel = f'results/{batch}/{run}'
                remote_cmd = shlex.join([args.remote_dir+'/.venv/bin/python', '-u', args.remote_dir + '/measure/server.py',
                    '--run',run,'--output',remote_rel,'--interface',args.capture_interface,
                    '--publisher-ip',route['prefsrc'],'--transport',args.transport])
                metadata = dict(run_id=run, scenario=args.scenario, transport=args.transport,
                                collector_transport=args.transport,protocol_scope='both legs; collector on B loopback',
                                level=level, warmup=repeat==0, count=args.count,
                                period_ms=args.period_ms, timeout_s=10, qos=0,
                                environment='two physical Linux machines', exit_code=125)
                remote, remote_error = None, None
                try:
                    remote_error = (local / 'server.log').open('w')
                    remote = subprocess.Popen(ssh+[remote_cmd], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                              stderr=remote_error, text=True)
                    ready = read_remote_ready(remote)
                    if ready['machine_id'] == identity():
                        raise RuntimeError('Same machine-id on A and B; refuse single-machine benchmark')
                    metadata['clock_a'] = clock_status()
                    metadata['clock_b'] = ready['clock']
                    if args.scenario == 's2':
                        metadata['chrony_reported_offset_sum_s'] = validate_clocks(metadata['clock_a'],metadata['clock_b'])
                    condition = ['delay', f'{level}ms'] if args.scenario=='s1' else ['loss', f'{level}%']
                    command(['sudo','-n','tc','qdisc','replace','dev',args.interface,'root','handle','1:','netem',*condition])
                    active_qdisc = True
                    metadata['qdisc'] = command(['tc','-s','qdisc','show','dev',args.interface])
                    probe = subprocess.run(['ping','-n','-c','10' if args.scenario=='s1' else '100','-i','.2','-W','2',args.broker], capture_output=True,text=True)
                    (local/'ping.txt').write_text(probe.stdout+probe.stderr)
                    with (local/f'{run}.jsonl').open('w') as log, (local/'publisher.stderr').open('w') as err:
                        count = 0 if args.scenario=='s1' else args.count
                        result = subprocess.run([str(ROOT/'pub/bench-pub'),args.transport,args.broker,run,
                            str(count),str(args.period_ms),'10'],stdout=log,stderr=err,
                            env=dict(env,SSLKEYLOGFILE=str(local/f'{run}.keys.log')),
                            timeout=15 if not count else 35+count*args.period_ms/1000)
                        metadata['exit_code'] = result.returncode
                except subprocess.TimeoutExpired:
                    metadata.update(exit_code=124,error='Publisher outer deadline exceeded')
                except Exception as exc:
                    metadata['error'] = str(exc)
                    raise
                finally:
                    # Even a failed publisher must leave the receiver observation
                    # window open through ten seconds after the final send.
                    if args.scenario == 's2':
                        client_log = local/f'{run}.jsonl'
                        if client_log.exists():
                            stamps=[]
                            for line in client_log.read_text().splitlines():
                                try:
                                    event=json.loads(line)
                                    if event.get('event')=='send':
                                        stamps.append(event['ts_us'])
                                except (ValueError,KeyError):
                                    pass
                            if stamps:
                                remaining=(max(stamps)+10000000)/1000000-time.time()
                                if 0<remaining<=11:
                                    time.sleep(remaining)
                    metadata['clock_a_end'] = clock_status()
                    if remote is not None:
                        if remote.poll() is None:
                            try:
                                remote.communicate('STOP\n', timeout=75)
                            except (subprocess.TimeoutExpired, BrokenPipeError):
                                remote.kill()
                                remote.communicate()
                        metadata['receiver_exit_code'] = remote.returncode
                        if remote.returncode != 0:
                            metadata['exit_code'] = 125
                        try:
                            copy = subprocess.run(['scp','-q','-r','-o','BatchMode=yes',
                                f'{args.ssh}:{args.remote_dir}/{remote_rel}/.',str(local)],capture_output=True,text=True,timeout=45)
                            metadata['results_copy_exit_code'] = copy.returncode
                            if copy.returncode:
                                metadata.update(exit_code=125, copy_error=copy.stderr)
                        except subprocess.TimeoutExpired:
                            metadata.update(exit_code=125,results_copy_exit_code=124,
                                            copy_error='SCP timed out; original receiver files remain on machine B')
                    if remote_error:
                        remote_error.close()
                    write_json(local/f'{run}.meta.json',metadata)
                print(run, 'warmup' if repeat==0 else 'measured',metadata['exit_code'],flush=True)
                time.sleep(2)
    finally:
        stop.set()
        keepalive.join(timeout=2)
        if active_qdisc:
            subprocess.run(['sudo','-n','tc','qdisc','del','dev',args.interface,'root'],check=True)
    print('Results:',out)


if __name__=='__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Stopped. Keep results; verify NetEm cleanup on the experiment interface.',file=sys.stderr)
        sys.exit(130)
    except Exception as error:
        print(f'ERROR: {error}. See per-run server.log/publisher.stderr and metadata.',file=sys.stderr)
        sys.exit(1)
