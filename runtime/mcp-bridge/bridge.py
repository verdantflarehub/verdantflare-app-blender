#!/usr/bin/env python3
"""Small, dependency-free MCP bridge for the Blender MCP P0.

The bridge deliberately owns the public protocol boundary.  It never evaluates
Python, opens a shell, or accepts a Blender/container path.  Blender is reached
through the line-delimited JSON protocol on the instance-local Unix socket.
"""

from __future__ import annotations

import argparse
import hmac
import http.server
import json
import os
import secrets
import socket
import stat
import sys
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable


PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "beagle-blender-mcp"
SERVER_VERSION = "0.1.0"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class BridgeError(Exception):
    def __init__(self, code: str, message: str, *, data: Any = None, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data
        self.status = status


class JobManager:
    """Single-worker queue for bounded L2 adapter operations."""

    def __init__(self, timeout_ms: int):
        self.timeout_ms = timeout_ms
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="blender-mcp-job")
        self._jobs: dict[str, dict[str, Any]] = {}
        import threading
        self._lock = threading.Lock()

    def submit(self, operation: str, fn: Callable[[], dict[str, Any]]) -> str:
        job_id = f"job-{uuid.uuid4().hex}"
        with self._lock:
            self._jobs[job_id] = {"job_id": job_id, "operation": operation, "state": "queued", "created_at": time.time()}
        future = self._executor.submit(self._run, job_id, fn)
        with self._lock:
            self._jobs[job_id]["future"] = future
        return job_id

    def _run(self, job_id: str, fn: Callable[[], dict[str, Any]]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job["state"] = "running"
        try:
            result = fn()
            with self._lock:
                job = self._jobs.get(job_id)
                if job and job["state"] != "cancelled":
                    job["state"] = "succeeded"
                    job["result"] = result
        except BridgeError as error:
            with self._lock:
                job = self._jobs.get(job_id)
                if job and job["state"] != "cancelled":
                    job["state"] = "timed_out" if error.code == "OPERATION_TIMEOUT" else "failed"
                    job["error"] = {"code": error.code, "message": error.message, "data": error.data}
        except Exception:
            with self._lock:
                job = self._jobs.get(job_id)
                if job and job["state"] != "cancelled":
                    job["state"] = "failed"
                    job["error"] = {"code": "UPSTREAM_UNAVAILABLE", "message": "job failed"}
        finally:
            with self._lock:
                if job_id in self._jobs:
                    self._jobs[job_id]["updated_at"] = time.time()

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise BridgeError("ASSET_NOT_FOUND", "job was not found")
            return {key: value for key, value in job.items() if key != "future"}

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise BridgeError("ASSET_NOT_FOUND", "job was not found")
            future: Future[Any] | None = job.get("future")
            if job["state"] == "queued" and future and future.cancel():
                job["state"] = "cancelled"
            elif job["state"] == "running":
                job["state"] = "cancelled"
            return {key: value for key, value in job.items() if key != "future"}

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


class AdapterClient:
    """JSON-lines client for the instance-local Blender adapter."""

    def __init__(self, socket_path: str, timeout: float = 10.0):
        self.socket_path = socket_path
        self.timeout = timeout

    def ready(self) -> bool:
        try:
            return stat.S_ISSOCK(os.stat(self.socket_path).st_mode)
        except OSError:
            return False

    def call(self, operation: str, args: dict[str, Any], *, request_id: str,
             scene_version: int | None, deadline_ms: int, generation: str | None = None) -> dict[str, Any]:
        request = {
            "protocol_version": 1,
            "request_id": request_id,
            "deadline": int(time.time() * 1000) + deadline_ms,
            "operation": operation,
            "args": args,
        }
        if scene_version is not None:
            request["scene_version"] = scene_version
        if generation is not None:
            request["generation"] = generation

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(min(self.timeout, max(0.1, deadline_ms / 1000)))
                conn.connect(self.socket_path)
                conn.sendall(json.dumps(request, separators=(",", ":")).encode() + b"\n")
                chunks: list[bytes] = []
                total = 0
                while b"\n" not in b"".join(chunks):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > MAX_RESPONSE_BYTES:
                        raise BridgeError("UPSTREAM_UNAVAILABLE", "adapter response is too large", status=502)
                raw = b"".join(chunks).split(b"\n", 1)[0]
        except BridgeError:
            raise
        except socket.timeout as exc:
            raise BridgeError("OPERATION_TIMEOUT", "Blender adapter operation timed out", status=504) from exc
        except (OSError, TimeoutError) as exc:
            raise BridgeError("UPSTREAM_UNAVAILABLE", "Blender adapter is unavailable", data=str(exc), status=503) from exc

        try:
            response = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeError("UPSTREAM_UNAVAILABLE", "adapter returned invalid JSON", status=502) from exc
        if not isinstance(response, dict):
            raise BridgeError("UPSTREAM_UNAVAILABLE", "adapter returned a non-object response", status=502)
        if response.get("ok") is False:
            error = response.get("error")
            if isinstance(error, dict):
                raise BridgeError(
                    str(error.get("code", "UPSTREAM_UNAVAILABLE")),
                    str(error.get("message", "adapter rejected the operation")),
                    data=error.get("data"),
                    status=409 if error.get("code") == "SCENE_VERSION_CONFLICT" else 400,
                )
            raise BridgeError("UPSTREAM_UNAVAILABLE", "adapter rejected the operation", status=502)
        return response


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    operation: str
    mutating: bool
    properties: dict[str, Any]
    required: tuple[str, ...] = ()

    def descriptor(self) -> dict[str, Any]:
        properties = dict(self.properties)
        properties["instance_id"] = _json_property("string", "The instance bound to this bridge.")
        required = list(self.required)
        if self.mutating:
            properties["scene_version"] = _json_property("integer", "Current optimistic-lock scene version.")
            required.append("scene_version")
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        }


