"""Shared metadata and clock validation for the two-host experiment."""
import json
import os
import platform
import re
import subprocess
from pathlib import Path


def host_info():
    info=dict(hostname=platform.node(),architecture=platform.machine(),
              kernel=platform.release(),python=platform.python_version())
    for key,path in [('os_release','/etc/os-release'),('cpu','/proc/cpuinfo'),('memory','/proc/meminfo')]:
        try:
            text=Path(path).read_text()
            if key=='cpu':
                text='\n'.join(line for line in text.splitlines() if line.startswith(('model name','Hardware','Model')))[:2000]
            elif key=='memory':
                text=text.splitlines()[0]
            info[key]=text
        except OSError:
            info[key]='unavailable'
    return info


def command(argv, **kwargs):
    return subprocess.run(argv, check=True, capture_output=True, text=True,
                          env=dict(os.environ, LC_ALL='C'), **kwargs).stdout


def identity():
    return Path('/etc/machine-id').read_text().strip()


def validate_listeners(raw):
    blocks = dict(re.findall(r'(?ms)^(tcp|ssl|quic):default\n(.*?)(?=^\w+:|\Z)',raw))
    for transport,port in [('tcp',1883),('ssl',8883),('quic',14567)]:
        block=blocks.get(transport,'')
        if not re.search(r'running\s*:\s*true\b',block) or not re.search(r'listen_on\s*:\s*\S*:'+str(port)+r'\b',block):
            raise ValueError(f'EMQX {transport}:{port} is not running')


def clock_status():
    try:
        raw = command(['chronyc', 'tracking'], timeout=5)
        offset = re.search(r'System time\s*:\s*([\d.eE+-]+) seconds', raw)
        leap = re.search(r'Leap status\s*:\s*(.*)', raw)
        reference = re.search(r'Reference ID\s*:\s*(.*)', raw)
        return dict(raw=raw, offset_s=float(offset[1]) if offset else None,
                    synchronized=bool(leap and leap[1].strip() == 'Normal'),
                    reference=reference[1].strip() if reference else '')
    except (OSError, subprocess.SubprocessError) as error:
        return dict(synchronized=False, offset_s=None, error=str(error))


def validate_clocks(a, b):
    for name, status in [('A', a), ('B', b)]:
        if not status.get('synchronized') or status.get('offset_s') is None:
            raise ValueError(f'Clock {name} is not synchronized by chrony')
    # Only a readiness guard. Different references/asymmetry can still bias OWD.
    bound = abs(a['offset_s']) + abs(b['offset_s'])
    if bound >= .001:
        raise ValueError(f'Sum of chrony offsets {bound*1000:.3f} ms is >= 1 ms')
    return bound


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
