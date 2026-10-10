"""Blender-side adapter for the Beagle Blender MCP bridge.

The socket worker never touches ``bpy``.  Requests are queued and drained by a
timer on Blender's main thread, which keeps Blender's data API out of a worker
thread and gives the bridge a small, explicit operation allowlist.
"""

from __future__ import annotations

import json
import hashlib
import importlib.util
import math
import os
import queue
import re
import socketserver
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import bpy  # type: ignore

_dependency_spec = importlib.util.spec_from_file_location("blender_project_dependencies", Path(__file__).parents[1] / "project_dependencies.py")
_dependencies = importlib.util.module_from_spec(_dependency_spec)
_dependency_spec.loader.exec_module(_dependencies)
_drain_spec = importlib.util.spec_from_file_location('blender_instance_drain', Path(__file__).parents[1] / 'instance_drain.py')
_drain = importlib.util.module_from_spec(_drain_spec)
_drain_spec.loader.exec_module(_drain)
_DRAIN = _drain.Journal()


bl_info = {
    "name": "Beagle Blender MCP adapter",
    "author": "Beagle",
    "version": (0, 1, 0),
    "blender": (4, 5, 0),
    "location": "",
    "category": "System",
}


_WORKSPACE = Path(os.environ.get("WORKSPACE_ROOT", "/workspace")).resolve()
_SOCKET_PATH = Path(os.environ.get("BLENDER_MCP_ADAPTER_SOCKET", "/run/user/1000/blender-mcp-adapter.sock"))
_QUEUE: "queue.Queue[Task]" = queue.Queue()
_SERVER: socketserver.BaseServer | None = None
_THREAD: threading.Thread | None = None
_SCENE_VERSION = 0
_GENERATION = uuid.uuid4().hex
_LOAD_HANDLER = None
_RESTORE_ID = None
_STARTUP_PROOF = getattr(sys.modules.get("blender_startup_workspace"), "proof", None)


@dataclass
class Task:
    request: dict[str, Any]
    done: threading.Event
    response: dict[str, Any] | None = None


def _error(code: str, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"ok": False, "error": error}


def _ok(**values: Any) -> dict[str, Any]:
    return {"ok": True, "generation": _GENERATION, "scene_version": _SCENE_VERSION, **values}


def _vector(value: Any, name: str) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{name} must contain three numbers")
    result = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in result):
        raise ValueError(f"{name} contains a non-finite number")
    return result  # type: ignore[return-value]


def _object_or_error(object_id: Any) -> Any:
    if not isinstance(object_id, str) or not object_id or len(object_id) > 128 or "\x00" in object_id:
        raise ValueError("object_id is invalid")
    obj = bpy.data.objects.get(object_id)
    if obj is None:
        raise KeyError(object_id)
    return obj


def _safe_workspace_file(relative_path: str) -> Path:
    unresolved = _WORKSPACE / relative_path
    current = _WORKSPACE
    for part in Path(relative_path).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("symlink paths are not allowed")
    candidate = unresolved.resolve()
    if candidate != _WORKSPACE and _WORKSPACE not in candidate.parents:
        raise ValueError("path escapes workspace")
    return candidate