def _json_property(type_name: str, description: str) -> dict[str, str]:
    return {"type": type_name, "description": description}


TOOLS: dict[str, Tool] = {
    "scene.get": Tool("scene.get", "Read the current scene summary.", "scene.get", False, {}),
    "scene.list_objects": Tool("scene.list_objects", "List objects in the current scene.", "scene.list_objects", False, {}),
    "object.get": Tool(
        "object.get", "Read one object by stable name.", "object.get", False,
        {"object_id": _json_property("string", "Stable Blender object name.")}, ("object_id",),
    ),
    "object.create": Tool(
        "object.create", "Create a primitive object.", "object.create", True,
        {
            "object_id": _json_property("string", "Stable object name."),
            "primitive": {"type": "string", "enum": ["cube", "sphere", "cylinder"], "description": "Allowed primitive."},
            "location": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
        }, ("object_id", "primitive"),
    ),
    "object.update_transform": Tool(
        "object.update_transform", "Update an object's transform.", "object.update_transform", True,
        {
            "object_id": _json_property("string", "Stable object name."),
            "location": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
            "rotation": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
            "scale": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
        }, ("object_id",),
    ),
    "object.delete": Tool(
        "object.delete", "Delete an object by stable name.", "object.delete", True,
        {"object_id": _json_property("string", "Stable object name.")}, ("object_id",),
    ),
    "scene.save": Tool("scene.save", "Save the current project.", "scene.save", True, {}),
    "scene.checkpoint": Tool("scene.checkpoint", "Create a recoverable checkpoint.", "scene.checkpoint", True, {}),
    "asset.import": Tool(
        "asset.import", "Import an approved GLB or glTF asset into the scene.", "asset.import", True,
        {
            "asset_id": _json_property("string", "Approved workspace asset ID."),
            "format": {"type": "string", "enum": ["glb", "gltf"]},
        }, ("asset_id", "format"),
    ),
    "asset.export": Tool(
        "asset.export", "Export the current scene to an approved GLB asset.", "asset.export", False,
        {
            "asset_id": _json_property("string", "Destination asset ID under exports/."),
            "format": {"type": "string", "enum": ["glb"]},
        }, ("asset_id", "format"),
    ),
    "job.get": Tool(
        "job.get", "Read the state of an instance job.", "job.get", False,
        {"job_id": _json_property("string", "Instance job ID.")}, ("job_id",),
    ),
    "job.cancel": Tool(
        "job.cancel", "Cancel a queued instance job.", "job.cancel", False,
        {"job_id": _json_property("string", "Instance job ID.")}, ("job_id",),
    ),
}


