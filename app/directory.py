"""Persistent instance identity and management ledger, separate from scene edits.

Authorization belongs to the application coordinator. All transitions here are
atomic; external effects must happen only after begin_* has committed.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


class Fault(Exception):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS instance_directory (
  id TEXT PRIMARY KEY, alias TEXT NOT NULL UNIQUE, org TEXT NOT NULL,
  project TEXT NOT NULL, source TEXT NOT NULL, enabled INTEGER NOT NULL,
  config TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL,
  operation TEXT, evidence TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS instance_commands (
  id TEXT PRIMARY KEY, instance TEXT NOT NULL, org TEXT NOT NULL, subject TEXT NOT NULL,
  key TEXT NOT NULL, digest TEXT NOT NULL, action TEXT NOT NULL, input TEXT NOT NULL,
  state TEXT NOT NULL, phase TEXT NOT NULL, result TEXT NOT NULL,
  created REAL NOT NULL, updated REAL NOT NULL, UNIQUE(org,subject,key));
CREATE UNIQUE INDEX IF NOT EXISTS instance_one_active_command
  ON instance_commands(instance) WHERE state='running';
CREATE TABLE IF NOT EXISTS instance_events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT, instance TEXT NOT NULL,
  operation TEXT NOT NULL, phase TEXT NOT NULL, state TEXT NOT NULL,
  details TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS retained_workspaces (
  instance TEXT PRIMARY KEY, org TEXT NOT NULL, project TEXT NOT NULL,
  details TEXT NOT NULL, retained_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS instance_upload_receipts (
  operation TEXT PRIMARY KEY, asset_id TEXT NOT NULL, pod_uid TEXT NOT NULL);
"""


