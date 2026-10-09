"""Blender-side adapter for the Beagle Blender MCP bridge.

The socket worker never touches ``bpy``.  Requests are queued and drained by a
timer on Blender's main thread, which keeps Blender's data API out of a worker
thread and gives the bridge a small, explicit operation allowlist.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import queue
import socketserver
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import bpy  # type: ignore


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
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    if not path.is_file() or path.stat().st_size == 0:
        raise OSError("Blender did not produce a valid project file")


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
    global _SCENE_VERSION
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
        if operation == "scene.get":
            return _ok(scene={"name": bpy.context.scene.name, "filepath": str(Path(bpy.data.filepath).name)})
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
            checkpoint = _WORKSPACE / "checkpoints" / f"checkpoint-{_SCENE_VERSION}-{uuid.uuid4().hex}.blend"
            _save(checkpoint)
            _save(_safe_workspace_file("project/main.blend"))
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
    global _SERVER, _THREAD, _LOAD_HANDLER
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
        global _GENERATION, _SCENE_VERSION
        _GENERATION, _SCENE_VERSION = uuid.uuid4().hex, 0
    _LOAD_HANDLER = bpy.app.handlers.persistent(on_load)
    bpy.app.handlers.load_post.append(_LOAD_HANDLER)


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
