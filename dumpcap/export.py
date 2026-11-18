"""Export Wireshark frame evidence; never equate a CONNACK frame number with N_pkt."""
import argparse
import csv
import ipaddress
import json
from pathlib import Path
import subprocess


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('pcap', type=Path)
    p.add_argument('--publisher-ip', required=True, type=ipaddress.IPv4Address)
    p.add_argument('--transport', choices=['tcp', 'tls', 'quic'], required=True)
    p.add_argument('--keylog', type=Path)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--first-frame', type=int, help='first frame of this connection in this capture')
    p.add_argument('--last-frame', type=int, help='frame containing successful CONNACK')
    a = p.parse_args()
    if bool(a.first_frame) != bool(a.last_frame):
        p.error('first-frame and last-frame must be provided together')
    if a.first_frame is not None and (a.first_frame < 1 or a.last_frame < a.first_frame):
        p.error('invalid frame interval')
    port = {'tcp':'tcp.port == 1883', 'tls':'tcp.port == 8883', 'quic':'udp.port == 14567'}[a.transport]
    display = f'ip.addr == {a.publisher_ip} && ({port})'
    if a.first_frame:
        display += f' && frame.number >= {a.first_frame} && frame.number <= {a.last_frame}'
    command = ['tshark', '-2', '-r', str(a.pcap)]
    if a.keylog:
        command += ['-o', f'tls.keylog_file:{a.keylog.resolve()}']
    command += ['-Y', display, '-T', 'fields', '-E', 'header=y', '-E', 'separator=,', '-E', 'quote=d']
    fields = ['frame.number', 'frame.time_epoch', 'frame.len', 'ip.src', 'ip.dst',
              'tcp.stream', 'tcp.flags.syn', 'mqtt.msgtype', 'tcp.analysis.retransmission']
    for field in fields:
        command += ['-e', field]
    result = subprocess.run(command, check=True, capture_output=True, text=True, encoding='utf-8')
    a.output.mkdir(parents=True, exist_ok=True)
    (a.output / 'frames.csv').write_text(result.stdout, encoding='utf-8')
    rows = list(csv.DictReader(result.stdout.splitlines()))
    summary = dict(display_filter=display, selected_frames=len(rows),
                   selected_frame_bytes=sum(int(r['frame.len']) for r in rows),
                   tcp_retransmission_frames=sum(bool(r['tcp.analysis.retransmission']) for r in rows),
                   n_pkt_to_connack=len(rows) if a.first_frame else None,
                   boundary_method='user verified connection start and successful CONNACK frame' if a.first_frame else 'not established',
                   note='Counts captured frames; NIC offloading may aggregate segments. A UDP datagram can contain multiple QUIC packets. '
                        'Encrypted CONNACK requires compatible keylog and dissector. Do not infer N_pkt from frame number.')
    (a.output / 'capture_summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
