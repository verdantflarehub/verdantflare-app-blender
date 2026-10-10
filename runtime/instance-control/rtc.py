"""Install only a short-lived session configuration, never a TURN signing key."""
import json
import os
from pathlib import Path
import re
import time


def install(value):
    if not isinstance(value, dict) or value.get('iceTransportPolicy') != 'relay':
        raise ValueError('INVALID_RTC_CONFIG')
    expires = value.get('expires_at')
    if type(expires) is not int or not time.time() + 30 < expires <= time.time() + 900:
        raise ValueError('INVALID_RTC_EXPIRY')
    servers = value.get('iceServers')
    if not isinstance(servers, list) or len(servers) != 1:
        raise ValueError('INVALID_RTC_CONFIG')
    server = servers[0]
    if (not isinstance(server, dict) or set(server) != {'urls', 'username', 'credential'}
            or not isinstance(server['urls'], list) or not 1 <= len(server['urls']) <= 2
            or not all(isinstance(u, str) and re.fullmatch(r'turn:[A-Za-z0-9.-]+:[0-9]{1,5}\?transport=(udp|tcp)', u) for u in server['urls'])
            or not re.fullmatch(str(expires) + r':[a-f0-9]{24}', server.get('username', ''))
            or not re.fullmatch(r'[A-Za-z0-9+/]{27}=', server.get('credential', ''))):
        raise ValueError('INVALID_RTC_CONFIG')
    path = Path(os.environ.get('BLENDER_RTC_FILE', '/run/user/1000/blender-rtc.json'))
    fd = os.open(path.with_suffix('.tmp'), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream)
    os.replace(path.with_suffix('.tmp'), path)
    return expires
