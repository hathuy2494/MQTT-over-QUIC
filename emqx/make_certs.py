"""Create one lab CA and one server certificate shared by TLS and QUIC."""
import argparse
import ipaddress
from pathlib import Path
import subprocess


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--broker-ip', required=True, type=ipaddress.IPv4Address)
    a = p.parse_args()
    root = Path(__file__).resolve().parent / 'certs'
    root.mkdir(exist_ok=True)
    if any((root / n).exists() for n in ['ca.key', 'ca.crt', 'server.key', 'server.crt']):
        p.error('certs already exist; keep them for repeatable measurements. Use a new folder to rotate.')
    ext = root / 'server.ext'
    ext.write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n'
                   'extendedKeyUsage=serverAuth\n'
                   f'subjectAltName=DNS:emqx.local,IP:{a.broker_ip},IP:127.0.0.1\n', encoding='ascii')
    commands = [
        ['openssl', 'ecparam', '-name', 'prime256v1', '-genkey', '-noout', '-out', 'ca.key'],
        ['openssl', 'req', '-new', '-x509', '-key', 'ca.key', '-sha256', '-days', '365',
         '-subj', '/CN=QUIC1 Lab CA', '-addext', 'basicConstraints=critical,CA:TRUE',
         '-addext', 'keyUsage=critical,keyCertSign,cRLSign', '-out', 'ca.crt'],
        ['openssl', 'ecparam', '-name', 'prime256v1', '-genkey', '-noout', '-out', 'server.key'],
        ['openssl', 'req', '-new', '-key', 'server.key', '-subj', '/CN=emqx.local', '-out', 'server.csr'],
        ['openssl', 'x509', '-req', '-in', 'server.csr', '-CA', 'ca.crt', '-CAkey', 'ca.key',
         '-CAcreateserial', '-days', '365', '-sha256', '-extfile', 'server.ext', '-out', 'server.crt'],
        ['openssl', 'verify', '-CAfile', 'ca.crt', 'server.crt']]
    for command in commands:
        subprocess.run(command, cwd=root, check=True)
    (root / 'ca.key').chmod(0o600)
    # Lab broker runs as an unprivileged image user; certs directory is read-only in it.
    (root / 'server.key').chmod(0o644)
    print('Created', root, '\nCopy ONLY ca.crt to machine A. Keep ca.key and server.key on B.')


if __name__ == '__main__':
    main()
