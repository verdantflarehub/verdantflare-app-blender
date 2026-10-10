#!/usr/bin/env python3
"""Start the pinned media runtime with a lease-issued RTC file and private logs."""
import asyncio
import logging
import os
from pathlib import Path
import re
import sys
import json
import urllib.parse

path = Path(os.environ.get('BLENDER_RTC_FILE', '/run/user/1000/blender-rtc.json'))
if not path.is_file():
    sys.exit('GUI RTC configuration is required')
os.environ.update(BDWIND_RENDER_ENGINE='wayland', BDWIND_CAPTURE_SOURCE='smithay-rtp',
                  BDWIND_ENABLE_RESIZE='false', BDWIND_RTC_CONFIG_JSON=str(path), BDWIND_TURN_HOST='',
                  BDWIND_TURN_SHARED_SECRET='', BDWIND_TURN_USERNAME='', BDWIND_TURN_PASSWORD='',
                  BDWIND_TURN_REST_URI='', BDWIND_STUN_HOST='', BDWIND_ICE_TRANSPORT_POLICY='relay')
os.environ.setdefault('BDWIND_PORT_GSTREAMER', os.environ.get('BDWIND_PORT_NGINX', '48083'))
factory = logging.getLogRecordFactory()


def private_record(*args, **kwargs):
    record = factory(*args, **kwargs)
    record.msg = re.sub(r'(turns?://)[^\s@]+@', r'\1[redacted]@', record.getMessage())
    record.args = ()
    return record


logging.setLogRecordFactory(private_record)
asyncio.set_event_loop(asyncio.new_event_loop())
from bdwind_gstreamer import __main__ as runtime


def parse_config(data):
    config = json.loads(data)
    turns = []
    for server in config['iceServers']:
        for url in server['urls']:
            if not url.startswith('turn:'):
                raise ValueError('Only configured TURN relays are allowed')
            user = urllib.parse.quote(server['username'], safe='')
            password = urllib.parse.quote(server['credential'], safe='')
            turns.append(f'turn://{user}:{password}@{url[5:]}')
    return [], turns, data


runtime.parse_rtc_config = parse_config
build_pipeline = runtime.WebRTCEngine.build_webrtcbin_pipeline


def relay_pipeline(self, *args, **kwargs):
    result = build_pipeline(self, *args, **kwargs)
    # GST_WEBRTC_ICE_TRANSPORT_POLICY_RELAY, before the pipeline starts gathering.
    self.webrtcbin.set_property('ice-transport-policy', 1)
    return result


runtime.WebRTCEngine.build_webrtcbin_pipeline = relay_pipeline
runtime.main()
