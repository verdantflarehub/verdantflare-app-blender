import http.server
import importlib.util
import json
from pathlib import Path
import threading
import unittest
import urllib.error
import urllib.request

spec = importlib.util.spec_from_file_location("instance_control", Path(__file__).parents[1] / "runtime/instance-control/server.py")
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)


class Adapter:
    def __init__(self):
        self.calls = []

    def call(self, operation, args, **context):
        if context.get("generation") not in (None, "generation-a"):
            raise control.bridge.BridgeError("STALE_GENERATION", "changed", status=409)
        self.calls.append((operation, args, context))
        return {"ok": True, "generation": "generation-a", "scene_version": 0}


class WorkerTests(unittest.TestCase):
    def test_gui_expiry_and_failed_stop_keep_rpc_writers_fenced(self):
        clock, actions = [0], []
        fail = [False]
        def run(action):
            actions.append(action)
            if fail[0]:
                raise control.gui.LeaseError("GUI_PROCESS_UNAVAILABLE")
        lease = control.gui.GUILease(run, lambda: clock[0])
        lease.action("open", "session-a")
        with self.assertRaisesRegex(control.gui.LeaseError, "GUI_EDIT_LEASE_HELD"):
            lease.guard_write()
        with self.assertRaisesRegex(control.gui.LeaseError, "GUI_LEASE_HELD"):
            lease.action("open", "session-b")
        clock[0] = 21
        fail[0] = True
        with self.assertRaisesRegex(control.gui.LeaseError, "GUI_PROCESS_UNAVAILABLE"):
            lease.guard_write()
        self.assertEqual(lease.session, "session-a")
        fail[0] = False
        lease.guard_write()
        self.assertIsNone(lease.session)
        with self.assertRaisesRegex(control.gui.LeaseError, "GUI_LEASE_EXPIRED"):
            lease.action("heartbeat", "session-a")
        lease.action("open", "session-b")
        lease.action("close", "session-b")
        lease.guard_write()
        self.assertEqual(actions[-2:], ["start", "stop"])

    def test_internal_only_identity_validation_and_probe(self):
        adapter = Adapter()
        state = control.bridge.BridgeState("instance-a", "worker-test-only", adapter)
        server = control.Server(("127.0.0.1", 0), state)
        server.gui.run = lambda action: None
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"

        def send(path, value=None, token="worker-test-only"):
            req = urllib.request.Request(base+path, data=None if value is None else json.dumps(value).encode(), headers={"Authorization": "Bearer "+token, "Content-Type": "application/json"})
            try:
                response = urllib.request.urlopen(req)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                return response.status, json.loads(response.read())

        try:
            self.assertEqual(send("/readyz")[0], 200)
            self.assertEqual(adapter.calls[0][0], "scene.get")  # Probe executes bpy, not just stat(socket).
            request = {"instance_id": "instance-a", "generation": "generation-a", "name": "scene.get", "arguments": {}, "request_id": "test"}
            self.assertEqual(send("/mcp", request)[0], 404)
            self.assertEqual(send("/internal/rpc", request, token="wrong")[0], 401)
            self.assertEqual(send("/internal/rpc", dict(request, instance_id="instance-b"))[0], 403)
            self.assertEqual(send("/internal/rpc", dict(request, generation="old"))[0], 409)
            self.assertEqual(send("/internal/rpc", dict(request, name="execute_python"))[0], 400)
            self.assertEqual(len(adapter.calls), 1)
            self.assertEqual(send("/internal/rpc", request)[0], 200)
            self.assertEqual(adapter.calls[-1][2]["generation"], "generation-a")
            lease = {"instance_id": "instance-a", "generation": "generation-a", "action": "open", "session": "s" * 43}
            self.assertEqual(send("/internal/gui", lease, token="wrong")[0], 401)
            self.assertEqual(send("/internal/gui", lease)[0], 200)
            write = dict(request, name="scene.save", arguments={"scene_version": 0})
            self.assertEqual(send("/internal/rpc", write)[1]["code"], "GUI_EDIT_LEASE_HELD")
            self.assertEqual(send("/internal/rpc", request)[0], 200)
            self.assertEqual(send("/internal/gui", dict(lease, action="close"))[0], 200)
            self.assertEqual(send("/internal/rpc", write)[0], 200)
        finally:
            server.shutdown()
            server.server_close()
            state.jobs.shutdown()


if __name__ == "__main__":
    unittest.main()
