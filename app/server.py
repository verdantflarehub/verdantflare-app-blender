#!/usr/bin/env python3
"""Blender application boundary: instance ACL, MCP, editing sessions and ledger.

One application replica owns a persistent SQLite database. Workers receive only
bounded internal RPC with a fixed instance and Blender process generation.
"""
from __future__ import annotations

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
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

VERSION = "0.1.18"
PROTOCOL = "2025-06-18"
LEASE_TTL_SECONDS = 1800
ALLOWED_STRAY_HOME_NAMES = frozenset({"C:", "D:"})
MAX_BODY = 1024 * 1024
MAX_RESPONSE = 4 * 1024 * 1024
ALIAS = re.compile(r"blender[A-Za-z0-9_-]{1,57}\Z")
KEY = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")
SCRIPT_PATH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,200}\.py\Z")
SUBDIR_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
BLEND_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.blend\Z")
EXPORT_ASSET_RE = re.compile(r"(?:exports|project)/[A-Za-z0-9._/-]{1,200}\.(?:blend|mp4|json|log|txt|py|glb|gltf|png|jpg|jpeg|exr|tif|tiff)\Z")
PROJECT_PATH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,200}\.[A-Za-z0-9]{1,12}\Z")
ASSET_MIME = {
    ".blend": "application/x-blender",
    ".mp4": "video/mp4",
    ".json": "application/json",
    ".log": "text/plain",
    ".txt": "text/plain",
    ".py": "text/x-python",
    ".glb": "model/gltf-binary",
    ".gltf": "model/gltf+json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".exr": "image/x-exr",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}
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
directory_spec = importlib.util.spec_from_file_location("blender_directory", Path(__file__).with_name("directory.py"))
directory = importlib.util.module_from_spec(directory_spec)
directory_spec.loader.exec_module(directory)
creation_spec = importlib.util.spec_from_file_location("blender_creation", Path(__file__).with_name("creation.py"))
creation = importlib.util.module_from_spec(creation_spec)
creation_spec.loader.exec_module(creation)
management_spec = importlib.util.spec_from_file_location("blender_management", Path(__file__).with_name("management.py"))
management = importlib.util.module_from_spec(management_spec)
management_spec.loader.exec_module(management)


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
        CREATE TABLE IF NOT EXISTS restores (
          instance TEXT PRIMARY KEY, details TEXT NOT NULL);
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
        self.directory = directory.Directory(self.store, content.uuid7)
        self.locks_guard = threading.Lock()
        self.locks = {}
        self.content = content_client or content.Client(os.environ["BLENDER_CONTENT_ORIGIN"], os.environ["BLENDER_CONTENT_TOKEN"])
        self.staging = Path(database).parent / "saves"
        self.staging.mkdir(exist_ok=True)
        self.save_threads = set()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.instances()  # Invalid configuration must fail startup.
        self.management = management.Coordinator(self, creation.CreationClient(self.content, self.staging), content.uuid7,
            (Error, content.ContentError, creation.CreationError, directory.Fault, sqlite3.Error, OSError, ValueError, KeyError, TypeError),
            enabled=os.environ.get('BLENDER_INSTANCE_MANAGEMENT') == 'true')

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
                if "name" in item and (not isinstance(item["name"], str) or not 1 <= len(item["name"].strip()) <= 40):
                    raise ValueError()
                managers = item.get("managers", [])
                if not isinstance(managers, list) or any(not isinstance(k, str) or item["grants"].get(k) != "edit" for k in managers):
                    raise ValueError()
                if not re.fullmatch(r"BLENDER_WORKER_[A-Z0-9_]+", item["token_env"]) or not os.environ.get(item["token_env"]):
                    raise ValueError()
            self.directory.sync_declared(cfg)
            return self.directory.entries()
        except directory.Fault as exc:
            raise Error(str(exc), 503) from None
        except (OSError, ValueError, KeyError, TypeError):
            raise Error("INSTANCE_CONFIGURATION_UNAVAILABLE", 503) from None

    def authorize(self, alias, subject, org):
        item = self.instances().get(alias)
        if item is None or item["organization_id"] != org or subject not in item["grants"]:
            raise Error("INSTANCE_NOT_FOUND", 404)
        try:
            if item['_directory']['source'] == 'dynamic':
                item["project"] = self.content.open(subject, org, item["project_id"], require_write=item['grants'][subject] == 'edit')
            else:
                item["project"] = self.content.open(subject, org, item["project_id"])
        except content.ContentError as exc:
            raise Error(exc.code, exc.status) from None
        item['_actor'] = {'alias': alias, 'subject': subject, 'org': org}
        return item

    def credential(self, item, kind='worker', *, stop_operation=None):
        """Resolve one credential for the current execution; never cache or persist it."""
        if kind not in {'worker', 'gui'}:
            raise Error('INVALID_ARGUMENT', 400)
        if item['_directory']['source'] != 'dynamic':
            token = os.environ.get(item.get('token_env' if kind == 'worker' else 'gui_auth_env', ''))
            if not token:
                raise Error('WORKER_CREDENTIAL_UNAVAILABLE', 503)
            return token
        actor = item.get('_actor')
        if not isinstance(actor, dict):
            raise Error('WORKER_CREDENTIAL_UNAVAILABLE', 503)
        def current():
            value = self.authorize(actor['alias'], actor['subject'], actor['org'])
            keys = ('id', 'project_id', 'organization_id', 'endpoint', 'file_endpoint', 'gui_endpoint', 'runtime_binding', 'execution_binding')
            if any(value.get(k) != item.get(k) for k in keys) or value['_directory']['version'] != item['_directory']['version']:
                raise Error('INSTANCE_STATE_CONFLICT', 409)
            if stop_operation is None:
                self.edit_admission(value)
            else:
                command = self.directory.operation(stop_operation, actor['org'], actor['subject'])
                if (kind != 'worker' or actor['subject'] not in value.get('managers', [])
                        or value['grants'][actor['subject']] != 'edit' or command['instance'] != value['id']
                        or command['action'] != 'stop' or command['state'] != 'running'
                        or command['phase'] not in {'accepted','draining','saving','aborting'}
                        or value['_directory']['state'] != 'stopping' or value['_directory']['operation_id'] != stop_operation):
                    raise Error('INSTANCE_ADMISSION_CLOSED', 409)
            if kind == 'gui' and value['grants'][actor['subject']] != 'edit':
                raise Error('GUI_ACCESS_DENIED', 403)
            return value
        value = current()
        binding, workspace = value.get('execution_binding'), value.get('runtime_binding')
        if not isinstance(binding, dict) or not isinstance(workspace, dict) or not isinstance(binding.get('worker'), dict):
            raise Error('WORKSPACE_BINDING_CONFLICT', 409)
        expected = binding['worker']
        if any(item.get(local) != expected.get(remote) for local, remote in (
                ('endpoint', 'control_endpoint'), ('file_endpoint', 'file_endpoint'), ('gui_endpoint', 'gui_endpoint'))):
            raise Error('WORKSPACE_BINDING_CONFLICT', 409)
        request = {'station_id': workspace['station_id'], 'organization_id': actor['org'], 'user_id': actor['subject'],
                   'project_id': item['project_id'], 'instance_id': item['id'], 'start_operation_id': binding['start_operation_id'], 'kind': kind}
        try:
            result = self.content.request(actor['subject'], actor['org'], 'POST', '/runtime/instances/access', request)
        except content.ContentError as exc:
            raise Error(exc.code, exc.status) from None
        if (not isinstance(result, dict) or result.get('instance_id') != item['id']
                or result.get('start_operation_id') != request['start_operation_id'] or result.get('kind') != kind
                or not isinstance(result.get('worker'), dict)
                or any(result['worker'].get(k) != expected.get(k) for k in ('pod_uid', 'service_uid', 'pod_name', 'namespace', 'control_endpoint', 'file_endpoint', 'gui_endpoint'))
                or result['worker'].get('pod_ready') is not True
                or not isinstance(result.get('credential'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', result['credential'])):
            raise Error('WORKSPACE_BINDING_CONFLICT', 409)
        current()  # ACL, Project access, and execution may change while Runtime responds.
        return result['credential']

    def lock(self, instance):
        with self.locks_guard:
            return self.locks.setdefault(instance, threading.RLock())

    def edit_admission(self, item):
        record = item["_directory"]
        if record["operation_id"] or (record["source"] == "dynamic" and record["state"] != "running"):
            raise Error("INSTANCE_ADMISSION_CLOSED", 409)

    def instance_view(self, alias, subject, org):
        item = self.authorize(alias, subject, org)
        binding = None
        storage = None
        record = item["_directory"]
        status, reason, generation, gui_held = "unknown", "WORKER_UNAVAILABLE", None, None
        try:
            if not item.get("endpoint") or record["state"] in {"creating", "stopped", "starting", "stopping", "deleting", "failed"}:
                raise Error("INSTANCE_NOT_RUNNING", 409)
            probe = self.worker(item, path="/internal/status")
            if (probe.get("instance_id") != item["id"] or probe.get("status") != "ready"
                    or not isinstance(probe.get("generation"), str) or not probe["generation"]
                    or type(probe.get("gui_held")) is not bool):
                raise Error("WORKER_IDENTITY_MISMATCH", 503)
            status, reason = "running", None
            generation, gui_held = probe["generation"], probe["gui_held"]
            candidate = probe.get("resource_binding")
            keys = ("pod_uid", "namespace", "pod_name", "container")
            if isinstance(candidate, dict) and all(isinstance(candidate.get(k), str) and 0 < len(candidate[k]) <= 253 for k in keys):
                binding = {k: candidate[k] for k in keys}
                gpu = candidate.get('gpu_uuid')
                if isinstance(gpu,str) and re.fullmatch(r'GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}',gpu):
                    binding['gpu_uuid'] = gpu
                if item.get('file_endpoint'):
                    try:
                        remote = self.management.client.agent(item['file_endpoint'],self.credential(item),'/internal/workspace/status')
                        metric = remote.get('storage',{})
                        if remote.get('instance_id') == item['id'] and remote.get('pod_uid') == binding['pod_uid']:
                            stamp = datetime.fromisoformat(metric.get('sampled_at',''))
                            age = (datetime.now(timezone.utc)-stamp).total_seconds()
                            if (metric.get('scope') == 'workspace_file_bytes' and metric.get('quality') == 'fresh'
                                    and type(metric.get('value')) is int and metric['value'] >= 0 and -5 <= age <= 30):
                                storage = {k:metric[k] for k in ('value','unit','scope','quality','sampled_at')}
                    except (self.management.errors + (ValueError,TypeError,KeyError)):
                        pass
        except Error as exc:
            reason = exc.code
            if record["source"] == "dynamic" and record["state"] != "running":
                status = record["state"]
        observed_at = datetime.now(timezone.utc).isoformat()
        # Revalidate after potentially slow I/O: revocation/retargeting wins.
        current = self.authorize(alias, subject, org)
        if any(current.get(k) != item.get(k) for k in ("id", "project_id", "endpoint")):
            raise Error("INSTANCE_CONFIGURATION_CHANGED", 503)
        if current["_directory"]["version"] != record["version"]:
            raise Error("INSTANCE_STATE_CONFLICT", 503)
        item = current
        now = time.time()
        with self.store.transaction() as db:
            held = db.execute("SELECT mode FROM sessions WHERE instance=? AND org=? AND generation=? AND expires>? AND mode IN ('edit','gui')",
                              (item["id"], org, generation, now)).fetchall()
            copy = db.execute("SELECT revision FROM working_copies WHERE instance=? AND project=?", (item["id"], item["project_id"])).fetchone()
            saved = db.execute("SELECT o.result,s.details FROM operations o JOIN saves s ON s.operation=o.id WHERE o.instance=? AND o.state='completed' ORDER BY o.created DESC LIMIT 1", (item["id"],)).fetchone()
        control_state = "unknown" if status != "running" else "busy" if gui_held or held else "idle"
        mode = "gui" if gui_held or any(r["mode"] == "gui" for r in held) else "mcp" if held else None
        actions = ["view"]
        lifecycle_available = False
        if (self.management.enabled and record['source'] == 'dynamic' and status == 'stopped'
                and not record['operation_id'] and subject in item.get('managers', [])
                and item['grants'][subject] == 'edit' and isinstance(item.get('runtime_binding'), dict)):
            try:
                options = self.management.options(subject, org)
                created = self.directory.last_command(item['id'], 'create')
                if (options['station_id'] == item['runtime_binding'].get('station_id')
                        and any(p['profile_id'] == item.get('profile_id') for p in options['profiles'])
                        and created and created['state'] == 'completed' and created['phase'] == 'stopped'):
                    actions.extend(['start','destroy'])
                    lifecycle_available = True
            except self.management.errors:
                pass
        if item["grants"][subject] == "edit" and item.get("gui_endpoint") and control_state == "idle":
            actions.append("open")
        if (self.management.enabled and record['source'] == 'dynamic' and status == 'running' and control_state == 'idle'
                and record['state'] == 'running' and not record['operation_id'] and subject in item.get('managers', [])
                and item['grants'][subject] == 'edit' and isinstance(item.get('runtime_binding'),dict)
                and isinstance(item.get('execution_binding',{}).get('proof'),dict)):
            try:
                options = self.management.options(subject,org)
                with self.store.transaction() as db:
                    saving = db.execute("SELECT 1 FROM operations o JOIN saves s ON s.operation=o.id WHERE o.instance=? AND o.state IN ('running','unknown','retryable')", (item['id'],)).fetchone()
                if options['station_id'] == item['runtime_binding'].get('station_id') and not saving:
                    actions.append('stop')
                    lifecycle_available = True
            except self.management.errors:
                pass
        save = {"state": "unknown", "revision_id": None, "last_saved_at": None}
        if saved:
            result, details = json.loads(saved["result"]), json.loads(saved["details"])
            if details.get("project") == item["project_id"] and details.get("kind", "save") == "save":
                save.update(revision_id=result.get("revision_id"), last_saved_at=result.get("saved_at"))
        stopped = self.directory.last_command(item['id'],'stop')
        if stopped and stopped['state'] == 'completed':
            saved_stop = self.directory.stop_receipt(stopped['id']).get('saved',{})
            if saved_stop.get('saved_at','') > (save['last_saved_at'] or ''):
                save.update(revision_id=saved_stop.get('revision_id'),last_saved_at=saved_stop['saved_at'])
        def unavailable(scope):
            return {"value": None, "request": None, "limit": None, "sampled_at": None,
                    "quality": "unavailable", "scope": scope, "reason": "RESOURCE_BINDING_UNAVAILABLE"}
        return {"alias": alias, "instance_id": item["id"], "name": item.get("name", alias.replace("blender", "Blender ", 1)),
            "project_id": item["project_id"], "project_name": item["project"].get("manifest", {}).get("name") or None,
            "access": item["grants"][subject], "can_manage": subject in item.get("managers", []), "mcp_path": "/mcp/" + alias,
            "status": status, "status_reason": reason, "observed_at": observed_at,
            "status_source": "instance_ledger" if record["source"] == "dynamic" and status != "running" else "worker_scene_probe",
            "state_version": item["_directory"]["version"], "management_state": item["_directory"]["state"],
            "management_operation_id": item["_directory"]["operation_id"],
            "control": {"state": control_state, "mode": mode}, "save": save,
            "workspace": {"registered": True if copy else None, "revision_id": copy["revision"] if copy else None,
                          "reserved_bytes": item.get('runtime_binding',{}).get('reserved_bytes')},
            "resources": {"cpu": unavailable("instance_container"), "memory": unavailable("instance_container"),
                          "gpu": unavailable("exclusive_gpu"), "storage": storage or unavailable("instance_workspace")},
            "_resource_binding": binding,  # Internal only; Studio strips before browser delivery.
            "allocated_gpu_count": None, "allowed_actions": actions,
            "lifecycle_available": lifecycle_available, "lifecycle_reason": None if lifecycle_available else "INSTANCE_LIFECYCLE_UNAVAILABLE"}

    def instance_views(self, subject, org):
        aliases = [alias for alias, item in self.instances().items() if item["organization_id"] == org and subject in item["grants"]]
        def view(alias):
            try:
                return self.instance_view(alias, subject, org)
            except Error as exc:
                if exc.status in {403, 404}:
                    return None
                raise
        with ThreadPoolExecutor(max_workers=4) as pool:
            items = [item for item in pool.map(view, aliases) if item is not None]
        can_create = False
        if self.management.enabled:
            try:
                can_create = bool(self.management.options(subject, org)['profiles'])
            except self.management.errors:
                pass
        return {"schema_version": 1, "capabilities": {"create": can_create}, "instances": items}

    def worker(self, item, payload=None, path=None, *, stop_operation=None, timeout=None):
        dynamic = item['_directory']['source'] == 'dynamic'
        path = path or (("/internal/status" if dynamic else "/readyz") if payload is None else "/internal/rpc")
        if stop_operation is not None and path != '/internal/drain':
            raise Error('INSTANCE_ADMISSION_CLOSED', 409)
        token = self.credential(item) if stop_operation is None else self.credential(item, stop_operation=stop_operation)
        request = urllib.request.Request(endpoint(item["endpoint"]) + path,
            data=None if payload is None else encode(payload).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=timeout or (30 if payload else 3)) as response:
                body = response.read(MAX_RESPONSE + 1)
                if len(body) > MAX_RESPONSE:
                    raise Error("WORKER_RESPONSE_INVALID", 502)
                result = json.loads(body)
                if not isinstance(result, dict):
                    raise ValueError()
                if dynamic and path == '/internal/status':
                    bound = item['execution_binding']['worker']
                    observed = result.get('resource_binding')
                    if (result.get('instance_id') != item['id'] or not isinstance(observed, dict)
                            or any(observed.get(k) != bound.get(k) for k in ('pod_uid', 'pod_name', 'namespace'))
                            or observed.get('container') != 'blender'):
                        raise Error('WORKER_IDENTITY_MISMATCH', 503)
                return result
        except urllib.error.HTTPError as exc:
            if exc.code in {400, 403, 404, 409, 504}:
                try:
                    err_body = json.loads(exc.read(16384))
                    code = err_body.get("code")
                    if code in {
                        "INVALID_ARGUMENT", "SCENE_VERSION_CONFLICT", "STALE_GENERATION", "ASSET_NOT_FOUND",
                        "POLICY_DENIED", "GUI_EDIT_LEASE_HELD", "GUI_LEASE_EXPIRED", "GUI_LEASE_HELD",
                        "GUI_LEASE_MISMATCH", "GUI_PROCESS_UNAVAILABLE", "PROJECT_EXTERNAL_DEPENDENCIES",
                        "PROJECT_FILE_INVALID", "WORKER_BUSY", "WORKER_ACTIVITY_UNKNOWN", "DRAIN_BUSY",
                        "DRAIN_ALREADY_STOPPING", "DRAIN_CONTENT_CHANGED", "DRAIN_STATE_UNKNOWN",
                        "DRAIN_PROCESS_CHANGED", "DRAIN_NOT_FOUND", "DRAIN_OPERATION_CONFLICT",
                        "BATCH_SCRIPT_FAILED", "BATCH_SCRIPT_TIMEOUT", "BATCH_OUTPUT_BLEND_MISSING",
                        "BATCH_EXECUTION_UNAVAILABLE", "EXPORT_QUOTA_EXCEEDED",
                        "WORKSPACE_CLEAN_PATH_INVALID", "OPERATION_TIMEOUT",
                    }:
                        err = Error(code, exc.code)
                        err.data = err_body.get("data")
                        raise err from None
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
            if tool.mutating or tool.name in {"asset.export", "workspace.clean"}:
                schema["properties"]["idempotency_key"] = {"type": "string", "minLength": 8, "maxLength": 128}
                schema["required"].append("idempotency_key")
            out.append(descriptor)
        for name, description, properties, required in [
            ("session.open", "Open an editing or observation session on this instance's assigned project.", {"project_id": {"type": "string"}, "mode": {"type": "string", "enum": ["read", "edit"]}}, ["project_id", "mode"]),
            ("session.close", "Release an editing session. Does not stop Blender.", {"editing_session_id": {"type": "string"}}, ["editing_session_id"]),
            ("operation.get", "Recover a previous operation result; unknown operations must not be blindly retried.", {"operation_id": {"type": "string"}}, ["operation_id"]),
            ("project.save", "Save an immutable Blender checkpoint through Artifact and commit a Project revision. Poll operation.get for completion; conflicts preserve the checkpoint.", {"editing_session_id": {"type": "string"}, "scene_version": {"type": "integer", "minimum": 0}, "idempotency_key": {"type": "string", "minLength": 8, "maxLength": 128}}, ["editing_session_id", "scene_version", "idempotency_key"]),
            (
                "script.run_project",
                "Run a Project Python (.py) batch script in an isolated /workspace/exports/<output_subdir> directory on the worker. Poll operation.get for completion.",
                {
                    "editing_session_id": {"type": "string"},
                    "idempotency_key": {"type": "string", "minLength": 8, "maxLength": 128},
                    "source_project_id": {"type": "string", "description": "Optional source Project UUID containing script_path; defaults to the instance's bound project."},
                    "revision_id": {"type": "string", "description": "Optional fixed Project revision UUID; defaults to latest revision."},
                    "script_path": {"type": "string", "description": "Project-relative .py path, e.g. blender/scripts/saw_set_v1.py."},
                    "output_subdir": {"type": "string", "description": "Subdirectory under /workspace/exports/ where outputs and build.log are written."},
                    "clean_output_subdir": {"type": "boolean", "description": "Whether to clean output_subdir before running."},
                    "load_blend_as_active": {"type": "string", "description": "Optional .blend filename inside output_subdir to open as the active scene upon completion."},
                    "scene_version": {"type": "integer", "minimum": 0, "description": "Current scene_version (required when load_blend_as_active is set)."},
                    "timeout_seconds": {"type": ["integer", "null"], "minimum": 10, "maximum": 600, "description": "Per-batch timeout (default 300, range 10..600). Explicit null uses the existing worker's configured timeout for legacy protocol compatibility; it is not a 300-second guarantee."},
                },
                ["editing_session_id", "idempotency_key", "script_path", "output_subdir"],
            ),
            (
                "project.export_assets",
                "Export generated workspace assets (.blend, .mp4, .json, .log, etc.) to a target Studio Project (verifying caller write permission) and commit a new revision. Poll operation.get for completion.",
                {
                    "editing_session_id": {"type": "string"},
                    "idempotency_key": {"type": "string", "minLength": 8, "maxLength": 128},
                    "target_project_id": {"type": "string", "description": "Target Project UUID to commit files into; defaults to the instance's bound project."},
                    "batch_operation_id": {"type": "string", "description": "Optional operation_id of the script.run_project batch that produced these assets, recorded for provenance."},
                    "provenance_path": {"type": "string", "description": "Optional target Project relative .json path (e.g. blender/provenance.json) to write structured export provenance into the target Project."},
                    "commit_message": {"type": "string", "description": "Optional commit message note."},
                    "files": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 64,
                        "items": {
                            "type": "object",
                            "properties": {
                                "asset_id": {"type": "string", "description": "Worker workspace asset path, e.g. exports/saw-bathroom-v1/SAW_bathroom_set_v1.blend"},
                                "project_path": {"type": "string", "description": "Target Project relative path, e.g. blender/SAW_bathroom_set_v1.blend"},
                                "role": {"type": "string", "enum": ["source", "output", "doc"]},
                            },
                            "required": ["asset_id", "project_path"],
                            "additionalProperties": False,
                        },
                    },
                },
                ["editing_session_id", "idempotency_key", "files"],
            ),
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
            if name != "session.close":
                self.edit_admission(item)
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
                    copy = db.execute("SELECT * FROM working_copies WHERE instance=?", (item["id"],)).fetchone()
                    if copy is not None and copy["project"] != item["project_id"]:
                        raise Error("WORKING_COPY_PROJECT_MISMATCH", 409)
                if copy is None:
                    generation = self.initialize_copy(item, subject, org, args["mode"], generation)
                with self.store.transaction() as db:
                    db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)", (sid, subject, org, item["id"], item["project_id"], generation, args["mode"], time.time() + LEASE_TTL_SECONDS))
                return {"editing_session_id": sid, "instance_id": item["id"], "project_id": item["project_id"], "generation": generation, "mode": args["mode"], "lease_ttl_seconds": LEASE_TTL_SECONDS, "expires_at": now + LEASE_TTL_SECONDS}
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
            async_tools = {"project.save", "script.run_project", "project.export_assets"}
            writing = name in async_tools or name in {"asset.export", "workspace.clean"} or (tool is not None and tool.mutating)
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
                        return {"operation_id": prev["id"], "state": prev["state"], "replayed": False, "async": name in async_tools}
                if name == "workspace.clean":
                    rm_c = args.get("remove_stray_home_c_drive", False)
                    dry_run = args.get("dry_run", False)
                    stray_names = args.get("stray_home_names", [])
                    export_subdirs = args.get("export_subdirs", [])
                    if (
                        type(rm_c) is not bool
                        or type(dry_run) is not bool
                        or not isinstance(stray_names, list)
                        or len(stray_names) > 8
                        or not isinstance(export_subdirs, list)
                        or len(export_subdirs) > 32
                        or any(not isinstance(sub, str) or not SUBDIR_RE.fullmatch(sub) for sub in export_subdirs)
                    ):
                        raise Error("INVALID_TOOL_ARGUMENTS")
                    if any(not isinstance(s, str) or s not in ALLOWED_STRAY_HOME_NAMES for s in stray_names):
                        raise Error("WORKSPACE_CLEAN_PATH_INVALID", 400)
                if name == "script.run_project":
                    source_project_id = args.get("source_project_id") or item["project_id"]
                    revision_id = args.get("revision_id")
                    script_path = args.get("script_path")
                    output_subdir = args.get("output_subdir")
                    clean_output = args.get("clean_output_subdir", False)
                    load_blend = args.get("load_blend_as_active")
                    scene_version = args.get("scene_version")
                    timeout_seconds = args.get("timeout_seconds", 300)
                    if (
                        not valid_id(source_project_id)
                        or (revision_id is not None and not valid_id(revision_id))
                        or not isinstance(script_path, str)
                        or not SCRIPT_PATH_RE.fullmatch(script_path)
                        or ".." in Path(script_path).parts
                        or not isinstance(output_subdir, str)
                        or not SUBDIR_RE.fullmatch(output_subdir)
                        or type(clean_output) is not bool
                        or (timeout_seconds is not None and (type(timeout_seconds) is not int or not 10 <= timeout_seconds <= 600))
                        or (load_blend is not None and (not isinstance(load_blend, str) or not BLEND_NAME_RE.fullmatch(load_blend)))
                        or (load_blend is not None and (type(scene_version) is not int or scene_version < 0))
                    ):
                        raise Error("INVALID_TOOL_ARGUMENTS")
                    if not item.get("file_endpoint"):
                        raise Error("WORKER_FILES_UNAVAILABLE", 503)
                    with self.store.transaction() as db:
                        running_rows = db.execute(
                            "SELECT s.details FROM operations o JOIN saves s ON s.operation=o.id WHERE o.instance=? AND o.state='running'",
                            (item["id"],),
                        ).fetchall()
                        if any(json.loads(r["details"]).get("kind") == "batch" for r in running_rows):
                            raise Error("WORKER_BUSY", 409)
                    try:
                        source_proj = (
                            self.content.open(subject, org, source_project_id, revision=revision_id)
                            if revision_id is not None
                            else self.content.open(subject, org, source_project_id)
                        )
                    except content.ContentError as exc:
                        raise Error(exc.code, exc.status) from None
                    matched = [f for f in source_proj.get("manifest", {}).get("files", []) if f.get("path") == script_path]
                    if len(matched) != 1:
                        raise Error("SCRIPT_NOT_FOUND_IN_PROJECT", 404)
                    operation_id = str(uuid.uuid4())
                    details = {
                        "kind": "batch",
                        "alias": alias,
                        "subject": subject,
                        "org": org,
                        "instance": item["id"],
                        "generation": row["generation"],
                        "source_project": source_project_id,
                        "source_revision": source_proj["revision_id"],
                        "script_path": script_path,
                        "script_file": {k: matched[0][k] for k in ("file_id", "path", "role", "content_ref") if k in matched[0]},
                        "output_subdir": output_subdir,
                        "clean_output_subdir": clean_output,
                        "load_blend_as_active": load_blend,
                        "scene_version": scene_version,
                        "timeout_seconds": timeout_seconds,
                    }
                    with self.store.transaction() as db:
                        db.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?,?)", (operation_id, subject, item["id"], args["idempotency_key"], digest, "running", None, now))
                        db.execute("INSERT INTO saves VALUES (?,?)", (operation_id, encode(details)))
                        db.execute("UPDATE sessions SET expires=? WHERE id=?", (time.time() + LEASE_TTL_SECONDS, row["id"]))
                    self.resume_save(operation_id, alias, subject, org)
                    return {
                        "operation_id": operation_id,
                        "state": "running",
                        "async": True,
                        "source_project_id": source_project_id,
                        "source_revision_id": source_proj["revision_id"],
                        "script_path": script_path,
                        "output_subdir": output_subdir,
                        "timeout_seconds": timeout_seconds,
                    }
                if name == "project.export_assets":
                    target_project_id = args.get("target_project_id") or item["project_id"]
                    batch_operation_id = args.get("batch_operation_id")
                    provenance_path = args.get("provenance_path")
                    files_arg = args.get("files")
                    commit_message = args.get("commit_message")
                    if (
                        not valid_id(target_project_id)
                        or (batch_operation_id is not None and not valid_id(batch_operation_id))
                        or (
                            provenance_path is not None
                            and (
                                not isinstance(provenance_path, str)
                                or not PROJECT_PATH_RE.fullmatch(provenance_path)
                                or not provenance_path.endswith(".json")
                                or ".." in Path(provenance_path).parts
                            )
                        )
                        or not isinstance(files_arg, list)
                        or not 1 <= len(files_arg) <= 64
                        or (commit_message is not None and (not isinstance(commit_message, str) or len(commit_message) > 500))
                    ):
                        raise Error("INVALID_TOOL_ARGUMENTS")
                    seen_paths = set()
                    normalized_files = []
                    for entry in files_arg:
                        if not isinstance(entry, dict) or set(entry) - {"asset_id", "project_path", "role"} or "asset_id" not in entry or "project_path" not in entry:
                            raise Error("INVALID_TOOL_ARGUMENTS")
                        asset_id, project_path = entry["asset_id"], entry["project_path"]
                        if (
                            not isinstance(asset_id, str)
                            or not EXPORT_ASSET_RE.fullmatch(asset_id)
                            or ".." in Path(asset_id).parts
                            or not isinstance(project_path, str)
                            or not PROJECT_PATH_RE.fullmatch(project_path)
                            or ".." in Path(project_path).parts
                            or project_path in seen_paths
                        ):
                            raise Error("INVALID_TOOL_ARGUMENTS")
                        suffix = Path(asset_id).suffix.lower()
                        default_role = "source" if suffix in {".blend", ".py"} else "doc" if suffix in {".json", ".log", ".txt"} else "output"
                        role = entry.get("role") or default_role
                        if role not in {"source", "output", "doc"}:
                            raise Error("INVALID_TOOL_ARGUMENTS")
                        seen_paths.add(project_path)
                        normalized_files.append({"asset_id": asset_id, "project_path": project_path, "role": role})
                    if provenance_path is not None and provenance_path in seen_paths:
                        raise Error("INVALID_TOOL_ARGUMENTS")
                    batch_meta = {}
                    if batch_operation_id is not None:
                        with self.store.transaction() as db:
                            batch_row = db.execute(
                                "SELECT o.state,o.result,s.details FROM operations o JOIN saves s ON s.operation=o.id WHERE o.id=? AND o.instance=?",
                                (batch_operation_id, item["id"]),
                            ).fetchone()
                        if batch_row is None:
                            raise Error("OPERATION_NOT_FOUND", 404)
                        batch_details = json.loads(batch_row["details"])
                        if batch_details.get("kind") != "batch":
                            raise Error("INVALID_TOOL_ARGUMENTS")
                        batch_res = json.loads(batch_row["result"]) if batch_row["result"] else {}
                        batch_meta = {
                            "source_project_id": batch_details.get("source_project"),
                            "source_revision_id": batch_details.get("source_revision"),
                            "script_path": batch_details.get("script_path"),
                            "script_sha256": batch_res.get("script_sha256"),
                            "output_subdir": batch_details.get("output_subdir"),
                        }
                    if not item.get("file_endpoint"):
                        raise Error("WORKER_FILES_UNAVAILABLE", 503)
                    try:
                        target_proj = self.content.open(subject, org, target_project_id, require_write=True)
                    except content.ContentError as exc:
                        raise Error(exc.code, exc.status) from None
                    existing_by_path = {
                        f.get("path"): f.get("file_id")
                        for f in target_proj.get("manifest", {}).get("files", [])
                        if isinstance(f, dict) and f.get("path") and f.get("file_id")
                    }
                    for entry in normalized_files:
                        entry["write_id"] = content.uuid7()
                        if entry["project_path"] in existing_by_path:
                            entry["file_id"] = existing_by_path[entry["project_path"]]
                    provenance_entry = None
                    if provenance_path is not None:
                        provenance_entry = {
                            "project_path": provenance_path,
                            "write_id": content.uuid7(),
                        }
                        if provenance_path in existing_by_path:
                            provenance_entry["file_id"] = existing_by_path[provenance_path]
                    provenance = {
                        "instance_id": item["id"],
                        "instance_alias": alias,
                        "generation": row["generation"],
                        "batch_operation_id": batch_operation_id,
                        "source_project_id": batch_meta.get("source_project_id"),
                        "source_revision_id": batch_meta.get("source_revision_id"),
                        "script_path": batch_meta.get("script_path"),
                        "script_sha256": batch_meta.get("script_sha256"),
                        "output_subdir": batch_meta.get("output_subdir"),
                    }
                    operation_id = str(uuid.uuid4())
                    details = {
                        "kind": "export",
                        "alias": alias,
                        "subject": subject,
                        "org": org,
                        "instance": item["id"],
                        "target_project": target_project_id,
                        "expected_revision": target_proj["revision_id"],
                        "commit_id": content.uuid7(),
                        "commit_message": commit_message,
                        "files": normalized_files,
                        "provenance": provenance,
                        "provenance_entry": provenance_entry,
                    }
                    with self.store.transaction() as db:
                        db.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?,?)", (operation_id, subject, item["id"], args["idempotency_key"], digest, "running", None, now))
                        db.execute("INSERT INTO saves VALUES (?,?)", (operation_id, encode(details)))
                        db.execute("UPDATE sessions SET expires=? WHERE id=?", (time.time() + LEASE_TTL_SECONDS, row["id"]))
                    self.resume_save(operation_id, alias, subject, org)
                    return {
                        "operation_id": operation_id,
                        "state": "running",
                        "async": True,
                        "target_project_id": target_project_id,
                        "expected_revision_id": target_proj["revision_id"],
                        "file_count": len(normalized_files) + (1 if provenance_entry else 0),
                        "provenance": provenance,
                    }
                with self.store.transaction() as db:
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
                    state = "failed" if exc.code == "PROJECT_EXTERNAL_DEPENDENCIES" else "unknown"
                    result = {"operation_id": operation_id, "state": state, "code": exc.code}
                    with self.store.transaction() as db:
                        db.execute("UPDATE operations SET state=?,result=? WHERE id=?", (state, encode(result), operation_id))
                    return result
                raise
            if operation_id:
                result.update(operation_id=operation_id, state="completed")
            with self.store.transaction() as db:
                if operation_id:
                    db.execute("UPDATE operations SET state='completed',result=? WHERE id=?", (encode(result), operation_id))
                db.execute("UPDATE sessions SET expires=? WHERE id=?", (time.time() + 1800, row["id"]))
            return result

    def initialize_copy(self, item, subject, org, mode, generation):
        with self.store.transaction() as db:
            previous = db.execute("SELECT details FROM restores WHERE instance=?", (item["id"],)).fetchone()
            details = json.loads(previous[0]) if previous else None
            files = [f for f in item["project"].get("manifest", {}).get("files", []) if f.get("path") == "blender/main.blend"]
            if len(files) > 1:
                raise Error("PROJECT_RESPONSE_INVALID", 502)
            if details is None and not files:
                db.execute("INSERT INTO working_copies VALUES (?,?,?,?)", (item["id"], item["project_id"], item["project"]["revision_id"], ""))
                return generation
            if mode != "edit":
                raise Error("INITIAL_PROJECT_RESTORE_REQUIRED", 409)
            if db.execute("SELECT 1 FROM sessions WHERE instance=?", (item["id"],)).fetchone():
                raise Error("INSTANCE_EDIT_LEASE_HELD", 409)
            if details is None:
                details = {"restore_id": str(uuid.uuid4()), "project": item["project_id"], "revision": item["project"]["revision_id"], "file": files[0], "generation": generation}
                db.execute("INSERT INTO restores VALUES (?,?)", (item["id"], encode(details)))
        if details["project"] != item["project_id"]:
            raise Error("WORKING_COPY_PROJECT_MISMATCH", 409)
        status = self.worker(item, {"instance_id": item["id"], "generation": generation, "name": "scene.get", "arguments": {}, "request_id": str(uuid.uuid4())})
        if status.get("restore_id") != details["restore_id"]:
            if generation != details["generation"]:
                raise Error("RESTORE_STATE_UNKNOWN", 409)
            path = self.staging / (details["restore_id"] + ".blend")
            try:
                info = self.content.download(subject, org, details["project"], details["revision"], details["file"], path)
            except content.ContentError as exc:
                raise Error(exc.code, exc.status) from None
            if not item.get("file_endpoint"):
                raise Error("WORKER_FILES_UNAVAILABLE", 503)
            try:
                with path.open("rb") as stream:
                    request = urllib.request.Request(endpoint(item["file_endpoint"]) + "/upload", data=stream, headers={
                        "Authorization": "Bearer " + self.credential(item), "Content-Type": "application/octet-stream",
                        "Content-Length": str(info["size"]), "X-Asset-Name": "restore.blend", "X-Asset-SHA256": info["sha256"]})
                    with self.opener.open(request, timeout=240) as response:
                        uploaded = json.loads(response.read(16385))
                if uploaded.get("sha256") != info["sha256"] or not re.fullmatch(r"inbox/[A-Za-z0-9_-]{16}/restore\.blend", uploaded.get("asset_id", "")):
                    raise ValueError()
            except (OSError, ValueError, AttributeError):
                raise Error("WORKER_UPLOAD_FAILED", 503) from None
            try:
                status = self.worker(item, {"instance_id": item["id"], "generation": generation,
                    "restore_id": details["restore_id"], "asset_id": uploaded["asset_id"], **info}, path="/internal/restore")
            except Error as exc:
                if exc.code in {"PROJECT_EXTERNAL_DEPENDENCIES", "PROJECT_FILE_INVALID"}:
                    # Explicit preflight rejection did not load the scene. A
                    # corrected Project revision can be selected next time.
                    with self.store.transaction() as db:
                        db.execute("DELETE FROM restores WHERE instance=?", (item["id"],))
                raise
        if status.get("ok") is not True or status.get("restore_id") != details["restore_id"] or not status.get("generation"):
            raise Error("RESTORE_STATE_UNKNOWN", 409)
        with self.store.transaction() as db:
            db.execute("INSERT INTO working_copies VALUES (?,?,?,?)", (item["id"], details["project"], details["revision"], details["file"]["file_id"]))
        return status["generation"]

    def gui(self, alias, subject, org, action, sid=None):
        item = self.authorize(alias, subject, org)
        with self.lock(item["id"]):
            item = self.authorize(alias, subject, org)
            if action != "close":
                self.edit_admission(item)
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
            kind = details.get("kind", "save")
            if kind == "batch" and row["state"] != "running":
                return
        with self.locks_guard:
            if operation in self.save_threads:
                return
            self.save_threads.add(operation)
        target = self.finish_batch if kind == "batch" else self.finish_export if kind == "export" else self.finish_save
        threading.Thread(target=target, args=(operation, details), daemon=True).start()

    def finish_batch(self, operation, details):
        subject, org = details["subject"], details["org"]
        script_name = Path(details["script_path"]).name
        local_path = self.staging / f"{operation}-{script_name}"
        try:
            item = self.authorize(details["alias"], subject, org)
            if item["id"] != details["instance"] or item["grants"][subject] != "edit":
                raise Error("READ_ONLY", 403)
            if not item.get("file_endpoint"):
                raise Error("WORKER_FILES_UNAVAILABLE", 503)
            info = self.content.download(
                subject, org, details["source_project"], details["source_revision"], details["script_file"], local_path
            )
            with local_path.open("rb") as stream:
                req = urllib.request.Request(
                    endpoint(item["file_endpoint"]) + "/upload",
                    data=stream,
                    headers={
                        "Authorization": "Bearer " + self.credential(item),
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(info["size"]),
                        "X-Asset-Name": script_name,
                        "X-Asset-SHA256": info["sha256"],
                    },
                )
                with self.opener.open(req, timeout=120) as response:
                    uploaded = json.loads(response.read(16385))
            if uploaded.get("sha256") != info["sha256"] or not str(uploaded.get("asset_id", "")).startswith("inbox/"):
                raise Error("WORKER_UPLOAD_FAILED", 503)
            timeout_seconds = details.get("timeout_seconds", 300)
            batch_payload = {
                "instance_id": item["id"],
                "generation": details["generation"],
                "request_id": operation,
                "script_asset_id": uploaded["asset_id"],
                "script_sha256": info["sha256"],
                "script_size": info["size"],
                "output_subdir": details["output_subdir"],
                "clean_output_subdir": details["clean_output_subdir"],
            }
            if timeout_seconds is not None:
                batch_payload["timeout_seconds"] = timeout_seconds
            if details.get("load_blend_as_active") is not None:
                batch_payload["load_blend_as_active"] = details["load_blend_as_active"]
                batch_payload["scene_version"] = details["scene_version"]
            result = self.worker(item, batch_payload, path="/internal/batch", timeout=(timeout_seconds if timeout_seconds is not None else 600) + 30)
            if result.get("ok") is not True or result.get("generation") != details["generation"]:
                raise Error("WORKER_RESPONSE_INVALID", 502)
            value = {
                "operation_id": operation,
                "state": "completed",
                "source_project_id": details["source_project"],
                "source_revision_id": details["source_revision"],
                "script_path": details["script_path"],
                "script_sha256": info["sha256"],
                "timeout_seconds": timeout_seconds,
                **{k: v for k, v in result.items() if k != "ok"},
            }
            with self.store.transaction() as db:
                db.execute("UPDATE operations SET state='completed',result=? WHERE id=?", (encode(value), operation))
        except (Error, content.ContentError, OSError, ValueError, KeyError) as exc:
            code = getattr(exc, "code", "WORKER_UNAVAILABLE")
            state = (
                "failed"
                if code in {
                    "BATCH_SCRIPT_FAILED", "BATCH_SCRIPT_TIMEOUT", "BATCH_OUTPUT_BLEND_MISSING",
                    "EXPORT_QUOTA_EXCEEDED", "WORKER_BUSY", "INVALID_ARGUMENT",
                    "ASSET_NOT_FOUND", "STALE_GENERATION", "SCENE_VERSION_CONFLICT",
                    "PERMISSION_DENIED", "READ_ONLY", "PROJECT_EXTERNAL_DEPENDENCIES",
                }
                else "unknown"
            )
            err_payload = {"operation_id": operation, "state": state, "code": code}
            if getattr(exc, "data", None) is not None:
                err_payload["data"] = exc.data
            with self.store.transaction() as db:
                db.execute("UPDATE operations SET state=?,result=? WHERE id=?", (state, encode(err_payload), operation))
        finally:
            local_path.unlink(missing_ok=True)
            with self.locks_guard:
                self.save_threads.discard(operation)

    def finish_export(self, operation, details):
        subject, org, target_project = details["subject"], details["org"], details["target_project"]
        try:
            item = self.authorize(details["alias"], subject, org)
            if item["id"] != details["instance"] or item["grants"][subject] != "edit":
                raise Error("SAVE_PERMISSION_DENIED", 403)
            self.content.open(subject, org, target_project, require_write=True)
            with self.store.transaction() as db:
                db.execute("UPDATE operations SET state='running' WHERE id=?", (operation,))
            upserts = []
            exported_meta = []
            for idx, entry in enumerate(details["files"]):
                suffix = Path(entry["asset_id"]).suffix.lower()
                path = self.staging / f"{operation}-{idx}{suffix}"
                if not path.exists():
                    request = urllib.request.Request(
                        endpoint(item["file_endpoint"]) + "/assets/" + urllib.parse.quote(entry["asset_id"], safe="/"),
                        headers={"Authorization": "Bearer " + self.credential(item)},
                    )
                    tmp = path.with_suffix(suffix + ".partial")
                    try:
                        with self.opener.open(request, timeout=240) as response, tmp.open("wb") as stream:
                            size = int(response.headers.get("Content-Length", "-1"))
                            expected = response.headers.get("X-Asset-SHA256", "")
                            if not 0 < size <= content.MAX_FILE or not re.fullmatch(r"[0-9a-f]{64}", expected):
                                raise Error("EXPORT_ASSET_INVALID", 502)
                            digest, count = hashlib.sha256(), 0
                            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                                count += len(chunk)
                                if count > size:
                                    raise Error("EXPORT_ASSET_INVALID", 502)
                                stream.write(chunk)
                                digest.update(chunk)
                            if count != size or digest.hexdigest() != expected:
                                raise Error("EXPORT_ASSET_HASH_MISMATCH", 502)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(tmp, path)
                    finally:
                        tmp.unlink(missing_ok=True)
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                size = path.stat().st_size
                mime = ASSET_MIME.get(suffix, "application/octet-stream")
                if mime == "application/x-blender":
                    ref = self.content.upload(subject, org, target_project, entry["write_id"], path, digest, size)
                else:
                    ref = self.content.upload(subject, org, target_project, entry["write_id"], path, digest, size, mime)
                file_update = {"path": entry["project_path"], "role": entry["role"], "content_ref": ref}
                if entry.get("file_id"):
                    file_update["file_id"] = entry["file_id"]
                upserts.append(file_update)
                exported_meta.append({
                    "asset_id": entry["asset_id"],
                    "project_path": entry["project_path"],
                    "role": entry["role"],
                    "content_ref": ref,
                    "sha256": digest,
                    "size": size,
                    "mime": mime,
                })
            saved_at = datetime.now(timezone.utc).isoformat()
            provenance = dict(details.get("provenance") or {
                "instance_id": item["id"],
                "instance_alias": details["alias"],
            })
            provenance["export_operation_id"] = operation
            provenance["target_project_id"] = target_project
            provenance["commit_message"] = details.get("commit_message")
            provenance["exported_at"] = saved_at
            prov_entry = details.get("provenance_entry")
            if prov_entry:
                prov_doc = {
                    **provenance,
                    "files": [
                        {
                            "asset_id": m["asset_id"],
                            "project_path": m["project_path"],
                            "role": m["role"],
                            "sha256": m["sha256"],
                            "size": m["size"],
                        }
                        for m in exported_meta
                    ],
                }
                prov_bytes = (json.dumps(prov_doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
                prov_digest = hashlib.sha256(prov_bytes).hexdigest()
                prov_path = self.staging / f"{operation}-provenance.json"
                prov_path.write_bytes(prov_bytes)
                prov_ref = self.content.upload(
                    subject, org, target_project, prov_entry["write_id"], prov_path, prov_digest, len(prov_bytes), "application/json"
                )
                prov_update = {"path": prov_entry["project_path"], "role": "doc", "content_ref": prov_ref}
                if prov_entry.get("file_id"):
                    prov_update["file_id"] = prov_entry["file_id"]
                upserts.append(prov_update)
                exported_meta.append({
                    "asset_id": "provenance",
                    "project_path": prov_entry["project_path"],
                    "role": "doc",
                    "content_ref": prov_ref,
                    "sha256": prov_digest,
                    "size": len(prov_bytes),
                    "mime": "application/json",
                })
            result = self.content.request(
                subject,
                org,
                "POST",
                "/project/commit",
                {
                    "project_id": target_project,
                    "expected_revision_id": details["expected_revision"],
                    "commit_id": details["commit_id"],
                    "changes": {"upsert_files": upserts},
                },
            )
            if result.get("project_id") != target_project or not content.ID.fullmatch(result.get("revision_id", "")):
                raise Error("PROJECT_RESPONSE_INVALID", 502)
            manifest_files = result.get("manifest", {}).get("files", [])
            for meta in exported_meta:
                matched = [
                    f for f in manifest_files
                    if f.get("path") == meta["project_path"] and f.get("content_ref") == meta["content_ref"]
                ]
                if len(matched) != 1 or not content.ID.fullmatch(matched[0].get("file_id", "")):
                    raise Error("PROJECT_RESPONSE_INVALID", 502)
                meta["file_id"] = matched[0]["file_id"]
            value = {
                "operation_id": operation,
                "state": "completed",
                "target_project_id": target_project,
                "revision_id": result["revision_id"],
                "files": exported_meta,
                "provenance": provenance,
                "saved_at": saved_at,
            }
            with self.store.transaction() as db:
                db.execute("UPDATE operations SET state='completed',result=? WHERE id=?", (encode(value), operation))
        except (Error, content.ContentError, OSError, ValueError, KeyError) as exc:
            code = getattr(exc, "code", "CONTENT_SERVICE_UNAVAILABLE")
            state = "failed" if getattr(exc, "status", 503) in {400, 403, 404, 409} and code not in {"COMMIT_IN_PROGRESS", "CONTENT_NOT_READY"} else "retryable"
            with self.store.transaction() as db:
                db.execute("UPDATE operations SET state=?,result=? WHERE id=?", (state, encode({"code": code, "target_project_id": target_project, "commit_id": details["commit_id"]}), operation))
        finally:
            with self.locks_guard:
                self.save_threads.discard(operation)

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
                request = urllib.request.Request(endpoint(item["file_endpoint"]) + "/assets/" + details["asset"], headers={"Authorization": "Bearer " + self.credential(item)})
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
            value = {"operation_id": operation, "state": "completed", "project_id": project_id, "revision_id": result["revision_id"], "content_ref": ref, "sha256": digest, "size": size, "scene_version": details["scene_version"], "saved_at": datetime.now(timezone.utc).isoformat()}
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
            if self.command == 'GET' and self.path == '/internal/instance-options':
                return self.management_reply(lambda: self.server.app.management.options(subject, org))
            if self.command == 'GET' and self.path.startswith('/internal/instance-operations/'):
                operation = self.path.removeprefix('/internal/instance-operations/')
                return self.management_reply(lambda: self.server.app.management.operation(subject, org, operation))
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
                auth = base64.b64encode(("beagle:" + self.server.app.credential(item, 'gui')).encode()).decode()
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
                return self.reply(200, self.server.app.instance_views(subject, org))
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

    def management_reply(self, effect, status=200):
        try:
            return self.reply(status, effect())
        except (Error, management.ManagementError, content.ContentError, creation.CreationError) as exc:
            return self.reply(exc.status, {'code': exc.code})
        except directory.Fault as exc:
            code = str(exc)
            return self.reply(404 if code == 'OPERATION_NOT_FOUND' else 409, {'code': code})
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
            return self.reply(503, {'code': 'MANAGEMENT_UNAVAILABLE'})

    def management_create(self, subject, org, start_alias=None, command='start'):
        try:
            if (self.headers.get('Transfer-Encoding') or self.headers.get('Content-Encoding')
                    or len(self.headers.get_all('Content-Length', [])) != 1 or self.headers.get_content_type() != 'application/json'):
                raise ValueError()
            length = int(self.headers['Content-Length'])
            if not 0 < length <= 16384:
                raise ValueError()
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError()
                    result[key] = value
                return result
            def invalid_constant(value):
                raise ValueError()
            request = json.loads(self.rfile.read(length), object_pairs_hook=unique,
                                 parse_constant=invalid_constant)
        except (ValueError, TypeError):
            return self.reply(400, {'code': 'INVALID_ARGUMENT'})
        if start_alias is not None:
            method = {'start':self.server.app.management.start_instance,'stop':self.server.app.management.stop_instance,
                      'destroy':self.server.app.management.destroy_instance}[command]
            return self.management_reply(lambda: method(subject, org, start_alias, request), 202)
        actions = {'/internal/instances': ('create',202), '/internal/instance-projects': ('projects',200),
                   '/internal/instance-source': ('source',200), '/internal/instance-project-create': ('new_project',200)}
        action, status = actions[self.path]
        return self.management_reply(lambda: getattr(self.server.app.management, action)(subject, org, request), status)

    def do_POST(self):
        request_id = None
        try:
            subject, org = self.identity()
            if self.path in {'/internal/instances','/internal/instance-projects','/internal/instance-source','/internal/instance-project-create'}:
                return self.management_create(subject, org)
            start = re.fullmatch(r'/internal/instances/(blender[A-Za-z0-9_-]{1,57})/(start|stop|destroy)', self.path)
            if start:
                return self.management_create(subject, org, start[1], start[2])
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
    app.management.start()
    try:
        Server(("0.0.0.0", int(os.environ.get("PORT", "8080"))), app).serve_forever()
    finally:
        app.management.stop_event.set()
