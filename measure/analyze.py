"""Analyze an explicitly selected two-machine result batch; never mix old labs."""
import argparse
import csv
import json
import statistics
from pathlib import Path
from common import validate_clocks


def percentile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered)-1)*p
    low = int(pos)
    high = min(low+1,len(ordered)-1)
    return ordered[low]+(ordered[high]-ordered[low])*(pos-low)


def analyze(root):
    rows = []
    for path in sorted(root.rglob('*.meta.json')):
        meta = json.loads(path.read_text())
        if 'scenario' not in meta or meta.get('warmup'):
            continue
        run = meta['run_id']
        log = path.parent/f'{run}.jsonl'
        events, parse_error = [], False
        if log.exists():
            for line in log.read_text().splitlines():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    parse_error = True
        conn = next((e for e in events if e.get('event')=='connect'),{})
        row = dict(run_id=run,scenario=meta['scenario'],transport=meta['transport'],
                   level=meta['level'],success=bool(conn.get('success')) and meta['exit_code']==0,
                   exit_code=meta['exit_code'],log_parse_error=parse_error,
                   connect_to_connack_us=conn.get('connect_to_connack_us') if conn.get('success') else None,
                   error=meta.get('error',''))
        if parse_error:
            row['success'] = False
        collector_meta=path.parent/'collector.meta.json'
        if collector_meta.exists():
            receiver=json.loads(collector_meta.read_text())
            row.update(collector_transport=receiver['transport'],
                collector_connect_to_connack_us=receiver.get('connect_to_connack_us'),
                collector_connect_to_suback_us=receiver.get('connect_to_suback_us'))
        if meta['scenario']=='s2':
            clock_valid, clock_error = True, ''
            try:
                validate_clocks(meta['clock_a'],meta['clock_b'])
                receiver = json.loads((path.parent/'receiver_end.json').read_text())
                validate_clocks(meta['clock_a_end'],receiver['clock'])
            except (ValueError,KeyError,OSError) as exc:
                clock_valid, clock_error = False,str(exc)
            sends = [e for e in events if e.get('event')=='send']
            sent = {e['seq']:e for e in sends if e.get('rc')==0}
            deadline = max((e['ts_us'] for e in sends),default=0)+10000000
            source = path.parent/f'{run}.csv'
            records = []
            if source.exists():
                with source.open(newline='',encoding='utf-8') as received_file:
                    records = list(csv.DictReader(received_file))
            seen, latencies = set(), []
            invalid=late=duplicates=out_of_order=negative=0
            last=-1
            for item in records:
                try:
                    seq,ts,recv=int(item['seq']),int(item['ts_us']),int(item['recv_ts_us'])
                except (ValueError,KeyError):
                    invalid+=1
                    continue
                if item['valid']!='True' or seq not in sent or ts!=sent[seq]['ts_us'] or not 0<=seq<meta['count']:
                    invalid+=1
                    continue
                if recv>deadline:
                    late+=1
                    continue
                if seq in seen:
                    duplicates+=1
                    continue
                out_of_order+=int(seq<last)
                last=max(last,seq)
                seen.add(seq)
                latency=recv-ts
                negative+=int(latency<0)
                latencies.append(latency)
            first=min((e['ts_us'] for e in sends),default=deadline)
            observation=(deadline-first)/1000000
            usable=clock_valid and negative==0
            row.update(planned=meta['count'],period_ms=meta['period_ms'],qos=meta['qos'],
                       attempted=len(sends),accepted=len(sent),unique_received=len(seen),
                       delivery_ratio=len(seen)/meta['count'],missing=meta['count']-len(seen),
                       duplicates=duplicates,out_of_order=out_of_order,invalid=invalid,late=late,
                       clock_valid=clock_valid,clock_error=clock_error,negative_latencies=negative,
                       latency_usable=usable,p50_us=percentile(latencies,.5) if usable else None,
                       p95_us=percentile(latencies,.95) if usable else None,
                       observation_s=observation,
                       goodput_kbps=len(seen)*128*8/observation/1000 if observation>0 and clock_valid else None)
        rows.append(row)
    groups={}
    for row in rows:
        key=tuple(row.get(k) for k in ['scenario','transport','level','planned','period_ms','qos'])
        groups.setdefault(key,[]).append(row)
    summaries=[]
    for key,members in groups.items():
        summary=dict(zip(['scenario','transport','level','planned','period_ms','qos'],key))
        summary.update(attempts=len(members),successes=sum(r['success'] for r in members))
        values=[r['connect_to_connack_us'] for r in members if r['success'] and r['connect_to_connack_us'] is not None]
        summary.update(median_connack_us=statistics.median(values) if values else None,
                       min_connack_us=min(values) if values else None,max_connack_us=max(values) if values else None)
        for field in ['p50_us','p95_us','delivery_ratio','goodput_kbps']:
            samples=[r[field] for r in members if r.get(field) is not None]
            summary['median_run_'+field]=statistics.median(samples) if samples else None
        for field in ['collector_connect_to_connack_us','collector_connect_to_suback_us']:
            samples=[r[field] for r in members if r.get(field) is not None]
            summary['median_'+field]=statistics.median(samples) if samples else None
        summaries.append(summary)
    for name,data in [('runs.csv',rows),('summary.csv',summaries)]:
        if data:
            keys=list(dict.fromkeys(key for row in data for key in row))
            with (root/name).open('w',newline='') as output:
                writer=csv.DictWriter(output,fieldnames=keys)
                writer.writeheader()
                writer.writerows(data)
    return rows


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('batch',type=Path,help='One results batch directory')
    args=p.parse_args()
    print(json.dumps(analyze(args.batch),indent=2))
