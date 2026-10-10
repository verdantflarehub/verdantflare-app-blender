#!/usr/bin/env python3
"""Blender application boundary: instance ACL, MCP, editing sessions and ledger.

One application replica owns a persistent SQLite database. Workers receive only
bounded internal RPC with a fixed instance and Blender process generation.
"""
from __future__ import annotations

from contextlib import contextmanager
import base64
import hashlib
import hmac
import http.server
import importlib.util
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

VERSION = "0.1.4"
PROTOCOL = "2025-06-18"
MAX_BODY = 1024 * 1024
MAX_RESPONSE = 4 * 1024 * 1024
ALIAS = re.compile(r"blender[A-Za-z0-9_-]{1,57}\Z")
KEY = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")
spec = importlib.util.spec_from_file_location("blender_tools", Path(__file__).parents[1] / "runtime/mcp-bridge/bridge.py")
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)
TOOLS = {k: v for k, v in bridge.TOOLS.items() if not k.startswith("job.")}
content_spec = importlib.util.spec_from_file_location("blender_content", Path(__file__).with_name("content.py"))
content = importlib.util.module_from_spec(content_spec)
content_spec.loader.exec_module(content)
turn_spec = importlib.util.spec_from_file_location("blender_turn", Path(__file__).with_name("turn.py"))
turn = importlib.util.module_from_spec(turn_spec)
turn_spec.loader.exec_module(turn)


