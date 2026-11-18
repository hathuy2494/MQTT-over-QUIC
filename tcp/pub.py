"""Manual MQTT/TCP publisher to the SAME EMQX broker; functional check only."""
import argparse
import subprocess


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host', required=True)
    p.add_argument('--message', default='hello from QUIC1')
    a = p.parse_args()
    subprocess.run(['mosquitto_pub', '-h', a.host, '-p', '1883', '-V', 'mqttv311',
                    '-t', 'quic1/check', '-q', '0', '-m', a.message], check=True)


if __name__ == '__main__':
    main()
