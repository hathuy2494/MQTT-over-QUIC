"""Manual MQTT/TCP subscriber to EMQX; formal measurement uses collector.py."""
import argparse
import subprocess


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host', default='127.0.0.1')
    a = p.parse_args()
    try:
        subprocess.run(['mosquitto_sub', '-h', a.host, '-p', '1883', '-V', 'mqttv311',
                        '-t', 'quic1/check', '-q', '1', '-v'], check=True)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
