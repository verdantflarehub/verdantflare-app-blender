import base64
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('turn', Path(__file__).parents[1] / 'app/turn.py')
turn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(turn)


class TurnTests(unittest.TestCase):
    def test_signature_scope_expiry_and_no_public_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'auth.conf'
            secret = 'fixture-signing-secret-' * 3
            path.write_text('static-auth-secret=' + secret)
            with patch.dict(os.environ, BLENDER_TURN_HOST='192.0.2.10', BLENDER_TURN_SECRET_FILE=str(path)):
                config = turn.configuration('worker:session-A', now=1000)
                server = config['iceServers'][0]
                self.assertEqual(config['expires_at'], 1900)
                self.assertTrue(server['username'].startswith('1900:'))
                self.assertEqual(server['credential'], base64.b64encode(hmac.new(secret.encode(), server['username'].encode(), hashlib.sha1).digest()).decode())
                self.assertNotIn(secret, json.dumps(config))
                self.assertNotIn('session-A', json.dumps(config))
                self.assertEqual(config['iceTransportPolicy'], 'relay')
                self.assertNotEqual(server['username'], turn.configuration('browser:session-A', now=1000)['iceServers'][0]['username'])
            with patch.dict(os.environ, BLENDER_TURN_HOST=''):
                with self.assertRaises(ValueError): turn.configuration('session-A')


if __name__ == '__main__': unittest.main()
