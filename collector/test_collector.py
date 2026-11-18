import importlib.util
import json
from pathlib import Path
import unittest
import jsonschema

ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location('quic1_collector', ROOT / 'collector.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CollectorValidationTests(unittest.TestCase):
    def setUp(self):
        self.validator = jsonschema.Draft202012Validator(json.loads((ROOT / 'schema.json').read_text()))

    def payload(self, **changes):
        body = dict(v=1, run_id='S2_tls_l5_r01', device_id='pi01', seq=0,
                    ts_us=1794910000000000, temp=27.5, pad='')
        body.update(changes)
        text = json.dumps(body, separators=(',', ':'))
        body['pad'] = 'x'*(128-len(text.encode()))
        return json.dumps(body, separators=(',', ':')).encode()

    def test_valid_message_timestamp_before_decode(self):
        stamp = 1794910000000123
        row = module.validate_message(self.payload(), 'quic4iot/pi01/data', stamp, self.validator)
        self.assertTrue(row['valid'])
        self.assertEqual(row['latency_us'], 123)
        self.assertEqual(row['bytes'], 128)

    def test_invalid_messages_are_kept_and_explained(self):
        for payload, topic in [(b'not json', 'quic4iot/pi01/data'),
                               (b'[]', 'quic4iot/pi01/data'),
                               (self.payload(seq=-1), 'quic4iot/pi01/data'),
                               (self.payload(), 'quic4iot/other/data'),
                               (self.payload()+b' ', 'quic4iot/pi01/data')]:
            with self.subTest(payload=payload, topic=topic):
                row = module.validate_message(payload, topic, 1794910000000123, self.validator)
                self.assertFalse(row['valid'])
                self.assertTrue(row['error'])


if __name__ == '__main__':
    unittest.main()
