#!/usr/bin/env python3
"""Private execution interface. This worker deliberately has no MCP endpoint."""
from __future__ import annotations

import http.server
import importlib.util
import json
import os
from pathlib import Path
import sys
import re
import threading
import uuid

spec = importlib.util.spec_from_file_location("blender_bridge", Path(__file__).parents[1] / "mcp-bridge/bridge.py")
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)
gui_spec = importlib.util.spec_from_file_location("blender_gui_control", Path(__file__).with_name("gui_control.py"))
gui = importlib.util.module_from_spec(gui_spec)
gui_spec.loader.exec_module(gui)

rtc_spec = importlib.util.spec_from_file_location("blender_rtc", Path(__file__).with_name("rtc.py"))
rtc = importlib.util.module_from_spec(rtc_spec)
rtc_spec.loader.exec_module(rtc)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never log client paths, headers or arguments.

    def setup(self):
        super().setup()
        self.connection.settimeout(35)

    def reply(self, status, value):
        data = json.dumps(value, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/healthz":
            return self.reply(200, {"status": "ok"})
        if self.path == "/readyz":
            try:
                result = self.server.state.adapter.call("scene.get", {}, request_id=str(uuid.uuid4()), scene_version=None, deadline_ms=2000)
                return self.reply(200, {"status": "ready", "instance_id": self.server.state.instance_id, "generation": result["generation"]})
            except (bridge.BridgeError, KeyError):
                return self.reply(503, {"status": "not_ready"})
        self.reply(404, {"code": "NOT_FOUND"})

    def do_POST(self):
        if self.path not in {"/internal/rpc", "/internal/gui"}:
            return self.reply(404, {"code": "NOT_FOUND"})
        state = self.server.state
        if len(self.headers.get_all("Authorization", [])) != 1 or not state.authorized(self.headers.get("Authorization")):
            return self.reply(401, {"code": "UNAUTHENTICATED"})
        try:
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                raise ValueError()
            length = int(self.headers.get("Content-Length", "-1"))
            if not 0 < length <= state.max_body_bytes:
                raise ValueError()
            request = json.loads(self.rfile.read(length))
            if self.path == "/internal/gui":
                fields = {"instance_id", "generation", "action", "session"}
                if isinstance(request, dict) and request.get("action") == "open":
                    fields.add("rtc_config")
                if not isinstance(request, dict) or set(request) != fields or request["instance_id"] != state.instance_id or not isinstance(request["session"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", request["session"]):
                    raise ValueError()
                if request["action"] != "close":
                    state.adapter.call("scene.get", {}, request_id=str(uuid.uuid4()), generation=request["generation"], scene_version=None, deadline_ms=2000)
                with self.server.gui.lock:
                    if request['action'] == 'open':
                        self.server.gui.guard_write()
                        expires = rtc.install(request['rtc_config'])
                        self.server.gui.deadline = self.server.gui.clock() + max(0, expires - __import__('time').time() - 30)
                    self.server.gui.action(request["action"], request["session"])
                return self.reply(200, {"ok": True})
            if not isinstance(request, dict) or set(request) != {"instance_id", "generation", "name", "arguments", "request_id"}:
                raise ValueError()
            if request["instance_id"] != state.instance_id:
                return self.reply(403, {"code": "INSTANCE_MISMATCH"})
            if not isinstance(request["generation"], str) or not request["generation"]:
                raise ValueError()
            tool = bridge.TOOLS.get(request["name"])
            args = request["arguments"]
            if tool is None or tool.name.startswith("job.") or not isinstance(args, dict):
                raise ValueError()
            if set(args) - (set(tool.properties) | {"scene_version"}) or any(k not in args for k in tool.required):
                raise ValueError()
            if tool.mutating and (type(args.get("scene_version")) is not int or args["scene_version"] < 0):
                raise ValueError()
            with self.server.gui.lock:
                if tool.mutating or tool.name == "asset.export":
                    self.server.gui.guard_write()
                result = state.adapter.call(tool.operation, {k: v for k, v in args.items() if k != "scene_version"},
                                            request_id=str(request["request_id"]), scene_version=args.get("scene_version"),
                                            generation=request["generation"], deadline_ms=25000)
            self.reply(200, result)
        except (ValueError, KeyError, TypeError):
            self.reply(400, {"code": "INVALID_ARGUMENT"})
        except bridge.BridgeError as exc:
            self.reply(exc.status, {"code": exc.code, "message": exc.message})
        except (gui.LeaseError, OSError, gui.subprocess.TimeoutExpired) as exc:
            self.reply(409, {"code": str(exc) if isinstance(exc, gui.LeaseError) else "GUI_PROCESS_UNAVAILABLE"})


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, state):
        self.state = state
        self.gui = gui.GUILease()
        super().__init__(address, Handler)


if __name__ == "__main__":
    state = bridge.build_state_from_env()
    server = Server((os.environ.get("BLENDER_MCP_LISTEN_HOST", "0.0.0.0"), int(os.environ.get("BLENDER_MCP_PORT", "48084"))), state)
    gui.supervise("stop")  # Control-process restart must not orphan a live input stream.
    threading.Thread(target=server.gui.watchdog, daemon=True).start()
    server.serve_forever()