def _read_token() -> str:
    token_file = os.environ.get("BLENDER_MCP_TOKEN_FILE")
    token = os.environ.get("BLENDER_MCP_TOKEN", "")
    if token_file:
        try:
            with open(token_file, "r", encoding="utf-8") as stream:
                token = stream.read().strip()
        except OSError as exc:
            raise RuntimeError(f"cannot read BLENDER_MCP_TOKEN_FILE: {exc}") from exc
    if not token:
        raise RuntimeError("BLENDER_MCP_TOKEN or BLENDER_MCP_TOKEN_FILE is required")
    return token


class BridgeState:
    def __init__(self, instance_id: str, token: str, adapter: AdapterClient,
                 *, request_timeout_ms: int = 10_000, job_timeout_ms: int = 600_000,
                 max_body_bytes: int = 1_048_576):
        if not instance_id:
            raise ValueError("instance_id is required")
        if not token:
            raise ValueError("token is required")
        self.instance_id = instance_id
        self._token = token
        self.adapter = adapter
        self.request_timeout_ms = request_timeout_ms
        self.job_timeout_ms = job_timeout_ms
        self.max_body_bytes = max_body_bytes
        self.jobs = JobManager(job_timeout_ms)

    def authorized(self, header: str | None) -> bool:
        if not header or not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[7:].strip(), self._token)

    def ready(self) -> bool:
        return self.adapter.ready()


L2_TOOLS = {"asset.import", "asset.export"}


def _rpc_error(request_id: Any, error: BridgeError) -> dict[str, Any]:
    result: dict[str, Any] = {"code": error.code, "message": error.message}
    if error.data is not None:
        result["data"] = error.data
    return {"jsonrpc": "2.0", "id": request_id, "error": result}


