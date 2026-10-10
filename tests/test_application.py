import http.server
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import uuid

spec = importlib.util.spec_from_file_location("blender_app", Path(__file__).parents[1] / "app/server.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class ContentFixture:
    def __init__(self, project):
        self.project, self.revision = project, app.content.uuid7()
        self.denied, self.lose_commit = False, False
        self.commits, self.uploads, self.files = {}, {}, []

    def open(self, subject, org, project):
        if self.denied:
            raise app.content.ContentError("PERMISSION_DENIED", 403)
        return {"project_id": project, "revision_id": self.revision, "manifest": {"files": self.files}}

    def upload(self, subject, org, project, write_id, path, sha256, size):
        data = Path(path).read_bytes()
        assert hashlib.sha256(data).hexdigest() == sha256 and len(data) == size
        return self.uploads.setdefault(write_id, {k: app.content.uuid7() for k in ("store_id", "artifact_id", "version_id")})

    def download(self, subject, org, project, revision, file, path):
        self.downloaded_revision = revision
        data = b"BLENDER-v450-restored"
        Path(path).write_bytes(data)
        return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def request(self, subject, org, method, path, value):
        assert path == "/project/commit"
        if value["commit_id"] in self.commits:
            return self.commits[value["commit_id"]]
        if value["expected_revision_id"] != self.revision:
            raise app.content.ContentError("REVISION_CONFLICT", 409)
        file = value["changes"]["upsert_files"][0].copy()
        if self.files:
            assert file["file_id"] == self.files[0]["file_id"]
        else:
            assert "file_id" not in file
            file["file_id"] = app.content.uuid7()
        self.files, self.revision = [file], app.content.uuid7()
        result = {"project_id": self.project, "revision_id": self.revision, "manifest": {"files": self.files}}
        self.commits[value["commit_id"]] = result
        if self.lose_commit:
            self.lose_commit = False
            raise app.content.ContentError("CONTENT_SERVICE_UNAVAILABLE")
        return result


class Worker(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/assets/checkpoints/"):
            data = b"BLENDER-v450" + bytes(range(256)) * 8000
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Asset-SHA256", hashlib.sha256(data).hexdigest())
            self.end_headers()
            self.wfile.write(data)
            return
        self.reply(200, {"instance_id": self.server.instance, "generation": self.server.generation})

    def do_POST(self):
        if self.path == "/upload":
            data = self.rfile.read(int(self.headers["Content-Length"]))
            return self.reply(201, {"asset_id": "inbox/" + "a" * 16 + "/restore.blend", "sha256": hashlib.sha256(data).hexdigest()})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body["instance_id"] != self.server.instance or body["generation"] != self.server.generation:
            return self.reply(409, {"code": "STALE_GENERATION"})
        if self.path == "/internal/gui":
            return self.reply(200, {"ok": True})
        self.server.requests.append(body)
        if self.path == "/internal/restore":
            self.server.restore_id = body["restore_id"]
            self.server.generation = str(uuid.uuid4())
            if getattr(self.server, "lose_restore", False):
                return self.reply(503, {"code": "UNAVAILABLE"})
            return self.reply(200, {"ok": True, "generation": self.server.generation, "restore_id": self.server.restore_id})
        if self.server.fail:
            return self.reply(503, {"code": "UNAVAILABLE"})
        self.reply(200, {"ok": True, "generation": self.server.generation, "restore_id": getattr(self.server, "restore_id", None), "scene_version": len(self.server.requests), "asset_id": "checkpoints/checkpoint-1-" + "a" * 32 + ".blend"})


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.subject, self.viewer, self.org, self.project = [str(uuid.uuid4()) for _ in range(4)]
        self.workers = []
        self.instances = {}
        for alias in ("blenderA", "blenderB"):
            worker = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Worker)
            worker.instance, worker.generation = str(uuid.uuid4()), str(uuid.uuid4())
            worker.requests, worker.fail = [], False
            self.workers.append(worker)
            threading.Thread(target=worker.serve_forever, daemon=True).start()
            os.environ["BLENDER_WORKER_TEST"] = "worker-test-only"
            self.instances[alias] = {"id": worker.instance, "organization_id": self.org, "project_id": self.project,
                "endpoint": f"http://127.0.0.1:{worker.server_port}", "token_env": "BLENDER_WORKER_TEST",
                "grants": {self.subject: "edit", self.viewer: "read"}}
        self.config = Path(self.temp.name) / "instances.json"
        self.save_config()
        self.content = ContentFixture(self.project)
        self.application = app.Application(self.config, str(Path(self.temp.name) / "state.db"), "studio-test-only", self.content)
        self.server = app.Server(("127.0.0.1", 0), self.application)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def save_config(self):
        self.config.write_text(json.dumps({"instances": self.instances}), encoding="utf-8")

    def tearDown(self):
        deadline = time.monotonic() + 5
        while self.application.save_threads and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(self.application.save_threads)
        for server in [self.server, *self.workers]:
            server.shutdown()
            server.server_close()
        self.application.store.db.close()
        self.temp.cleanup()

    def call(self, name, args, alias="blenderA", subject=None):
        return self.application.call(alias, subject or self.subject, self.org, name, args)

    def open(self, alias="blenderA", subject=None, mode="edit"):
        return self.call("session.open", {"project_id": self.project, "mode": mode}, alias, subject)["editing_session_id"]

    def rpc(self, body, alias="blenderA", token="studio-test-only", subject=None, extra=None):
        headers = {"Authorization": "Bearer " + token, "X-User-Id": subject or self.subject, "X-Organization-Id": self.org, "Content-Type": "application/json"}
        headers.update(extra or {})
        request = urllib.request.Request(self.base + "/internal/mcp/" + alias, data=json.dumps(body).encode(), headers=headers)
        try:
            response = urllib.request.urlopen(request)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None

    def test_independent_instances_no_fallback_and_generation(self):
        a, b = self.open(), self.open("blenderB")
        self.call("scene.get", {"editing_session_id": a})
        self.call("scene.get", {"editing_session_id": b}, "blenderB")
        with self.assertRaisesRegex(app.Error, "EDITING_SESSION_INVALID"):
            self.call("scene.get", {"editing_session_id": a}, "blenderB")
        self.workers[0].fail = True
        with self.assertRaisesRegex(app.Error, "WORKER_UNAVAILABLE"):
            self.call("scene.get", {"editing_session_id": a})
        self.assertEqual(len(self.workers[1].requests), 1)
        self.workers[0].generation = str(uuid.uuid4())
        with self.assertRaisesRegex(app.Error, "STALE_GENERATION"):
            self.call("scene.get", {"editing_session_id": a})

    def test_acl_rechecked_after_session_and_per_instance(self):
        a = self.open()
        del self.instances["blenderB"]["grants"][self.subject]
        self.save_config()
        status, _ = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, alias="blenderB")
        self.assertEqual(status, 404)
        del self.instances["blenderA"]["grants"][self.subject]
        self.save_config()
        with self.assertRaisesRegex(app.Error, "INSTANCE_NOT_FOUND"):
            self.call("scene.get", {"editing_session_id": a})
        self.assertEqual(self.workers[0].requests, [])

    def test_wrong_identity_and_token_rejected(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        self.assertEqual(self.rpc(request, token="wrong")[0], 401)
        self.assertEqual(self.rpc(request, subject=str(uuid.uuid4()))[0], 404)
        self.assertEqual(self.rpc(request, extra={"X-Organization-Id": str(uuid.uuid4())})[0], 404)

    def test_single_writer_and_readonly(self):
        self.open()
        with self.assertRaisesRegex(app.Error, "INSTANCE_EDIT_LEASE_HELD"):
            self.open()
        read = self.open(subject=self.viewer, mode="read")
        self.call("scene.get", {"editing_session_id": read}, subject=self.viewer)
        with self.assertRaisesRegex(app.Error, "READ_ONLY"):
            self.call("object.create", {"editing_session_id": read, "idempotency_key": "readonly-001", "scene_version": 0, "object_id": "Cube", "primitive": "cube"}, subject=self.viewer)

    def test_writes_replay_after_gateway_restart_without_reexecution(self):
        sid = self.open()
        args = {"editing_session_id": sid, "idempotency_key": "create-cube-001", "scene_version": 0, "object_id": "Cube", "primitive": "cube"}
        first = self.call("object.create", args)
        self.application.store.db.close()
        self.application.store = app.Store(str(Path(self.temp.name) / "state.db"))
        self.assertEqual(self.call("object.create", args), first)
        self.assertEqual(len(self.workers[0].requests), 1)
        with self.assertRaisesRegex(app.Error, "IDEMPOTENCY_CONFLICT"):
            self.call("object.create", dict(args, object_id="Other"))

    def test_uncertain_operation_never_retried(self):
        sid = self.open()
        args = {"editing_session_id": sid, "idempotency_key": "save-project-001", "scene_version": 0}
        self.workers[0].fail = True
        first = self.call("scene.save", args)
        self.assertEqual(first["state"], "unknown")
        self.workers[0].fail = False
        retry = self.call("scene.save", args)
        self.assertEqual(retry["state"], "unknown")
        self.assertEqual(len(self.workers[0].requests), 1)
        self.assertEqual(self.call("operation.get", {"operation_id": first["operation_id"]})["state"], "unknown")

    def test_notifications_batches_headers_and_tool_schema(self):
        status, body = self.rpc({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "object.delete", "arguments": {}}})
        self.assertEqual(status, 400)
        self.assertIsInstance(body["error"]["code"], int)
        self.assertEqual(self.workers[0].requests, [])
        self.assertEqual(self.rpc([{ "jsonrpc": "2.0", "id": 1, "method": "ping"}])[0], 400)
        self.assertEqual(self.rpc({"jsonrpc": "2.0", "id": 1, "method": "ping"}, extra={"Mcp-Session-Id": "other"})[0], 400)
        self.assertEqual(self.rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})[0], 202)
        status, body = self.rpc({"jsonrpc": "2.0", "id": "list", "method": "tools/list"})
        self.assertEqual(status, 200)
        for tool in body["result"]["tools"]:
            self.assertNotIn("instance_id", tool["inputSchema"]["properties"])

    def test_project_revocation_blocks_existing_session_and_discovery(self):
        sid = self.open()
        self.content.denied = True
        with self.assertRaisesRegex(app.Error, "PERMISSION_DENIED"):
            self.call("scene.get", {"editing_session_id": sid})
        self.assertEqual(self.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[0], 403)
        self.assertEqual(self.workers[0].requests, [])

    def test_gui_and_mcp_share_lease_and_gui_rechecks_project(self):
        secret = Path(self.temp.name) / 'auth.conf'
        secret.write_text('static-auth-secret=' + 'test-key-' * 8)
        env = patch.dict(os.environ, BLENDER_TURN_HOST='192.0.2.10', BLENDER_TURN_SECRET_FILE=str(secret))
        env.start()
        self.addCleanup(env.stop)
        os.environ["BLENDER_GUI_TEST"] = "fixture-password"
        self.instances["blenderA"].update(gui_endpoint=self.instances["blenderA"]["endpoint"], gui_auth_env="BLENDER_GUI_TEST")
        self.save_config()
        sid = self.open()
        with self.assertRaisesRegex(app.Error, "INSTANCE_EDIT_LEASE_HELD"):
            self.application.gui("blenderA", self.subject, self.org, "open")
        self.call("session.close", {"editing_session_id": sid})
        session = self.application.gui("blenderA", self.subject, self.org, "open")
        with self.assertRaisesRegex(app.Error, "INSTANCE_EDIT_LEASE_HELD"):
            self.open()
        with self.assertRaisesRegex(app.Error, "GUI_RELEASE_REQUIRED"):
            self.call("session.close", {"editing_session_id": session["editing_session_id"]})
        self.content.denied = True
        with self.assertRaisesRegex(app.Error, "PERMISSION_DENIED"):
            self.application.gui("blenderA", self.subject, self.org, "check", session["editing_session_id"])
        self.content.denied = False
        self.application.gui("blenderA", self.subject, self.org, "close", session["editing_session_id"])
        self.open()

    def save(self, sid, key):
        self.instances["blenderA"]["file_endpoint"] = self.instances["blenderA"]["endpoint"]
        self.save_config()
        return self.call("project.save", {"editing_session_id": sid, "scene_version": 0, "idempotency_key": key})

    def wait_save(self, operation):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.application.store.transaction() as db:
                row = db.execute("SELECT * FROM operations WHERE id=?", (operation,)).fetchone()
            if row["state"] != "running" and not self.application.save_threads:
                return dict(row)
            time.sleep(0.01)
        self.fail("save did not finish")

    def test_project_save_binary_revision_and_conflict_preservation(self):
        sid = self.open()
        first = self.save(sid, "project-save-first")
        self.assertEqual(first["state"], "running")
        row = self.wait_save(first["operation_id"])
        self.assertEqual(row["state"], "completed", row)
        result = json.loads(row["result"])
        self.assertEqual(result["revision_id"], self.content.revision)
        second = self.save(sid, "project-save-second")
        self.assertEqual(self.wait_save(second["operation_id"])["state"], "completed")
        self.assertEqual(len(self.content.files), 1)
        # An unrelated editor advances the Project. Never rebase silently.
        self.content.revision = app.content.uuid7()
        conflict = self.save(sid, "project-save-conflict")
        row = self.wait_save(conflict["operation_id"])
        self.assertEqual(row["state"], "failed")
        self.assertEqual(json.loads(row["result"])["code"], "REVISION_CONFLICT")
        self.assertTrue((self.application.staging / (conflict["operation_id"] + ".blend")).is_file())

    def test_project_save_commit_response_loss_recovers_after_restart(self):
        sid = self.open()
        self.content.lose_commit = True
        first = self.save(sid, "project-save-recovery")
        self.assertEqual(self.wait_save(first["operation_id"])["state"], "retryable")
        self.application.store.db.close()
        self.application.store = app.Store(str(Path(self.temp.name) / "state.db"))
        self.call("operation.get", {"operation_id": first["operation_id"]})
        self.assertEqual(self.wait_save(first["operation_id"])["state"], "completed")
        self.assertEqual(len(self.content.commits), 1)
        self.assertEqual(len(self.content.uploads), 1)
        self.assertEqual(len(self.workers[0].requests), 1)

    def existing_project(self):
        self.content.files = [{"path": "blender/main.blend", "file_id": app.content.uuid7(),
            "content_ref": {k: app.content.uuid7() for k in ("store_id", "artifact_id", "version_id")}}]
        self.instances["blenderA"]["file_endpoint"] = self.instances["blenderA"]["endpoint"]
        self.save_config()

    def test_first_restore_requires_editor_and_saves_using_original_file(self):
        self.existing_project()
        with self.assertRaisesRegex(app.Error, "INITIAL_PROJECT_RESTORE_REQUIRED"):
            self.open(subject=self.viewer, mode="read")
        old_generation = self.workers[0].generation
        sid = self.open()
        self.assertNotEqual(old_generation, self.workers[0].generation)
        self.assertEqual(self.content.downloaded_revision, self.content.revision)
        self.call("scene.get", {"editing_session_id": sid})
        file_id = self.content.files[0]["file_id"]
        saved = self.save(sid, "restored-project-save")
        self.assertEqual(self.wait_save(saved["operation_id"])["state"], "completed")
        self.assertEqual(self.content.files[0]["file_id"], file_id)
        self.open(subject=self.viewer, mode="read")
        self.assertEqual(sum("restore_id" in r for r in self.workers[0].requests), 1)

    def test_restore_response_loss_recovers_pinned_revision_after_app_restart(self):
        self.existing_project()
        initial = self.content.revision
        self.workers[0].lose_restore = True
        with self.assertRaisesRegex(app.Error, "WORKER_UNAVAILABLE"):
            self.open()
        self.application.store.db.close()
        self.application.store = app.Store(str(Path(self.temp.name) / "state.db"))
        self.content.revision = app.content.uuid7()
        sid = self.open()
        copy = self.application.store.db.execute("SELECT * FROM working_copies").fetchone()
        self.assertEqual(copy["revision"], initial)
        self.assertEqual(sum("restore_id" in r for r in self.workers[0].requests), 1)
        saved = self.save(sid, "restored-stale-project")
        self.assertEqual(self.wait_save(saved["operation_id"])["state"], "failed")

    def test_restore_unknown_generation_never_reloads_or_issues_session(self):
        self.existing_project()
        self.workers[0].lose_restore = True
        with self.assertRaisesRegex(app.Error, "WORKER_UNAVAILABLE"):
            self.open()
        self.workers[0].restore_id = None
        with self.assertRaisesRegex(app.Error, "RESTORE_STATE_UNKNOWN"):
            self.open()
        self.assertEqual(self.application.store.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)
        self.assertEqual(sum("restore_id" in r for r in self.workers[0].requests), 1)


if __name__ == "__main__":
    unittest.main()