class Directory:
    def __init__(self, store, new_id):
        self.store, self.new_id = store, new_id
        with store.lock:
            store.db.executescript(SCHEMA)

    def sync_declared(self, entries):
        """Refresh operator ACL/config without replacing historical identities."""
        now = time.time()
        with self.store.transaction() as db:
            db.execute("UPDATE instance_directory SET enabled=0 WHERE source='declared' AND enabled=1")
            for alias, config in entries.items():
                row = db.execute("SELECT * FROM instance_directory WHERE alias=? OR id=?", (alias, config["id"])).fetchone()
                if row:
                    if (row["id"], row["alias"], row["org"], row["project"], row["source"]) != (
                            config["id"], alias, config["organization_id"], config["project_id"], "declared"):
                        raise Fault("INSTANCE_IDENTITY_CONFLICT")
                    if row["state"] == "deleted":
                        continue
                    previous = json.loads(row["config"])
                    if row["operation"] and any(previous.get(k) != config.get(k) for k in (
                            "endpoint", "file_endpoint", "gui_endpoint", "token_env", "gui_auth_env")):
                        raise Fault("INSTANCE_CONFIGURATION_BUSY")
                    changed = row["config"] != encode(config)
                    db.execute("UPDATE instance_directory SET config=?,enabled=1,version=version+?,updated=? WHERE id=?",
                               (encode(config), int(changed), now if changed else row["updated"], row["id"]))
                else:
                    db.execute("INSERT INTO instance_directory VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (config["id"], alias, config["organization_id"], config["project_id"], "declared", 1,
                                encode(config), "unknown", 1, None, "{}", now, now))

    def entries(self):
        with self.store.transaction() as db:
            rows = db.execute("SELECT * FROM instance_directory WHERE enabled=1 AND state!='deleted' ORDER BY created,alias").fetchall()
        return {row["alias"]: self.entry(row) for row in rows}

    @staticmethod
    def entry(row):
        config = json.loads(row["config"])
        config["_directory"] = {"source": row["source"], "state": row["state"], "version": row["version"],
                                "operation_id": row["operation"], "evidence": json.loads(row["evidence"]), "updated": row["updated"]}
        return config

    @staticmethod
    def replay(db, org, subject, key, digest):
        row = db.execute("SELECT * FROM instance_commands WHERE org=? AND subject=? AND key=?", (org, subject, key)).fetchone()
        if row and row["digest"] != digest:
            raise Fault("IDEMPOTENCY_CONFLICT")
        return dict(row) if row else None

    @staticmethod
    def digest(action, instance, payload):
        return hashlib.sha256(encode({"action": action, "instance": instance, "input": payload}).encode()).hexdigest()

    def command(self, db, instance, org, subject, key, action, payload, digest):
        op, now = self.new_id(), time.time()
        db.execute("INSERT INTO instance_commands VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (op, instance, org, subject, key, digest, action, encode(payload), "running", "accepted", "{}", now, now))
        # Keep the event and command acceptance in the same commit as the instance.
        db.execute("INSERT INTO instance_events(instance,operation,phase,state,details,created) VALUES (?,?,?,?,?,?)",
                   (instance, op, "accepted", "running", "{}", now))
        return op

    def begin_create(self, org, subject, key, payload, alias, config):
        digest = self.digest("create", None, payload)
        with self.store.transaction() as db:
            previous = self.replay(db, org, subject, key, digest)
            if previous:
                return previous
            if config["organization_id"] != org or config.get("grants") != {subject: "edit"} or config.get("managers") != [subject]:
                raise Fault("INVALID_INITIAL_AUTHORITY")
            if config.get('name'):
                names = db.execute("SELECT alias,config FROM instance_directory WHERE org=? AND enabled=1 AND state!='deleted'", (org,)).fetchall()
                if any(json.loads(row['config']).get('name', row['alias'].replace('blender', 'Blender ', 1)).strip().casefold() == config['name'].strip().casefold() for row in names):
                    raise Fault('INSTANCE_NAME_CONFLICT')
            if db.execute("SELECT 1 FROM instance_directory WHERE alias=? OR id=?", (alias, config["id"])).fetchone():
                raise Fault("INSTANCE_IDENTITY_CONFLICT")
            now = time.time()
            op = self.command(db, config["id"], org, subject, key, "create", payload, digest)
            db.execute("INSERT INTO instance_directory VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (config["id"], alias, org, config["project_id"], "dynamic", 1, encode(config), "creating", 1, op, "{}", now, now))
            return dict(db.execute("SELECT * FROM instance_commands WHERE id=?", (op,)).fetchone())

    def begin_command(self, instance, org, subject, key, action, expected_version, payload):
        targets = {"start": ("stopped", "starting"), "stop": ("running", "stopping"), "destroy": ("stopped", "deleting")}
        if action not in targets or type(expected_version) is not int or expected_version < 1:
            raise Fault("INVALID_ARGUMENT")
        digest = self.digest(action, instance, {"expected_version": expected_version, "payload": payload})
        with self.store.transaction() as db:
            previous = self.replay(db, org, subject, key, digest)
            if previous:
                return previous
            row = db.execute("SELECT * FROM instance_directory WHERE id=? AND org=? AND enabled=1", (instance, org)).fetchone()
            if row is None or row["state"] == "deleted":
                raise Fault("INSTANCE_NOT_FOUND")
            if row["operation"]:
                raise Fault("INSTANCE_BUSY")
            if row["version"] != expected_version or row["state"] != targets[action][0]:
                raise Fault("INSTANCE_STATE_CONFLICT")
            op = self.command(db, instance, org, subject, key, action, payload, digest)
            db.execute("UPDATE instance_directory SET state=?,version=version+1,operation=?,updated=? WHERE id=?",
                       (targets[action][1], op, time.time(), instance))
            return dict(db.execute("SELECT * FROM instance_commands WHERE id=?", (op,)).fetchone())

    def advance(self, operation, expected_version, phase, evidence, final_state=None, binding=None, retained=None, working_copy=None):
        """Commit verified execution evidence; only the coordinator may call this."""
        if not isinstance(evidence, dict) or not isinstance(phase, str) or not phase:
            raise Fault("INVALID_ARGUMENT")
        with self.store.transaction() as db:
            command = db.execute("SELECT * FROM instance_commands WHERE id=?", (operation,)).fetchone()
            if command is None:
                raise Fault("OPERATION_NOT_FOUND")
            row = db.execute("SELECT * FROM instance_directory WHERE id=?", (command["instance"],)).fetchone()
            result = encode({"instance_state": final_state, "evidence": evidence, "binding": binding, "retained": retained})
            if command["state"] != "running":
                if command["phase"] == phase and command["result"] == result:
                    return dict(command)
                raise Fault("OPERATION_STATE_CONFLICT")
            if row["operation"] != operation:
                raise Fault("OPERATION_STATE_CONFLICT")
            if type(expected_version) is not int or row["version"] != expected_version:
                raise Fault("INSTANCE_STATE_CONFLICT")
            desired = {"create": "stopped", "start": "running", "stop": "stopped", "destroy": "deleted"}[command["action"]]
            if final_state is not None and final_state not in {desired, "failed"}:
                raise Fault("INSTANCE_STATE_CONFLICT")
            if working_copy is not None:
                if command["action"] != "create" or final_state != "stopped" or set(working_copy) != {"project", "revision", "file_id"} or working_copy["project"] != row["project"]:
                    raise Fault("INVALID_WORKING_COPY")
                previous = db.execute("SELECT project,revision,file_id FROM working_copies WHERE instance=?", (row["id"],)).fetchone()
                if previous is not None and dict(previous) != working_copy:
                    raise Fault("WORKING_COPY_CONFLICT")
                db.execute("INSERT OR IGNORE INTO working_copies VALUES (?,?,?,?)",
                           (row["id"], working_copy["project"], working_copy["revision"], working_copy["file_id"]))
            if final_state == "deleted":
                if not isinstance(retained, dict) or not retained.get("workspace_id"):
                    raise Fault("WORKSPACE_RETENTION_REQUIRED")
                db.execute("INSERT INTO retained_workspaces VALUES (?,?,?,?,?)",
                           (row["id"], row["org"], row["project"], encode(retained), time.time()))
            config = json.loads(row["config"])
            if binding is not None:
                # Runtime binding cannot retarget identity, ACL, project or organization.
                allowed = {"endpoint", "file_endpoint", "gui_endpoint", "token_env", "gui_auth_env", "runtime_binding", "execution_binding"}
                if not isinstance(binding, dict) or set(binding) - allowed:
                    raise Fault("INVALID_EXECUTION_BINDING")
                config.update(binding)
            state = "failed" if final_state == "failed" else "completed" if final_state else "running"
            now = time.time()
            if phase == command["phase"] and result == command["result"] and encode(config) == row["config"]:
                return dict(command)
            db.execute("UPDATE instance_commands SET state=?,phase=?,result=?,updated=? WHERE id=?", (state, phase, result, now, operation))
            db.execute("UPDATE instance_directory SET state=?,version=version+1,operation=?,evidence=?,config=?,updated=? WHERE id=?",
                       (final_state or row["state"], None if final_state else operation, encode(evidence), encode(config), now, row["id"]))
            db.execute("INSERT INTO instance_events(instance,operation,phase,state,details,created) VALUES (?,?,?,?,?,?)",
                       (row["id"], operation, phase, state, encode(evidence), now))
            return dict(db.execute("SELECT * FROM instance_commands WHERE id=?", (operation,)).fetchone())

    def pending(self):
        with self.store.transaction() as db:
            return [dict(row) for row in db.execute("SELECT * FROM instance_commands WHERE state='running' ORDER BY created,id")]

    def last_command(self, instance, action):
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM instance_commands WHERE instance=? AND action=? ORDER BY created DESC,id DESC LIMIT 1', (instance, action)).fetchone()
        return dict(row) if row else None

    def keyed_operation(self, org, subject, key):
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM instance_commands WHERE org=? AND subject=? AND key=?", (org, subject, key)).fetchone()
        return dict(row) if row else None

    def upload_receipt(self, operation, receipt=None):
        with self.store.transaction() as db:
            row = db.execute("SELECT asset_id,pod_uid FROM instance_upload_receipts WHERE operation=?", (operation,)).fetchone()
            if receipt is None:
                return dict(row) if row else None
            if (not isinstance(receipt, dict) or set(receipt) != {"asset_id", "pod_uid"}
                    or not isinstance(receipt['asset_id'], str) or not re.fullmatch(r'inbox/[A-Za-z0-9_-]{16}/restore\.blend', receipt['asset_id'])
                    or not isinstance(receipt['pod_uid'], str) or not re.fullmatch(r'[0-9a-f-]{36}', receipt['pod_uid'])):
                raise Fault("INVALID_UPLOAD_RECEIPT")
            try:
                pod_uid = uuid.UUID(receipt['pod_uid'])
                if str(pod_uid) != receipt['pod_uid'] or pod_uid.int == 0:
                    raise ValueError()
            except ValueError:
                raise Fault("INVALID_UPLOAD_RECEIPT") from None
            command = db.execute("SELECT state,action FROM instance_commands WHERE id=?", (operation,)).fetchone()
            if command is None or command['state'] != 'running' or command['action'] != 'create':
                raise Fault("OPERATION_STATE_CONFLICT")
            if row is not None and dict(row) != receipt:
                raise Fault("WORKSPACE_BINDING_CONFLICT")
            db.execute("INSERT OR IGNORE INTO instance_upload_receipts VALUES (?,?,?)", (operation, receipt['asset_id'], receipt['pod_uid']))

    def operation(self, operation, org, subject):
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM instance_commands WHERE id=? AND org=? AND subject=?", (operation, org, subject)).fetchone()
        if row is None:
            raise Fault("OPERATION_NOT_FOUND")
        return dict(row)