def _save(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    result = bpy.ops.wm.save_as_mainfile(filepath=str(path))
    if result != {'FINISHED'} or not path.is_file() or path.stat().st_size == 0:
        raise OSError("Blender did not produce a valid project file")
    with path.open('rb') as stream:
        os.fsync(stream.fileno())


def _checkpoint() -> Path:
    target = _safe_workspace_file('project/main.blend')
    _save(target)
    checkpoint = _safe_workspace_file(f'checkpoints/checkpoint-{_SCENE_VERSION}-{uuid.uuid4().hex}.blend')
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    with target.open('rb') as source, checkpoint.open('xb') as output:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            output.write(chunk)
        output.flush();os.fsync(output.fileno())
    for parent in (target.parent, checkpoint.parent):
        descriptor = os.open(parent, os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return checkpoint


def _asset_file(asset_id: Any, *, prefix: str | None = None) -> Path:
    if not isinstance(asset_id, str) or not asset_id or len(asset_id) > 256:
        raise ValueError("asset_id is invalid")
    if prefix and not asset_id.startswith(prefix):
        raise ValueError(f"asset_id must be under {prefix}")
    path = _safe_workspace_file(asset_id)
    if path.suffix.lower() not in {".blend", ".glb", ".gltf", ".png", ".jpg", ".jpeg", ".exr", ".tif", ".tiff"}:
        raise ValueError("asset extension is not allowed")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _execute(request: dict[str, Any]) -> dict[str, Any]:
    global _SCENE_VERSION, _RESTORE_ID
    if request.get("protocol_version") != 1:
        return _error("UPSTREAM_UNAVAILABLE", "unsupported adapter protocol")
    deadline = request.get("deadline")
    if type(deadline) is not int or deadline < int(time.time() * 1000):
        return _error("OPERATION_TIMEOUT", "request expired before execution")
    if request.get("operation") != "scene.get" or request.get("generation") is not None:
        if request.get("generation") != _GENERATION:
            return _error("STALE_GENERATION", "Blender process changed; open a new editing session")
    operation = request.get("operation")
    args = request.get("args") or {}
    if not isinstance(operation, str) or not isinstance(args, dict):
        return _error("INVALID_ARGUMENT", "operation and args are required")
    mutating = operation in {"object.create", "object.update_transform", "object.delete", "scene.save", "scene.checkpoint", "asset.import"}
    requested_version = request.get("scene_version")
    if mutating and (type(requested_version) is not int or requested_version != _SCENE_VERSION):
        return _error("SCENE_VERSION_CONFLICT", "scene version is stale", {"current": _SCENE_VERSION})
    try:
        if operation == 'instance.resume':
            return _ok(**_DRAIN.resumed(args))
        if operation == 'instance.drain':
            with _DRAIN.locked():
                record = _DRAIN.read()
                _DRAIN.match(args, record)
                if record is None or record['phase'] != 'accepted':
                    raise _drain.DrainError('DRAIN_OPERATION_CONFLICT')
                _drain.idle(bpy)
                if _dependencies.external_dependencies(bpy):
                    return _error('PROJECT_EXTERNAL_DEPENDENCIES', 'Pack external resources before stopping.')
                snapshot = _checkpoint()
                _SCENE_VERSION += 1
                if _drain.idle(bpy):
                    raise _drain.DrainError('DRAIN_CONTENT_CHANGED')
                live = _drain.process(os.getpid())
                record.update(phase='captured', proof={**_drain.content(snapshot), 'asset_id':snapshot.relative_to(_WORKSPACE).as_posix(),
                    'scene_version':_SCENE_VERSION, 'pid':live['pid'], 'start_ticks':live['start_ticks']})
                _DRAIN.write(record)
            # This main thread does not return to Blender's event loop between
            # snapshot and self-STOP. The controller verifies /proc independently.
            os.kill(os.getpid(), _drain.signal.SIGSTOP)
            return _ok(operation_id=record['operation_id'])
        _DRAIN.guard()
        if operation == "scene.get":
            return _ok(scene={"name": bpy.context.scene.name, "filepath": str(Path(bpy.data.filepath).name)}, restore_id=_RESTORE_ID, startup=_STARTUP_PROOF)
        if operation == "scene.restore":
            restore_id, asset = args.get("restore_id"), args.get("asset_id")
            if not isinstance(restore_id, str) or str(uuid.UUID(restore_id)) != restore_id:
                raise ValueError("restore_id is invalid")
            if _RESTORE_ID == restore_id:
                return _ok(restore_id=_RESTORE_ID)
            if not isinstance(asset, str) or not re.fullmatch(r"inbox/[A-Za-z0-9_-]{16}/restore\.blend", asset):
                raise ValueError("restore asset is invalid")
            path = _asset_file(asset, prefix="inbox/")
            if type(args.get("size")) is not int or not 0 < args["size"] <= 512 * 1024 * 1024 or path.stat().st_size != args["size"] or _sha256(path) != args.get("sha256"):
                raise ValueError("restore content integrity mismatch")
            # Preserve the entire current scene before any load. Never replace
            # a user's checkpoint, and keep automatic Python execution disabled.
            checkpoint = _safe_workspace_file("checkpoints/pre-restore-" + uuid.uuid4().hex + ".blend")
            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            bpy.ops.wm.save_as_mainfile(filepath=str(checkpoint), copy=True)
            if not checkpoint.is_file() or not checkpoint.stat().st_size:
                raise OSError("pre-restore checkpoint failed")
            bpy.ops.wm.open_mainfile(filepath=str(path), load_ui=False, use_scripts=False)
            target = _safe_workspace_file("project/main.blend")
            _save(target)
            receipt = _safe_workspace_file("project/restore.json")
            temporary = receipt.with_suffix(".partial")
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump({"restore_id": restore_id, "sha256": _sha256(target)}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, receipt)
            _RESTORE_ID = restore_id
            return _ok(restore_id=_RESTORE_ID)
        if operation == "scene.list_objects":
            return _ok(objects=[{"object_id": obj.name, "type": obj.type} for obj in bpy.context.scene.objects])
        if operation == "object.get":
            obj = _object_or_error(args.get("object_id"))
            return _ok(object={
                "object_id": obj.name,
                "type": obj.type,
                "location": list(obj.location),
                "rotation": list(obj.rotation_euler),
                "scale": list(obj.scale),
            })
        if operation == "object.create":
            name = args.get("object_id")
            if not isinstance(name, str) or not name or len(name) > 128 or "\x00" in name:
                raise ValueError("object_id is invalid")
            if bpy.data.objects.get(name) is not None:
                return _error("INVALID_ARGUMENT", "object_id already exists")
            primitive = args.get("primitive")
            location = _vector(args.get("location", [0, 0, 0]), "location")
            operators = {"cube": bpy.ops.mesh.primitive_cube_add, "sphere": bpy.ops.mesh.primitive_uv_sphere_add, "cylinder": bpy.ops.mesh.primitive_cylinder_add}
            if primitive not in operators:
                return _error("INVALID_ARGUMENT", "primitive is not allowed")
            operators[primitive](location=location)
            obj = bpy.context.object
            obj.name = name
            _SCENE_VERSION += 1
            return _ok(object={"object_id": obj.name, "type": obj.type})
        if operation == "object.update_transform":
            obj = _object_or_error(args.get("object_id"))
            updates = {key: _vector(args[key], key) for key in ("location", "rotation", "scale") if key in args}
            for key, target in (("location", "location"), ("rotation", "rotation_euler"), ("scale", "scale")):
                if key in updates:
                    setattr(obj, target, updates[key])
            _SCENE_VERSION += 1
            return _ok(object={"object_id": obj.name})
        if operation == "object.delete":
            obj = _object_or_error(args.get("object_id"))
            bpy.data.objects.remove(obj, do_unlink=True)
            _SCENE_VERSION += 1
            return _ok(deleted=args["object_id"])
        if operation == "scene.save":
            _save(_safe_workspace_file("project/main.blend"))
            _SCENE_VERSION += 1
            return _ok(asset_id="project/main.blend")
        if operation == "scene.checkpoint":
            if _dependencies.external_dependencies(bpy):
                return _error("PROJECT_EXTERNAL_DEPENDENCIES", "Pack external resources before saving a single-file Project.")
            # Save once at the canonical path, then preserve those exact bytes.
            # Two Blender saves can differ and cannot prove that the Project
            # checkpoint and the restart working copy contain identical bytes.
            checkpoint = _checkpoint()
            _SCENE_VERSION += 1
            return _ok(asset_id=str(checkpoint.relative_to(_WORKSPACE)))
        if operation == "asset.import":
            asset_id = args.get("asset_id")
            path = _asset_file(asset_id)
            if args.get("format") not in {"glb", "gltf"} or path.suffix.lower().lstrip(".") != args["format"]:
                return _error("INVALID_ARGUMENT", "asset format does not match the asset extension")
            before = {obj.name for obj in bpy.context.scene.objects}
            bpy.ops.import_scene.gltf(filepath=str(path))
            imported = sorted(obj.name for obj in bpy.context.scene.objects if obj.name not in before)
            _SCENE_VERSION += 1
            return _ok(asset_id=asset_id, objects=imported)
        if operation == "asset.export":
            asset_id = args.get("asset_id")
            path = _asset_file(asset_id, prefix="exports/")
            if args.get("format") != "glb" or path.suffix.lower() != ".glb":
                return _error("INVALID_ARGUMENT", "only GLB export is supported")
            path.parent.mkdir(parents=True, exist_ok=True)
            bpy.ops.export_scene.gltf(filepath=str(path), export_format="GLB", use_selection=False)
            if not path.is_file() or path.stat().st_size == 0:
                raise OSError("Blender did not produce an export")
            return _ok(asset_id=asset_id, sha256=_sha256(path), size=path.stat().st_size)
        return _error("POLICY_DENIED", "operation is not in the adapter allowlist")
    except _drain.DrainError as exc:
        return _error(str(exc), 'Instance drain could not be confirmed.')
    except KeyError as exc:
        return _error("ASSET_NOT_FOUND", f"object not found: {exc.args[0]}")
    except (OSError, ValueError, TypeError) as exc:
        return _error("INVALID_ARGUMENT", str(exc))
    except RuntimeError as exc:
        return _error("UPSTREAM_UNAVAILABLE", str(exc))


class _RequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline(4 * 1024 * 1024 + 1)
        if len(line) > 4 * 1024 * 1024:
            self.wfile.write(json.dumps(_error("INVALID_ARGUMENT", "request is too large")).encode() + b"\n")
            return
        try:
            request = json.loads(line.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self.wfile.write(json.dumps(_error("INVALID_ARGUMENT", str(exc))).encode() + b"\n")
            return
        task = Task(request=request, done=threading.Event())
        _QUEUE.put(task)
        if not task.done.wait(timeout=30):
            response = _error("OPERATION_TIMEOUT", "Blender main thread did not process the request")
        else:
            response = task.response or _error("UPSTREAM_UNAVAILABLE", "adapter returned no response")
        self.wfile.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def _drain_queue() -> float:
    for _ in range(32):
        try:
            task = _QUEUE.get_nowait()
        except queue.Empty:
            break
        try:
            task.response = _execute(task.request)
        finally:
            task.done.set()
    return 0.05


def register() -> None:
    global _SERVER, _THREAD, _LOAD_HANDLER, _RESTORE_ID
    if _SERVER is not None:
        return
    _SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        _SOCKET_PATH.unlink()
    except FileNotFoundError:
        pass
    _SERVER = _UnixServer(str(_SOCKET_PATH), _RequestHandler)
    os.chmod(_SOCKET_PATH, 0o600)
    _THREAD = threading.Thread(target=_SERVER.serve_forever, name="blender-mcp-adapter", daemon=True)
    _THREAD.start()
    bpy.app.timers.register(_drain_queue, first_interval=0.05, persistent=True)
    def on_load(_):
        global _GENERATION, _SCENE_VERSION, _RESTORE_ID, _STARTUP_PROOF
        _GENERATION, _SCENE_VERSION = uuid.uuid4().hex, 0
        _RESTORE_ID = None
        _STARTUP_PROOF = None
    _LOAD_HANDLER = bpy.app.handlers.persistent(on_load)
    bpy.app.handlers.load_post.append(_LOAD_HANDLER)
    try:
        receipt = json.loads(_safe_workspace_file("project/restore.json").read_text(encoding="utf-8"))
        target = _safe_workspace_file("project/main.blend")
        if Path(bpy.data.filepath).resolve() == target and _sha256(target) == receipt["sha256"]:
            _RESTORE_ID = str(uuid.UUID(receipt["restore_id"]))
    except (OSError, ValueError, KeyError, TypeError):
        pass


def unregister() -> None:
    global _SERVER, _THREAD, _LOAD_HANDLER
    if _LOAD_HANDLER is not None and _LOAD_HANDLER in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_LOAD_HANDLER)
    _LOAD_HANDLER = None
    if _SERVER is not None:
        _SERVER.shutdown()
        _SERVER.server_close()
        _SERVER = None
    if _THREAD is not None:
        _THREAD.join(timeout=2)
        _THREAD = None
    try:
        _SOCKET_PATH.unlink()
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    register()
