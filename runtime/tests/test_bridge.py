from __future__ import annotations

import importlib.util
import json
import socketserver
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("blender_mcp_bridge", ROOT / "mcp-bridge" / "bridge.py")
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bridge
SPEC.loader.exec_module(bridge)


class FakeAdapter(socketserver.StreamRequestHandler):
    def handle(self):
        request = json.loads(self.rfile.readline())
        operation = request["operation"]
        if request.get("scene_version") not in (None, 0):
            response = {"ok": False, "error": {"code": "SCENE_VERSION_CONFLICT", "message": "stale"}}
        elif operation == "scene.get":
            response = {"ok": True, "scene_version": 0, "scene": {"name": "Scene"}}
        elif operation == "object.create":
            response = {"ok": True, "scene_version": 1, "object": {"object_id": request["args"]["object_id"]}}
        else:
            response = {"ok": True, "scene_version": 0}
        self.wfile.write(json.dumps(response).encode() + b"\n")


class FakeUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.socket_path = str(Path(self.tempdir.name) / "adapter.sock")
        self.adapter_server = FakeUnixServer(self.socket_path, FakeAdapter)
        threading.Thread(target=self.adapter_server.serve_forever, daemon=True).start()
        state = bridge.BridgeState(
            "instance-1", "token-1", bridge.AdapterClient(self.socket_path), request_timeout_ms=1000
        )
        self.http = bridge.BridgeHTTPServer(("127.0.0.1", 0), state)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.http.server_address[1]}"

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.adapter_server.shutdown()
        self.adapter_server.server_close()
        Path(self.socket_path).unlink(missing_ok=True)
        self.tempdir.cleanup()

    def request(self, payload, token="token-1"):
        body = json.dumps(payload).encode()
        request = urllib.request.Request(
            self.url + "/mcp", data=body, method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read())

    def test_health_and_readiness(self):
        with urllib.request.urlopen(self.url + "/healthz") as response:
            self.assertEqual(response.status, 200)
        with urllib.request.urlopen(self.url + "/readyz") as response:
            self.assertEqual(response.status, 200)

    def test_initialize_and_tools_list(self):
        initialized = self.request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(initialized["result"]["serverInfo"]["version"], "0.1.0")
        listed = self.request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        self.assertIn("scene.get", {tool["name"] for tool in listed["result"]["tools"]})

    def test_tool_call_enforces_instance_and_scene_version(self):
        scene = self.request({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "scene.get", "arguments": {"instance_id": "instance-1"}},
        })
        self.assertEqual(scene["result"]["isError"], False)
        create = self.request({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "object.create", "arguments": {
                "instance_id": "instance-1", "scene_version": 0,
                "object_id": "Cube", "primitive": "cube",
            }},
        })
        self.assertEqual(create["result"]["isError"], False)
        denied = self.request({
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "object.create", "arguments": {
                "instance_id": "other", "scene_version": 0,
                "object_id": "Cube", "primitive": "cube",
            }},
        })
        self.assertEqual(denied["error"]["code"], "AUTH_FORBIDDEN")

    def test_l2_asset_operation_returns_pollable_job(self):
        submitted = self.request({
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "asset.export", "arguments": {
                "instance_id": "instance-1", "asset_id": "exports/smoke.glb", "format": "glb",
            }},
        })
        job_id = json.loads(submitted["result"]["content"][0]["text"])["job_id"]
        for _ in range(20):
            status = self.request({
                "jsonrpc": "2.0", "id": 8, "method": "tools/call",
                "params": {"name": "job.get", "arguments": {"job_id": job_id}},
            })
            state = json.loads(status["result"]["content"][0]["text"])["state"]
            if state == "succeeded":
                break
            time.sleep(0.01)
        self.assertEqual(state, "succeeded")

    def test_invalid_token_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request({"jsonrpc": "2.0", "id": 6, "method": "ping"}, token="wrong")
        self.assertEqual(raised.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
