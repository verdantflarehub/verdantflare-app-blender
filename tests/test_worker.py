import http.server
import importlib.util
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
import tempfile
import time
import urllib.error
import urllib.request

spec = importlib.util.spec_from_file_location("instance_control", Path(__file__).parents[1] / "runtime/instance-control/server.py")
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)


class Adapter:
    def __init__(self):
        self.calls = []
        self.startup = None

    def call(self, operation, args, **context):
        if context.get("generation") not in (None, "generation-a"):
            raise control.bridge.BridgeError("STALE_GENERATION", "changed", status=409)
        self.calls.append((operation, args, context))
        return {"ok": True, "generation": "generation-a", "scene_version": 0, "startup": self.startup}


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
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        env = patch.dict(control.rtc.os.environ, BLENDER_RTC_FILE=str(Path(temporary.name) / 'rtc.json'))
        env.start()
        self.addCleanup(env.stop)
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
            self.assertEqual(send("/internal/status", token="wrong")[0], 401)
            with patch.dict(control.os.environ, BLENDER_POD_UID="pod-uid", BLENDER_POD_NAME="pod-a",
                            BLENDER_POD_NAMESPACE="fixture", BLENDER_CONTAINER_NAME="blender",
                            BLENDER_GPU_UUID="GPU-12345678-1234-4234-8234-123456789abc"):
                code, observed = send("/internal/status")
            self.assertEqual(code, 200)
            self.assertEqual(observed["resource_binding"], {"pod_uid":"pod-uid", "pod_name":"pod-a", "namespace":"fixture", "container":"blender", "gpu_uuid":"GPU-12345678-1234-4234-8234-123456789abc"})
            self.assertIsNone(observed["startup"])  # Legacy worker has no managed-load proof.
            self.assertNotIn("resource_binding", send("/readyz")[1])
            self.assertEqual(observed["instance_id"], "instance-a")
            self.assertFalse(observed["gui_held"])
            calls_before_rejections = len(adapter.calls)
            request = {"instance_id": "instance-a", "generation": "generation-a", "name": "scene.get", "arguments": {}, "request_id": "test"}
            self.assertEqual(send("/mcp", request)[0], 404)
            self.assertEqual(send("/internal/rpc", request, token="wrong")[0], 401)
            self.assertEqual(send("/internal/rpc", dict(request, instance_id="instance-b"))[0], 403)
            self.assertEqual(send("/internal/rpc", dict(request, generation="old"))[0], 409)
            self.assertEqual(send("/internal/rpc", dict(request, name="execute_python"))[0], 400)
            self.assertEqual(len(adapter.calls), calls_before_rejections)
            self.assertEqual(send("/internal/rpc", request)[0], 200)
            adapter.startup = {"pod_uid": "private-startup-proof"}
            self.assertNotIn("startup", send("/internal/rpc", request)[1])
            self.assertEqual(adapter.calls[-1][2]["generation"], "generation-a")
            self.assertEqual(send("/internal/status")[1]["startup"], adapter.startup)
            restore = {"instance_id": "instance-a", "generation": "generation-a", "restore_id": "12345678-1234-4234-8234-123456789abc",
                "asset_id": "inbox/" + "a" * 16 + "/restore.blend", "sha256": "b" * 64, "size": 1024}
            self.assertEqual(send("/internal/restore", restore, token="wrong")[0], 401)
            self.assertEqual(send("/internal/restore", dict(restore, asset_id="../../other.blend"))[0], 400)
            self.assertEqual(send("/internal/restore", dict(restore, instance_id="instance-b"))[0], 400)
            self.assertEqual(send("/internal/rpc", dict(request, name="scene.restore"))[0], 400)
            with patch.object(control.project_check, "check") as check:
                self.assertEqual(send("/internal/restore", restore)[0], 200)
                check.assert_called_once_with(restore)
            self.assertEqual(adapter.calls[-1][0], "scene.restore")
            before = sum(c[0] == "scene.restore" for c in adapter.calls)
            with patch.object(control.project_check, "check", side_effect=control.project_check.ProjectCheckError("PROJECT_EXTERNAL_DEPENDENCIES")):
                self.assertEqual(send("/internal/restore", restore)[1]["code"], "PROJECT_EXTERNAL_DEPENDENCIES")
            self.assertEqual(sum(c[0] == "scene.restore" for c in adapter.calls), before)
            with patch.object(control.project_check, "check") as check:
                self.assertEqual(send("/internal/restore", dict(restore, generation="old"))[1]["code"], "STALE_GENERATION")
                check.assert_not_called()
            lease = {"instance_id": "instance-a", "generation": "generation-a", "action": "open", "session": "s" * 43}
            self.assertEqual(send("/internal/gui", lease, token="wrong")[0], 401)
            self.assertEqual(send("/internal/gui", lease)[0], 400)
            expires = int(time.time()) + 900
            lease['rtc_config'] = {'iceTransportPolicy':'relay', 'expires_at':expires, 'iceServers':[
                {'urls':['turn:192.0.2.10:3478?transport=udp'], 'username':str(expires)+':'+'a'*24, 'credential':'b'*27+'='}]}
            self.assertEqual(send("/internal/gui", lease)[0], 200)
            before = server.gui.expires
            self.assertTrue(send("/internal/status")[1]["gui_held"])
            self.assertEqual(server.gui.expires, before)  # Observation cannot renew.
            server.gui.expires = 0
            self.assertTrue(send("/internal/status")[1]["gui_held"])  # Expiry isn't proof of stopped input.
            server.gui.expires = before
            write = dict(request, name="scene.save", arguments={"scene_version": 0})
            self.assertEqual(send("/internal/rpc", write)[1]["code"], "GUI_EDIT_LEASE_HELD")
            with patch.object(control.project_check, "check") as check:
                self.assertEqual(send("/internal/restore", restore)[1]["code"], "GUI_EDIT_LEASE_HELD")
                check.assert_not_called()
            self.assertEqual(send("/internal/rpc", request)[0], 200)
            self.assertEqual(send("/internal/gui", {k:v for k,v in dict(lease, action="close").items() if k != 'rtc_config'})[0], 200)
            self.assertEqual(send("/internal/rpc", write)[0], 200)
        finally:
            server.shutdown()
            server.server_close()
            state.jobs.shutdown()


if __name__ == "__main__":
    unittest.main()