class MCPHandler(http.server.BaseHTTPRequestHandler):
    server: "BridgeHTTPServer"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Do not use the default access log: it may include query strings or IDs
        # supplied by a client.  Structured fields stay deliberately minimal.
        sys.stderr.write(f"[blender-mcp] {self.command} {self.path} {fmt % args}\n")

    @property
    def state(self) -> BridgeState:
        return self.server.state

    def _send_json(self, payload: Any, status: int = 200, *, session_id: str | None = None) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        if session_id:
            self.send_header("Mcp-Session-Id", session_id)
        self.end_headers()
        self.wfile.write(encoded)

    def _send_empty(self, status: int = 202) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/healthz":
            self._send_json({"status": "ok", "instance_id": self.state.instance_id})
            return
        if self.path.rstrip("/") == "/readyz":
            ready = self.state.ready()
            self._send_json(
                {"status": "ready" if ready else "not_ready", "checks": {"adapter": ready}},
                200 if ready else 503,
            )
            return
        self._send_json({"error": "not_found"}, 404)

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/mcp":
            self._send_json({"error": "not_found"}, 404)
            return
        if not self.state.authorized(self.headers.get("Authorization")):
            self._send_json({"error": "AUTH_REQUIRED"}, 401)
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 0 or length > self.state.max_body_bytes:
            self._send_json({"error": "request body is invalid or too large"}, 413)
            return
        try:
            request = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"error": "invalid JSON"}, 400)
            return

        requests = request if isinstance(request, list) else [request]
        responses: list[dict[str, Any]] = []
        session_id: str | None = None
        for item in requests:
            response, created_session = self._handle_rpc(item)
            session_id = session_id or created_session
            if response is not None:
                responses.append(response)
        if not responses:
            self._send_empty()
        else:
            self._send_json(responses if isinstance(request, list) else responses[0], session_id=session_id)

    def _handle_rpc(self, request: Any) -> tuple[dict[str, Any] | None, str | None]:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return _rpc_error(None, BridgeError("INVALID_ARGUMENT", "valid JSON-RPC 2.0 request required")), None
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        if not isinstance(method, str) or not isinstance(params, dict):
            return _rpc_error(request_id, BridgeError("INVALID_ARGUMENT", "method and params are invalid")), None
        if method == "notifications/initialized":
            return None, None
        try:
            if method == "initialize":
                session = secrets.token_urlsafe(24)
                result = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                }
                return {"jsonrpc": "2.0", "id": request_id, "result": result}, session
            if method == "ping":
                result: Any = {}
            elif method == "tools/list":
                result = {"tools": [tool.descriptor() for tool in TOOLS.values()]}
            elif method == "tools/call":
                result = self._call_tool(request_id, params)
            else:
                raise BridgeError("METHOD_NOT_FOUND", f"unsupported method: {method}")
            if "id" not in request:
                return None, None
            return {"jsonrpc": "2.0", "id": request_id, "result": result}, None
        except BridgeError as error:
            if "id" not in request:
                return None, None
            return _rpc_error(request_id, error), None
        except Exception as error:  # fail closed without exposing internals
            if "id" not in request:
                return None, None
            return _rpc_error(request_id, BridgeError("UPSTREAM_UNAVAILABLE", "internal bridge error", status=502)), None

    def _call_tool(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or name not in TOOLS:
            raise BridgeError("POLICY_DENIED", "tool is not in the allowlist")
        if not isinstance(arguments, dict):
            raise BridgeError("INVALID_ARGUMENT", "tool arguments must be an object")
        tool = TOOLS[name]
        allowed = set(tool.properties) | {"instance_id", "scene_version"}
        unknown = sorted(set(arguments) - allowed)
        if unknown:
            raise BridgeError("INVALID_ARGUMENT", "unknown tool argument", data={"unknown": unknown})
        missing = [key for key in tool.required if key not in arguments]
        if missing:
            raise BridgeError("INVALID_ARGUMENT", "required tool argument is missing", data={"missing": missing})
        supplied_instance = arguments.get("instance_id", self.state.instance_id)
        if supplied_instance != self.state.instance_id:
            raise BridgeError("AUTH_FORBIDDEN", "instance_id does not match this bridge", status=403)
        if name == "job.get":
            result = self.state.jobs.get(arguments["job_id"])
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": False}
        if name == "job.cancel":
            result = self.state.jobs.cancel(arguments["job_id"])
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": False}
        scene_version = arguments.get("scene_version")
        if tool.mutating and (not isinstance(scene_version, int) or scene_version < 0):
            raise BridgeError("INVALID_ARGUMENT", "mutating tools require a non-negative scene_version")
        adapter_args = {key: value for key, value in arguments.items() if key not in {"instance_id", "scene_version"}}
        adapter_request_id = str(request_id or uuid.uuid4())
        if name in L2_TOOLS:
            job_id = self.state.jobs.submit(
                tool.operation,
                lambda: self.state.adapter.call(
                    tool.operation,
                    adapter_args,
                    request_id=adapter_request_id,
                    scene_version=scene_version,
                    deadline_ms=self.state.job_timeout_ms,
                ),
            )
            return {"content": [{"type": "text", "text": json.dumps({
                "job_id": job_id, "state": "queued", "poll_after_ms": 500,
            })}], "isError": False}
        response = self.state.adapter.call(
            tool.operation,
            adapter_args,
            request_id=adapter_request_id,
            scene_version=scene_version,
            deadline_ms=self.state.request_timeout_ms,
        )
        result = {key: value for key, value in response.items() if key not in {"ok", "error"}}
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": False}


class BridgeHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], state: BridgeState):
        self.state = state
        super().__init__(address, MCPHandler)

    def server_close(self) -> None:
        self.state.jobs.shutdown()
        super().server_close()


def build_state_from_env() -> BridgeState:
    instance_id = os.environ.get("INSTANCE_ID", "")
    token = _read_token()
    socket_path = os.environ.get("BLENDER_MCP_ADAPTER_SOCKET", "/run/user/1000/blender-mcp-adapter.sock")
    timeout_ms = int(os.environ.get("BLENDER_MCP_REQUEST_TIMEOUT_MS", "10000"))
    job_timeout_ms = int(os.environ.get("BLENDER_MCP_JOB_TIMEOUT_MS", "600000"))
    max_body = int(os.environ.get("BLENDER_MCP_MAX_BODY_BYTES", str(1_048_576)))
    return BridgeState(
        instance_id,
        token,
        AdapterClient(socket_path, max(timeout_ms, job_timeout_ms) / 1000),
        request_timeout_ms=timeout_ms,
        job_timeout_ms=job_timeout_ms,
        max_body_bytes=max_body,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("BLENDER_MCP_LISTEN_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("BLENDER_MCP_PORT", "48084")))
    args = parser.parse_args(argv)
    try:
        state = build_state_from_env()
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"[blender-mcp] configuration error: {exc}", file=sys.stderr)
        return 78
    server = BridgeHTTPServer((args.host, args.port), state)
    print(f"[blender-mcp] listening on {args.host}:{args.port} instance={state.instance_id}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
