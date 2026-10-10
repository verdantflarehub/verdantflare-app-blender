"""Issue bounded relay credentials only behind the application's GUI lease."""
import base64
import hashlib
import hmac
import os
from pathlib import Path
import re
import time

TTL = 900
RECONNECT_AFTER = 720


def configuration(scope, now=None):
    host = os.environ.get('BLENDER_TURN_HOST', '')
    port = os.environ.get('BLENDER_TURN_PORT', '3478')
    if not re.fullmatch(r'[A-Za-z0-9.-]+', host) or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError('TURN_NOT_CONFIGURED')
    secret = Path(os.environ['BLENDER_TURN_SECRET_FILE']).read_text().strip()
    if not secret.startswith('static-auth-secret=') or '\n' in secret:
        raise ValueError('TURN_NOT_CONFIGURED')
    secret = secret.removeprefix('static-auth-secret=')
    if len(secret) < 32:
        raise ValueError('TURN_NOT_CONFIGURED')
    expires = int(time.time() if now is None else now) + TTL
    username = f'{expires}:{hashlib.sha256(scope.encode()).hexdigest()[:24]}'
    password = base64.b64encode(hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()).decode()
    return {'iceTransportPolicy': 'relay', 'lifetimeDuration': f'{TTL}s', 'expires_at': expires,
            'iceServers': [{'urls': [f'turn:{host}:{port}?transport=udp', f'turn:{host}:{port}?transport=tcp'],
                            'username': username, 'credential': password}]}