class Error(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def valid_id(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def endpoint(value):
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("invalid internal origin")
    return value.rstrip("/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Store:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
          id TEXT PRIMARY KEY, subject TEXT NOT NULL, org TEXT NOT NULL,
          instance TEXT NOT NULL, project TEXT NOT NULL, generation TEXT NOT NULL,
          mode TEXT NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS operations (
          id TEXT PRIMARY KEY, subject TEXT NOT NULL, instance TEXT NOT NULL,
          key TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL,
          result TEXT, created REAL NOT NULL, UNIQUE(subject, instance, key));
        CREATE TABLE IF NOT EXISTS working_copies (
          instance TEXT PRIMARY KEY, project TEXT NOT NULL, revision TEXT NOT NULL,
          file_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS saves (
          operation TEXT PRIMARY KEY, details TEXT NOT NULL);
        """)
        # An interrupted request is not safe to replay automatically.
        self.db.execute("UPDATE operations SET state='unknown' WHERE state='running'")

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise


class Application:
    def __init__(self, config, database, token, content_client=None):
        if not token:
            raise ValueError("BLENDER_STUDIO_TOKEN is required")
        self.config, self.token = Path(config), token
        self.store = Store(database)
        self.locks_guard = threading.Lock()
        self.locks = {}
        self.content = content_client or content.Client(os.environ["BLENDER_CONTENT_ORIGIN"], os.environ["BLENDER_CONTENT_TOKEN"])
        self.staging = Path(database).parent / "saves"
        self.staging.mkdir(exist_ok=True)
        self.save_threads = set()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.instances()  # Invalid configuration must fail startup.

    def instances(self):
        try:
            cfg = json.loads(self.config.read_text(encoding="utf-8"))["instances"]
            if not isinstance(cfg, dict):
                raise ValueError()
            ids = set()
            for alias, item in cfg.items():
                if not ALIAS.fullmatch(alias) or not valid_id(item["id"]) or item["id"] in ids:
                    raise ValueError()
                ids.add(item["id"])
                if not valid_id(item["organization_id"]) or not valid_id(item["project_id"]):
                    raise ValueError()
                endpoint(item["endpoint"])
                if item.get("file_endpoint"):
                    endpoint(item["file_endpoint"])
                if item.get("gui_endpoint"):
                    endpoint(item["gui_endpoint"])
                    if not re.fullmatch(r"BLENDER_GUI_[A-Z0-9_]+", item.get("gui_auth_env", "")) or not os.environ.get(item["gui_auth_env"]):
                        raise ValueError()
                if not isinstance(item["grants"], dict) or any(not valid_id(k) or v not in {"read", "edit"} for k, v in item["grants"].items()):
                    raise ValueError()
                if not re.fullmatch(r"BLENDER_WORKER_[A-Z0-9_]+", item["token_env"]) or not os.environ.get(item["token_env"]):
                    raise ValueError()
            return cfg
        except (OSError, ValueError, KeyError, TypeError):
            raise Error("INSTANCE_CONFIGURATION_UNAVAILABLE", 503) from None

    def authorize(self, alias, subject, org):
        item = self.instances().get(alias)
        if item is None or item["organization_id"] != org or subject not in item["grants"]:
            raise Error("INSTANCE_NOT_FOUND", 404)
        try:
            item["project"] = self.content.open(subject, org, item["project_id"])
        except content.ContentError as exc:
            raise Error(exc.code, exc.status) from None
        return item

    def lock(self, instance):
        with self.locks_guard:
            return self.locks.setdefault(instance, threading.RLock())

    def worker(self, item, payload=None, path=None):
        path = path or ("/readyz" if payload is None else "/internal/rpc")
        request = urllib.request.Request(endpoint(item["endpoint"]) + path,
            data=None if payload is None else encode(payload).encode(),
            headers={"Authorization": "Bearer " + os.environ[item["token_env"]], "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=30 if payload else 3) as response:
                body = response.read(MAX_RESPONSE + 1)
                if len(body) > MAX_RESPONSE:
                    raise Error("WORKER_RESPONSE_INVALID", 502)
                result = json.loads(body)
                if not isinstance(result, dict):
                    raise ValueError()
                return result
        except urllib.error.HTTPError as exc:
            if exc.code in {400, 403, 409}:
                try:
                    code = json.loads(exc.read(16384)).get("code")
                    if code in {"INVALID_ARGUMENT", "SCENE_VERSION_CONFLICT", "STALE_GENERATION", "ASSET_NOT_FOUND", "POLICY_DENIED", "GUI_EDIT_LEASE_HELD", "GUI_LEASE_EXPIRED", "GUI_LEASE_HELD", "GUI_LEASE_MISMATCH", "GUI_PROCESS_UNAVAILABLE"}:
                        raise Error(code, exc.code) from None
                except (ValueError, AttributeError):
                    pass
            raise Error("WORKER_UNAVAILABLE", 503) from None
        except (OSError, ValueError):
            raise Error("WORKER_UNAVAILABLE", 503) from None

    def generation(self, item):
        status = self.worker(item)
        if status.get("instance_id") != item["id"] or not isinstance(status.get("generation"), str) or not status["generation"]:
            raise Error("WORKER_IDENTITY_MISMATCH", 503)
        return status["generation"]

    def descriptors(self):
        out = []
        for tool in TOOLS.values():
            descriptor = tool.descriptor()
            schema = descriptor["inputSchema"]
            del schema["properties"]["instance_id"]
            schema["properties"]["editing_session_id"] = {"type": "string"}
            schema["required"].append("editing_session_id")
            if tool.mutating or tool.name == "asset.export":
                schema["properties"]["idempotency_key"] = {"type": "string", "minLength": 8, "maxLength": 128}
                schema["required"].append("idempotency_key")
            out.append(descriptor)
        for name, description, properties, required in [
            ("session.open", "Open an editing or observation session on this instance's assigned project.", {"project_id": {"type": "string"}, "mode": {"type": "string", "enum": ["read", "edit"]}}, ["project_id", "mode"]),
            ("session.close", "Release an editing session. Does not stop Blender.", {"editing_session_id": {"type": "string"}}, ["editing_session_id"]),
            ("operation.get", "Recover a previous operation result; unknown operations must not be blindly retried.", {"operation_id": {"type": "string"}}, ["operation_id"]),
            ("project.save", "Save an immutable Blender checkpoint through Artifact and commit a Project revision. Poll operation.get for completion; conflicts preserve the checkpoint.", {"editing_session_id": {"type": "string"}, "scene_version": {"type": "integer", "minimum": 0}, "idempotency_key": {"type": "string", "minLength": 8, "maxLength": 128}}, ["editing_session_id", "scene_version", "idempotency_key"]),
        ]:
            out.append({"name": name, "description": description, "inputSchema": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}})
        return out

    def call(self, alias, subject, org, name, args):
        item = self.authorize(alias, subject, org)
        schema = next((x["inputSchema"] for x in self.descriptors() if x["name"] == name), None)
        if schema is None or not isinstance(args, dict) or set(args) - set(schema["properties"]) or any(x not in args for x in schema["required"]):
            raise Error("INVALID_TOOL_ARGUMENTS")
        with self.lock(item["id"]):
            # Re-read grants after waiting for another request's execution.
            item = self.authorize(alias, subject, org)
            now = time.time()
            if name == "operation.get":
                with self.store.transaction() as db:
                    row = db.execute("SELECT id,state,result FROM operations WHERE id=? AND subject=? AND instance=?", (args["operation_id"], subject, item["id"])).fetchone()
                if row is None:
                    raise Error("OPERATION_NOT_FOUND", 404)
                self.resume_save(row["id"], alias, subject, org)
                return {"operation_id": row["id"], "state": row["state"], "async": True, "result": json.loads(row["result"]) if row["result"] else None}
            if name == "session.open":
                if args["project_id"] != item["project_id"] or args["mode"] not in {"read", "edit"}:
                    raise Error("PROJECT_OR_MODE_DENIED", 403)
                if args["mode"] == "edit" and item["grants"][subject] != "edit":
                    raise Error("READ_ONLY", 403)
                generation = self.generation(item)
                sid = secrets.token_urlsafe(32)
                with self.store.transaction() as db:
                    db.execute("DELETE FROM sessions WHERE instance=? AND (expires < ? OR generation != ?)", (item["id"], now, generation))
                    if args["mode"] == "edit" and db.execute("SELECT 1 FROM sessions WHERE instance=? AND mode IN ('edit','gui')", (item["id"],)).fetchone():
                        raise Error("INSTANCE_EDIT_LEASE_HELD", 409)
                    db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)", (sid, subject, org, item["id"], item["project_id"], generation, args["mode"], now + 1800))
                    copy = db.execute("SELECT * FROM working_copies WHERE instance=?", (item["id"],)).fetchone()
                    if copy is None:
                        if any(f.get("path") == "blender/main.blend" for f in item["project"].get("manifest", {}).get("files", [])):
                            raise Error("INITIAL_PROJECT_RESTORE_REQUIRED", 409)
                        db.execute("INSERT INTO working_copies VALUES (?,?,?,?)", (item["id"], item["project_id"], item["project"]["revision_id"], ""))
                    elif copy["project"] != item["project_id"]:
                        raise Error("WORKING_COPY_PROJECT_MISMATCH", 409)
                return {"editing_session_id": sid, "instance_id": item["id"], "project_id": item["project_id"], "generation": generation, "mode": args["mode"], "expires_at": now + 1800}
            with self.store.transaction() as db:
                row = db.execute("SELECT * FROM sessions WHERE id=? AND subject=? AND org=? AND instance=? AND project=? AND expires>?", (args["editing_session_id"], subject, org, item["id"], item["project_id"], now)).fetchone()
                if row is None:
                    raise Error("EDITING_SESSION_INVALID", 403)
                if name == "session.close":
                    if row["mode"] == "gui":
                        raise Error("GUI_RELEASE_REQUIRED", 409)
                    db.execute("DELETE FROM sessions WHERE id=?", (row["id"],))
                    return {"closed": True}
            if row["generation"] != self.generation(item):
                raise Error("STALE_GENERATION", 409)
            tool = TOOLS.get(name)
            writing = name == "project.save" or tool.mutating or name == "asset.export"
            if writing and (row["mode"] != "edit" or item["grants"][subject] != "edit"):
                raise Error("READ_ONLY", 403)
            operation_id = None
            if writing:
                if not isinstance(args["idempotency_key"], str) or not KEY.fullmatch(args["idempotency_key"]):
                    raise Error("INVALID_IDEMPOTENCY_KEY")
                digest = hashlib.sha256(encode({"name": name, "arguments": args}).encode()).hexdigest()
                with self.store.transaction() as db:
                    prev = db.execute("SELECT * FROM operations WHERE subject=? AND instance=? AND key=?", (subject, item["id"], args["idempotency_key"])).fetchone()
                    if prev:
                        if prev["digest"] != digest:
                            raise Error("IDEMPOTENCY_CONFLICT", 409)
                        if prev["state"] == "completed":
                            return json.loads(prev["result"])
                        return {"operation_id": prev["id"], "state": prev["state"], "replayed": False, "async": name == "project.save"}
                    operation_id = str(uuid.uuid4())
                    db.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?,?)", (operation_id, subject, item["id"], args["idempotency_key"], digest, "running", None, now))
            payload = {"instance_id": item["id"], "generation": row["generation"], "name": "scene.checkpoint" if name == "project.save" else name,
                       "arguments": {k: v for k, v in args.items() if k not in {"editing_session_id", "idempotency_key"}}, "request_id": operation_id or str(uuid.uuid4())}
            try:
                result = self.worker(item, payload)
                if result.get("ok") is not True or result.get("generation") != row["generation"]:
                    raise Error("WORKER_RESPONSE_INVALID", 502)
                if name == "project.save":
                    if not re.fullmatch(r"checkpoints/checkpoint-[0-9]+-[0-9a-f]{32}\.blend", result.get("asset_id", "")):
                        raise Error("CHECKPOINT_RESPONSE_INVALID", 502)
                    with self.store.transaction() as db:
                        copy = db.execute("SELECT * FROM working_copies WHERE instance=?", (item["id"],)).fetchone()
                        details = {"alias": alias, "subject": subject, "org": org, "instance": item["id"], "project": item["project_id"], "revision": copy["revision"], "file_id": copy["file_id"], "asset": result["asset_id"], "scene_version": result["scene_version"], "write_id": content.uuid7(), "commit_id": content.uuid7()}
                        db.execute("INSERT INTO saves VALUES (?,?)", (operation_id, encode(details)))
                        db.execute("UPDATE sessions SET expires=? WHERE id=?", (time.time() + 1800, row["id"]))
                    self.resume_save(operation_id, alias, subject, org)
                    return {"operation_id": operation_id, "state": "running", "async": True, "scene_version": result["scene_version"]}
            except Error as exc:
                if operation_id:
                    # Even an error can follow a partial scene modification.
                    result = {"operation_id": operation_id, "state": "unknown", "code": exc.code}
                    with self.store.transaction() as db:
                        db.execute("UPDATE operations SET state='unknown',result=? WHERE id=?", (encode(result), operation_id))
                    return result
                raise
            if operation_id:
                result.update(operation_id=operation_id, state="completed")
            with self.store.transaction() as db:
                if operation_id:
                    db.execute("UPDATE operations SET state='completed',result=? WHERE id=?", (encode(result), operation_id))
                db.execute("UPDATE sessions SET expires=? WHERE id=?", (time.time() + 1800, row["id"]))
            return result

    def gui(self, alias, subject, org, action, sid=None):
        item = self.authorize(alias, subject, org)
        with self.lock(item["id"]):
            item = self.authorize(alias, subject, org)
            if item["grants"][subject] != "edit" or not item.get("gui_endpoint"):
                raise Error("GUI_ACCESS_DENIED", 403)
            if action == "open":
                try:
                    rtc_config = turn.configuration("worker:" + secrets.token_urlsafe(24))
                except (OSError, ValueError, KeyError):
                    raise Error("TURN_NOT_CONFIGURED", 503) from None
                session = self.call(alias, subject, org, "session.open", {"project_id": item["project_id"], "mode": "edit"})
                sid = session["editing_session_id"]
                with self.store.transaction() as db:
                    db.execute("UPDATE sessions SET mode='gui',expires=? WHERE id=?", (time.time() + 45, sid))
                try:
                    self.worker(item, {"instance_id": item["id"], "generation": session["generation"], "session": sid, "action": "open", "rtc_config": rtc_config}, path="/internal/gui")
                except Error:
                    # Keep fencing unless the worker confirms its input process stopped.
                    try:
                        self.gui(alias, subject, org, "close", sid)
                    except Error:
                        pass
                    raise
                return {"editing_session_id": sid, "instance_id": item["id"], "project_id": item["project_id"], "mode": "gui", "reconnect_after": turn.RECONNECT_AFTER}
            with self.store.transaction() as db:
                row = db.execute("SELECT * FROM sessions WHERE id=? AND subject=? AND org=? AND instance=? AND project=? AND mode='gui'", (sid, subject, org, item["id"], item["project_id"])).fetchone()
            if row is None or action != "close" and row["expires"] <= time.time():
                raise Error("GUI_SESSION_INVALID", 403)
            if action == "close":
                self.worker(item, {"instance_id": item["id"], "generation": row["generation"], "session": sid, "action": "close"}, path="/internal/gui")
                with self.store.transaction() as db:
                    db.execute("DELETE FROM sessions WHERE id=?", (sid,))
                return {"closed": True}
            if action != "check":
                raise Error("INVALID_GUI_ACTION")
            self.worker(item, {"instance_id": item["id"], "generation": row["generation"], "session": sid, "action": "heartbeat"}, path="/internal/gui")
            with self.store.transaction() as db:
                db.execute("UPDATE sessions SET expires=? WHERE id=?", (time.time() + 45, sid))
            return {"instance_id": item["id"], "project_id": item["project_id"], "gui_origin": endpoint(item["gui_endpoint"])}

    def resume_save(self, operation, alias, subject, org):
        with self.store.transaction() as db:
            row = db.execute("SELECT s.details,o.state FROM saves s JOIN operations o ON o.id=s.operation WHERE s.operation=? AND o.subject=?", (operation, subject)).fetchone()
            if row is None or row["state"] not in {"running", "unknown", "retryable"}:
                return
            details = json.loads(row["details"])
            if (details["alias"], details["subject"], details["org"]) != (alias, subject, org):
                raise Error("OPERATION_NOT_FOUND", 404)
        with self.locks_guard:
            if operation in self.save_threads:
                return
            self.save_threads.add(operation)
        threading.Thread(target=self.finish_save, args=(operation, details), daemon=True).start()

    def finish_save(self, operation, details):
        subject, org, project_id = details["subject"], details["org"], details["project"]
        try:
            item = self.authorize(details["alias"], subject, org)
            if item["id"] != details["instance"] or item["project_id"] != project_id or item["grants"][subject] != "edit":
                raise Error("SAVE_PERMISSION_DENIED", 403)
            with self.store.transaction() as db:
                db.execute("UPDATE operations SET state='running' WHERE id=?", (operation,))
            path = self.staging / (operation + ".blend")
            if not path.exists():
                request = urllib.request.Request(endpoint(item["file_endpoint"]) + "/assets/" + details["asset"], headers={"Authorization": "Bearer " + os.environ[item["token_env"]]})
                tmp = path.with_suffix(".partial")
                try:
                    with self.opener.open(request, timeout=120) as response, tmp.open("wb") as stream:
                        size = int(response.headers.get("Content-Length", "-1"))
                        expected = response.headers.get("X-Asset-SHA256", "")
                        if not 0 < size <= content.MAX_FILE or not re.fullmatch(r"[0-9a-f]{64}", expected):
                            raise Error("CHECKPOINT_INVALID", 502)
                        digest, count = hashlib.sha256(), 0
                        for chunk in iter(lambda: response.read(1024 * 1024), b""):
                            count += len(chunk)
                            if count > size:
                                raise Error("CHECKPOINT_INVALID", 502)
                            stream.write(chunk)
                            digest.update(chunk)
                        if count != size or digest.hexdigest() != expected:
                            raise Error("CHECKPOINT_HASH_MISMATCH", 502)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(tmp, path)
                finally:
                    tmp.unlink(missing_ok=True)
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            size = path.stat().st_size
            ref = self.content.upload(subject, org, project_id, details["write_id"], path, digest, size)
            file_update = {"path": "blender/main.blend", "role": "source", "content_ref": ref}
            if details["file_id"]:
                file_update["file_id"] = details["file_id"]
            result = self.content.request(subject, org, "POST", "/project/commit", {"project_id": project_id, "expected_revision_id": details["revision"], "commit_id": details["commit_id"], "changes": {"upsert_files": [file_update]}})
            if result.get("project_id") != project_id or not content.ID.fullmatch(result.get("revision_id", "")):
                raise Error("PROJECT_RESPONSE_INVALID", 502)
            saved = [f for f in result.get("manifest", {}).get("files", []) if f.get("path") == "blender/main.blend" and f.get("content_ref") == ref]
            if len(saved) != 1 or not content.ID.fullmatch(saved[0].get("file_id", "")):
                raise Error("PROJECT_RESPONSE_INVALID", 502)
            value = {"operation_id": operation, "state": "completed", "project_id": project_id, "revision_id": result["revision_id"], "content_ref": ref, "sha256": digest, "size": size, "scene_version": details["scene_version"]}
            with self.store.transaction() as db:
                db.execute("UPDATE operations SET state='completed',result=? WHERE id=?", (encode(value), operation))
                db.execute("UPDATE working_copies SET revision=?,file_id=? WHERE instance=? AND project=? AND revision=?", (result["revision_id"], saved[0]["file_id"], details["instance"], project_id, details["revision"]))
        except (Error, content.ContentError, OSError, ValueError, KeyError) as exc:
            code = getattr(exc, "code", "CONTENT_SERVICE_UNAVAILABLE")
            state = "failed" if getattr(exc, "status", 503) in {400, 403, 404, 409} and code not in {"COMMIT_IN_PROGRESS", "CONTENT_NOT_READY"} else "retryable"
            with self.store.transaction() as db:
                db.execute("UPDATE operations SET state=?,result=? WHERE id=?", (state, encode({"code": code, "checkpoint": details["asset"], "commit_id": details["commit_id"]}), operation))
        finally:
            with self.locks_guard:
                self.save_threads.discard(operation)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(40)

    def reply(self, status, value=None):
        data = b"" if value is None else encode(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def identity(self):
        for key in ("Authorization", "X-User-Id", "X-Organization-Id"):
            if len(self.headers.get_all(key, [])) != 1:
                raise Error("UNAUTHENTICATED", 401)
        if not hmac.compare_digest(self.headers["Authorization"].encode(), ("Bearer " + self.server.app.token).encode()):
            raise Error("UNAUTHENTICATED", 401)
        subject, org = self.headers["X-User-Id"], self.headers["X-Organization-Id"]
        if not valid_id(subject) or not valid_id(org):
            raise Error("INVALID_IDENTITY", 403)
        return subject, org

    def do_GET(self):
        if self.path == "/healthz":
            return self.reply(200, {"status": "ok", "version": VERSION})
        try:
            subject, org = self.identity()
            if self.path.startswith("/internal/gui-config/"):
                parts = self.path.removeprefix("/internal/gui-config/").split("/")
                if len(parts) != 3 or parts[2] not in {"settings", "turn"}:
                    raise Error("NOT_FOUND", 404)
                alias, sid, name = parts
                self.server.app.gui(alias, subject, org, "check", sid)
                if name == 'turn':
                    try:
                        return self.reply(200, turn.configuration('browser:' + sid))
                    except (OSError, KeyError, ValueError):
                        raise Error('TURN_NOT_CONFIGURED', 503) from None
                item = self.server.app.authorize(alias, subject, org)
                auth = base64.b64encode(("beagle:" + os.environ[item["gui_auth_env"]]).encode()).decode()
                request = urllib.request.Request(endpoint(item["gui_endpoint"]) + "/" + name, headers={"Authorization": "Basic " + auth})
                try:
                    with self.server.app.opener.open(request, timeout=5) as response:
                        raw = response.read(65537)
                        if len(raw) > 65536:
                            raise ValueError()
                        data = json.loads(raw)
                        if not isinstance(data, dict):
                            raise ValueError()
                    if name == "settings":
                        data = {k: v for k, v in data.items() if k in {"BDWIND_FRAMERATE", "BDWIND_VIDEO_BITRATE", "BDWIND_AUDIO_BITRATE", "BDWIND_ENCODER", "BDWIND_RESOLUTION"}}
                    return self.reply(200, data)
                except (OSError, ValueError):
                    raise Error("GUI_STARTING", 503) from None
            if self.path.startswith("/internal/gui-assets/"):
                parts = self.path.removeprefix("/internal/gui-assets/").split("/", 2)
                if len(parts) != 3:
                    raise Error("NOT_FOUND", 404)
                alias, sid, name = parts
                self.server.app.gui(alias, subject, org, "check", sid)
                root = Path(os.environ.get("BLENDER_WEB_ROOT", "/app/webclient")).resolve()
                path = (root / (name or "index.html")).resolve()
                if root not in path.parents or not path.is_file() or path.suffix not in {".html", ".js", ".css", ".svg", ".png", ".ico", ".woff", ".woff2", ".ttf", ".json"} or path.stat().st_size > MAX_RESPONSE:
                    raise Error("NOT_FOUND", 404)
                data = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(data)
                return
            if self.path == "/internal/instances":
                instances = []
                for alias, item in self.server.app.instances().items():
                    if item["organization_id"] == org and subject in item["grants"]:
                        try:
                            self.server.app.authorize(alias, subject, org)
                        except Error as exc:
                            if exc.status in {403, 404}:
                                continue
                            raise
                        instances.append({"alias": alias, "instance_id": item["id"], "project_id": item["project_id"], "access": item["grants"][subject], "mcp_path": "/mcp/" + alias})
                return self.reply(200, {"instances": instances})
            if self.path.startswith("/internal/access/"):
                alias = self.path.removeprefix("/internal/access/")
                item = self.server.app.authorize(alias, subject, org)
                return self.reply(200, {"instance_id": item["id"], "project_id": item["project_id"], "access": item["grants"][subject]})
            if self.path.startswith("/internal/mcp/"):
                self.server.app.authorize(self.path.removeprefix("/internal/mcp/"), subject, org)
                return self.reply(405, {"code": "STATELESS_JSON_MCP_POST_ONLY"})
            self.reply(404, {"code": "NOT_FOUND"})
        except Error as exc:
            self.reply(exc.status, {"code": exc.code})

    do_DELETE = do_GET

    def do_POST(self):
        request_id = None
        try:
            subject, org = self.identity()
            if self.path.startswith("/internal/gui/"):
                parts = self.path.removeprefix("/internal/gui/").split("/")
                if len(parts) != 2 or parts[1] not in {"open", "check", "close"}:
                    raise Error("NOT_FOUND", 404)
                if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1 or self.headers.get_content_type() != "application/json":
                    raise Error("INVALID_BODY")
                length = int(self.headers.get("Content-Length", "-1"))
                if not 0 < length <= 4096:
                    raise Error("INVALID_BODY")
                data = json.loads(self.rfile.read(length))
                expected = set() if parts[1] == "open" else {"editing_session_id"}
                if not isinstance(data, dict) or set(data) != expected or "editing_session_id" in data and not isinstance(data["editing_session_id"], str):
                    raise Error("INVALID_BODY")
                return self.reply(200, self.server.app.gui(parts[0], subject, org, parts[1], data.get("editing_session_id")))
            if not self.path.startswith("/internal/mcp/"):
                raise Error("NOT_FOUND", 404)
            alias = self.path.removeprefix("/internal/mcp/")
            self.server.app.authorize(alias, subject, org)
            if self.headers.get("Mcp-Session-Id"):
                raise Error("STATELESS_TRANSPORT_NO_SESSION_HEADER")
            if self.headers.get("MCP-Protocol-Version", PROTOCOL) not in {PROTOCOL, "2025-03-26", "2024-11-05"}:
                raise Error("UNSUPPORTED_PROTOCOL_VERSION")
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                raise Error("INVALID_BODY")
            if self.headers.get_content_type() != "application/json":
                raise Error("JSON_REQUIRED", 415)
            length = int(self.headers.get("Content-Length", "-1"))
            if not 0 < length <= MAX_BODY:
                raise Error("BODY_TOO_LARGE", 413)
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
                raise Error("INVALID_REQUEST")
            request_id = request.get("id")
            if "id" in request and (type(request_id) not in (int, str) or (isinstance(request_id, str) and len(request_id) > 256)):
                request_id = None
                raise Error("INVALID_ID")
            method = request["method"]
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise Error("INVALID_PARAMS")
            # A missing id must never turn a tool invocation into a hidden write.
            if "id" not in request:
                if method in {"notifications/initialized", "notifications/cancelled"}:
                    return self.reply(202)
                raise Error("REQUEST_ID_REQUIRED")
            if method == "initialize":
                version = params.get("protocolVersion")
                result = {"protocolVersion": version if version in {PROTOCOL, "2025-03-26", "2024-11-05"} else PROTOCOL,
                          "capabilities": {"tools": {}}, "serverInfo": {"name": "verdantflare-blender-" + alias, "version": VERSION},
                          "instructions": "Open a session for the assigned project before scene operations. This connection controls only " + alias + "."}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self.server.app.descriptors()}
            elif method == "tools/call":
                try:
                    value = self.server.app.call(alias, subject, org, params.get("name"), params.get("arguments", {}))
                    failed = value.get("state") in {"unknown", "retryable", "failed"} or (value.get("state") == "running" and not value.get("async"))
                except Error as exc:
                    value, failed = {"code": exc.code}, True
                result = {"content": [{"type": "text", "text": encode(value)}], "isError": failed}
            else:
                return self.reply(200, {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}})
            self.reply(200, {"jsonrpc": "2.0", "id": request_id, "result": result})
        except Error as exc:
            self.reply(exc.status, {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000 if exc.status != 400 else -32600, "message": exc.code}})
        except (ValueError, TypeError, KeyError, sqlite3.Error):
            self.reply(400, {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32600, "message": "Invalid request"}})


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, app):
        self.app = app
        super().__init__(address, Handler)


if __name__ == "__main__":
    app = Application(os.environ["BLENDER_INSTANCES_FILE"], os.environ["BLENDER_DATABASE"], os.environ["BLENDER_STUDIO_TOKEN"])
    Server(("0.0.0.0", int(os.environ.get("PORT", "8080"))), app).serve_forever()
