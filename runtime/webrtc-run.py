#!/usr/bin/env python3
"""Start the pinned media runtime with a lease-issued RTC file and private logs."""
import asyncio
import logging
import os
from pathlib import Path
import re
import sys
import json
import time
import urllib.parse

path = Path(os.environ.get('BLENDER_RTC_FILE', '/run/user/1000/blender-rtc.json'))
input_stamp_path = Path(os.environ.get('BLENDER_GUI_INPUT_STAMP_FILE', '/run/user/1000/blender-gui-last-input'))
_last_input_touch = 0.0


def _touch_input_stamp():
    global _last_input_touch
    now = time.monotonic()
    if now - _last_input_touch < 1.0:
        return
    _last_input_touch = now
    try:
        input_stamp_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = input_stamp_path.with_suffix('.tmp')
        tmp.write_text(f'{now:.3f}\n', encoding='utf-8')
        tmp.replace(input_stamp_path)
    except OSError:
        pass
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
for _hook_name in ('on_data_channel_message', 'handle_input_message', 'on_input_message'):
    if hasattr(runtime.WebRTCEngine, _hook_name):
        _orig_hook = getattr(runtime.WebRTCEngine, _hook_name)

        def _make_wrapped_input_hook(orig):
            def _wrapped(self, *args, **kwargs):
                _touch_input_stamp()
                return orig(self, *args, **kwargs)
            return _wrapped

        setattr(runtime.WebRTCEngine, _hook_name, _make_wrapped_input_hook(_orig_hook))
runtime.main()
