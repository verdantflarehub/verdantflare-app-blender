#!/usr/bin/env python3
"""Private execution interface. This worker deliberately has no MCP endpoint."""
from __future__ import annotations

import http.server
import importlib.util
import json
import os
from pathlib import Path
import sys
import uuid

spec = importlib.util.spec_from_file_location("blender_bridge", Path(__file__).parents[1] / "mcp-bridge/bridge.py")
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)


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
        if self.path != "/internal/rpc":
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
            result = state.adapter.call(tool.operation, {k: v for k, v in args.items() if k != "scene_version"},
                                        request_id=str(request["request_id"]), scene_version=args.get("scene_version"),
                                        generation=request["generation"], deadline_ms=25000)
            self.reply(200, result)
        except (ValueError, KeyError, TypeError):
            self.reply(400, {"code": "INVALID_ARGUMENT"})
        except bridge.BridgeError as exc:
            self.reply(exc.status, {"code": exc.code, "message": exc.message})


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, state):
        self.state = state
        super().__init__(address, Handler)


if __name__ == "__main__":
    state = bridge.build_state_from_env()
    Server((os.environ.get("BLENDER_MCP_LISTEN_HOST", "0.0.0.0"), int(os.environ.get("BLENDER_MCP_PORT", "48084"))), state).serve_forever()
