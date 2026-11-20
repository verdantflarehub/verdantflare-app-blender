#!/usr/bin/env python3
"""Private execution interface. This worker deliberately has no MCP endpoint."""
from __future__ import annotations

import http.server
import importlib.util
import json
import os
from pathlib import Path
import sys
import re
import threading
import time
import uuid

spec = importlib.util.spec_from_file_location("blender_bridge", Path(__file__).parents[1] / "mcp-bridge/bridge.py")
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)
gui_spec = importlib.util.spec_from_file_location("blender_gui_control", Path(__file__).with_name("gui_control.py"))
gui = importlib.util.module_from_spec(gui_spec)
gui_spec.loader.exec_module(gui)

rtc_spec = importlib.util.spec_from_file_location("blender_rtc", Path(__file__).with_name("rtc.py"))
rtc = importlib.util.module_from_spec(rtc_spec)
rtc_spec.loader.exec_module(rtc)

check_spec = importlib.util.spec_from_file_location("blender_project_check", Path(__file__).with_name("project_check.py"))
project_check = importlib.util.module_from_spec(check_spec)
check_spec.loader.exec_module(project_check)
drain_spec = importlib.util.spec_from_file_location('blender_drain_control', Path(__file__).parents[1] / 'instance_drain.py')
drain = importlib.util.module_from_spec(drain_spec)
drain_spec.loader.exec_module(drain)
workspace_spec = importlib.util.spec_from_file_location("blender_workspace", Path(__file__).parents[1] / "file-agent/workspace.py")
workspace = importlib.util.module_from_spec(workspace_spec)
workspace_spec.loader.exec_module(workspace)
batch_spec = importlib.util.spec_from_file_location("blender_batch_run", Path(__file__).with_name("batch_run.py"))
batch_run = importlib.util.module_from_spec(batch_spec)
sys.modules[batch_spec.name] = batch_run
batch_spec.loader.exec_module(batch_run)
inspect_spec = importlib.util.spec_from_file_location("blender_inspect_blend", Path(__file__).with_name("inspect_blend.py"))
inspect_blend = importlib.util.module_from_spec(inspect_spec)
inspect_spec.loader.exec_module(inspect_blend)
residency_spec = importlib.util.spec_from_file_location("blender_residency", Path(__file__).with_name("residency.py"))
residency = importlib.util.module_from_spec(residency_spec)
sys.modules[residency_spec.name] = residency
residency_spec.loader.exec_module(residency)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never log client paths, headers or arguments.

    def setup(self):
        super().setup()
        self.connection.settimeout(35)

    def reply(self, status, value):
        data = json.dumps(value, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/internal/gpu-state":
            if len(self.headers.get_all("Authorization", [])) != 1 or not self.server.state.authorized(self.headers.get("Authorization")):
                return self.reply(401, {"code": "UNAUTHENTICATED"})
            return self.reply(200, self.server.residency.gpu_state())
        if self.path == "/internal/status":
            if len(self.headers.get_all("Authorization", [])) != 1 or not self.server.state.authorized(self.headers.get("Authorization")):
                return self.reply(401, {"code": "UNAUTHENTICATED"})
            # Observation must never expire, renew or acquire an editing lease.
            if not self.server.gui.lock.acquire(timeout=0.1):
                return self.reply(503, {"code": "WORKER_BUSY"})
            try:
                record = self.server.drain.read()
                if record and record['phase'] != 'aborted':
                    return self.reply(200, {'status':'draining','instance_id':self.server.state.instance_id,
                        'generation':record['generation'],'gui_held':self.server.gui.session is not None,
                        'gpu_state':self.server.residency.gpu_state()})
                if self.server.residency.quarantined:
                    return self.reply(503, {
                        "code": "UNRECLAIMED_GPU_LINEAGE",
                        "instance_id": self.server.state.instance_id,
                        "gpu_state": self.server.residency.gpu_state(),
                    })
                binding = {key: os.environ.get(env) for key, env in (
                    ("pod_uid", "BLENDER_POD_UID"), ("namespace", "BLENDER_POD_NAMESPACE"),
                    ("pod_name", "BLENDER_POD_NAME"), ("container", "BLENDER_CONTAINER_NAME"),
                    ("gpu_uuid", "BLENDER_GPU_UUID"))}
                if self.server.residency.is_standby():
                    standby = self.server.residency.standby_probe()
                    if standby is None:
                        return self.reply(503, {"code": "WORKER_NOT_READY"})
                    return self.reply(200, {
                        "status": "ready",
                        "instance_id": self.server.state.instance_id,
                        "generation": standby["generation"],
                        "gui_held": False,
                        "startup": standby["startup"],
                        "gpu_state": self.server.residency.gpu_state(),
                        "resource_binding": binding,
                    })
                result = self.server.state.adapter.call("scene.get", {}, request_id=str(uuid.uuid4()), scene_version=None, deadline_ms=2000)
                if result.get("ok") is not True or not result.get("generation"):
                    return self.reply(503, {"code": "WORKER_NOT_READY"})
                self.server.residency.last_known_generation = result["generation"]
                return self.reply(200, {"status": "ready", "instance_id": self.server.state.instance_id,
                    "generation": result["generation"], "gui_held": self.server.gui.session is not None,
                    "startup": result.get("startup"),
                    "gpu_state": self.server.residency.gpu_state(),
                    "resource_binding": binding})
            except (bridge.BridgeError, drain.DrainError, OSError, KeyError):
                return self.reply(503, {"code": "WORKER_NOT_READY"})
            finally:
                self.server.gui.lock.release()
        if self.path == "/healthz":
            return self.reply(200, {"status": "ok"})
        if self.path == "/readyz":
            try:
                record = self.server.drain.read()
                if record and record['phase'] != 'aborted':
                    return self.reply(200, {'status':'draining','instance_id':self.server.state.instance_id})
                if self.server.residency.quarantined:
                    return self.reply(503, {"status": "quarantined", "reason": self.server.residency.quarantine_reason})
                if self.server.residency.is_standby():
                    standby = self.server.residency.standby_probe()
                    if standby is None:
                        return self.reply(503, {"status": "not_ready"})
                    return self.reply(200, {
                        "status": "ready",
                        "instance_id": self.server.state.instance_id,
                        "generation": standby["generation"],
                        "residency_state": "zero_vram_standby",
                    })
                result = self.server.state.adapter.call("scene.get", {}, request_id=str(uuid.uuid4()), scene_version=None, deadline_ms=2000)
                self.server.residency.last_known_generation = result["generation"]
                return self.reply(200, {"status": "ready", "instance_id": self.server.state.instance_id, "generation": result["generation"]})
            except (bridge.BridgeError, drain.DrainError, OSError, KeyError):
                return self.reply(503, {"status": "not_ready"})
        self.reply(404, {"code": "NOT_FOUND"})

    def do_POST(self):
        if self.path not in {
            "/internal/rpc",
            "/internal/gui",
            "/internal/restore",
            "/internal/drain",
            "/internal/batch",
            "/internal/v1/gpu-residency/evict-vram",
            "/internal/v1/gpu-residency/revoke-lease",
            "/internal/v1/gpu-residency/pre-cuda-check",
            "/internal/launch-grant",
        }:
            return self.reply(404, {"code": "NOT_FOUND"})
        state = self.server.state
        auth_header = self.headers.get("Authorization")
        if len(self.headers.get_all("Authorization", [])) != 1 or not (
            state.authorized(auth_header)
            or (
                self.path == "/internal/v1/gpu-residency/pre-cuda-check"
                and self.server.residency.authorized_handshake(auth_header)
            )
            or (
                self.path in {
                    "/internal/v1/gpu-residency/evict-vram",
                    "/internal/v1/gpu-residency/revoke-lease",
                }
                and self.server.residency.authorized_residency_callback(auth_header)
            )
        ):
            try:
                cl = int(self.headers.get("Content-Length", "0"))
                if 0 < cl <= 4096:
                    self.rfile.read(cl)
            except (ValueError, OSError):
                pass
            return self.reply(401, {"code": "UNAUTHENTICATED"})
        try:
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                raise ValueError()
            length = int(self.headers.get("Content-Length", "-1"))
            if not 0 < length <= state.max_body_bytes:
                raise ValueError()
            request = json.loads(self.rfile.read(length))
            if self.path == "/internal/v1/gpu-residency/evict-vram":
                if not isinstance(request, dict):
                    raise ValueError()
                if "instance_id" in request and request["instance_id"] != state.instance_id:
                    return self.reply(403, {"code": "INSTANCE_MISMATCH", "reason_code": "INSTANCE_MISMATCH", "settled": False})
                expected_seq = request.get("expected_residency_seq")
                expected_epoch = request.get("expected_gpu_epoch")
                expected_boot_id = request.get("expected_boot_id")
                deadline_ms = request.get("deadline_ms", request.get("soft_evict_deadline_ms"))
                force_hard = request.get("force_hard", False)
                if (
                    (expected_seq is not None and type(expected_seq) is not int)
                    or (expected_epoch is not None and type(expected_epoch) is not int)
                    or (expected_boot_id is not None and not isinstance(expected_boot_id, str))
                    or (deadline_ms is not None and type(deadline_ms) is not int)
                    or type(force_hard) is not bool
                ):
                    raise ValueError()
                res_hib = self.server.residency.hibernate(
                    reason=str(request.get("reason") or "SCHEDULER_PREEMPT"),
                    expected_boot_id=expected_boot_id,
                    expected_residency_seq=expected_seq,
                    expected_gpu_epoch=expected_epoch,
                    force_gui_idle=bool(request.get("force_gui_idle", False)),
                    force_hard=force_hard,
                    colocated_heavy_queued=request.get("colocated_heavy_queued"),
                    deadline_ms=deadline_ms,
                )
                is_sched_cb = bool(
                    self.server.residency.authorized_residency_callback(auth_header)
                    and not state.authorized(auth_header)
                )
                if is_sched_cb:
                    self.server.residency.mark_evict_callback_handled()
                elif self.server.residency.residency_sync_enabled:
                    self.server.residency.residency_sync_step()
                return self.reply(200, res_hib)
            if self.path == "/internal/v1/gpu-residency/revoke-lease":
                if not isinstance(request, dict):
                    raise ValueError()
                if "instance_id" in request and request["instance_id"] != state.instance_id:
                    return self.reply(403, {"code": "INSTANCE_MISMATCH"})
                res_rev = self.server.residency.revoke_lease(request)
                is_sched_cb = bool(
                    self.server.residency.authorized_residency_callback(auth_header)
                    and not state.authorized(auth_header)
                )
                if is_sched_cb:
                    self.server.residency.mark_evict_callback_handled()
                elif self.server.residency.residency_sync_enabled:
                    self.server.residency.residency_sync_step()
                return self.reply(200, res_rev)
            if self.path == "/internal/v1/gpu-residency/pre-cuda-check":
                if not isinstance(request, dict):
                    raise ValueError()
                return self.reply(200, self.server.residency.pre_cuda_check(request))
            if self.path == "/internal/launch-grant":
                if not isinstance(request, dict):
                    raise ValueError()
                return self.reply(200, self.server.residency.install_launch_grant(request))
            if self.path == '/internal/drain':
                return self.manage_drain(request)
            if self.path == "/internal/batch":
                self.connection.settimeout(620)
                allowed_keys = {
                    "instance_id", "generation", "request_id", "script_asset_id", "script_sha256",
                    "script_size", "output_subdir", "clean_output_subdir", "load_blend_as_active",
                    "scene_version", "timeout_seconds",
                }
                required_keys = {"instance_id", "generation", "script_asset_id", "script_sha256", "script_size", "output_subdir"}
                if (
                    not isinstance(request, dict)
                    or set(request) - allowed_keys
                    or any(k not in request for k in required_keys)
                    or request["instance_id"] != state.instance_id
                    or not isinstance(request["generation"], str)
                    or not request["generation"]
                ):
                    raise ValueError()
                with self.server.residency.activity_scope(batch=True) as batch_guard:
                    with self.server.gui.lock:
                        self.server.drain.guard()
                        self.server.gui.guard_write()
                        current = state.adapter.call(
                            "scene.get", {}, request_id=str(uuid.uuid4()),
                            generation=request["generation"], scene_version=None, deadline_ms=2000,
                        )
                    batch_result = batch_run.run(request, authorize_launch=batch_guard)
                    batch_guard()
                    batch_result["generation"] = current.get("generation", request["generation"])
                    batch_result["scene_version"] = current.get("scene_version", 0)
                    if batch_result.get("load_blend_asset_id"):
                        with self.server.gui.lock:
                            batch_guard()
                            self.server.drain.guard()
                            self.server.gui.guard_write()
                            target_version = request.get("scene_version", current.get("scene_version", 0))
                            opened = state.adapter.call(
                                "scene.open_file",
                                {"asset_id": batch_result["load_blend_asset_id"], "save_as_project_main": True},
                                request_id=str(request.get("request_id") or uuid.uuid4()),
                                generation=request["generation"],
                                scene_version=target_version,
                                deadline_ms=25000,
                            )
                            batch_result["generation"] = opened.get("generation", request["generation"])
                            batch_result["scene_version"] = opened.get("scene_version", target_version)
                            batch_result["loaded_scene"] = opened.get("scene")
                batch_result["ok"] = True
                return self.reply(200, batch_result)
            if self.path == "/internal/restore":
                if (not isinstance(request, dict) or set(request) != {"instance_id", "generation", "restore_id", "asset_id", "sha256", "size"}
                        or request["instance_id"] != state.instance_id
                        or not isinstance(request["generation"], str) or not request["generation"]
                        or not isinstance(request["restore_id"], str) or str(uuid.UUID(request["restore_id"])) != request["restore_id"]
                        or not isinstance(request["asset_id"], str) or not re.fullmatch(r"inbox/[A-Za-z0-9_-]{16}/restore\.blend", request["asset_id"])
                        or not isinstance(request["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", request["sha256"])
                        or type(request["size"]) is not int or not 0 < request["size"] <= 512 * 1024 * 1024):
                    raise ValueError()
                with self.server.gui.lock, self.server.residency.activity_scope():
                    self.server.drain.guard()
                    self.server.gui.guard_write()
                    deadline = time.monotonic() + 26
                    # Fence the input generation before starting a bounded
                    # child process; preflight never touches the live scene.
                    state.adapter.call("scene.get", {}, request_id=str(uuid.uuid4()),
                        generation=request["generation"], scene_version=None, deadline_ms=2000)
                    project_check.check(request)
                    remaining = int((deadline - time.monotonic()) * 1000)
                    if remaining <= 0:
                        raise project_check.ProjectCheckError("PROJECT_PREFLIGHT_UNAVAILABLE")
                    result = state.adapter.call("scene.restore", {k: request[k] for k in ("restore_id", "asset_id", "sha256", "size")},
                        request_id=request["restore_id"], generation=request["generation"], scene_version=None, deadline_ms=min(25000, remaining))
                return self.reply(200, result)
            if self.path == "/internal/gui":
                fields = {"instance_id", "generation", "action", "session"}
                optional_fields = set()
                if isinstance(request, dict) and request.get("action") == "open":
                    fields.add("rtc_config")
                elif isinstance(request, dict) and request.get("action") == "heartbeat":
                    optional_fields.add("user_input")
                if (
                    not isinstance(request, dict)
                    or not fields.issubset(set(request))
                    or set(request) - (fields | optional_fields)
                    or request["instance_id"] != state.instance_id
                    or not isinstance(request["session"], str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", request["session"])
                    or ("user_input" in request and type(request["user_input"]) is not bool)
                ):
                    raise ValueError()
                with self.server.gui.lock:
                    if request['action'] != 'close':
                        self.server.drain.guard()
                        self.server.residency.ensure_awake()
                        state.adapter.call("scene.get", {}, request_id=str(uuid.uuid4()), generation=request["generation"], scene_version=None, deadline_ms=2000)
                    if request['action'] == 'open':
                        self.server.gui.guard_write()
                        expires = rtc.install(request['rtc_config'])
                        self.server.gui.deadline = self.server.gui.clock() + max(0, expires - __import__('time').time() - 30)
                    self.server.gui.action(request["action"], request["session"])
                    if request.get("user_input") is True:
                        self.server.gui.record_input()
                    self.server.residency._sync_state()
                return self.reply(200, {"ok": True})
            if not isinstance(request, dict) or set(request) != {"instance_id", "generation", "name", "arguments", "request_id"}:
                raise ValueError()
            if request["instance_id"] != state.instance_id:
                return self.reply(403, {"code": "INSTANCE_MISMATCH"})
            if not isinstance(request["generation"], str) or not request["generation"]:
                raise ValueError()
            tool = bridge.TOOLS.get(request["name"])
            args = request["arguments"]
            if tool is None or tool.name.startswith("job.") or not isinstance(args, dict):
                raise ValueError()
            if set(args) - (set(tool.properties) | {"scene_version"}) or any(k not in args for k in tool.required):
                raise ValueError()
            if tool.mutating and (type(args.get("scene_version")) is not int or args["scene_version"] < 0):
                raise ValueError()
            with self.server.gui.lock, self.server.residency.activity_scope():
                self.server.drain.guard()
                if tool.mutating or tool.name in {"asset.export", "workspace.clean"}:
                    self.server.gui.guard_write()
                if tool.name == "workspace.clean":
                    state.adapter.call("scene.get", {}, request_id=str(request["request_id"]),
                                       scene_version=None, generation=request["generation"], deadline_ms=2000)
                    try:
                        cleaned = workspace.clean_workspace(
                            workspace.workspace_root(os.environ.get("WORKSPACE_ROOT", "/workspace")),
                            remove_stray_home_c_drive=args.get("remove_stray_home_c_drive", False),
                            stray_home_names=args.get("stray_home_names"),
                            export_subdirs=args.get("export_subdirs"),
                            dry_run=args.get("dry_run", False),
                        )
                    except workspace.WorkspaceError as exc:
                        code = "WORKSPACE_CLEAN_PATH_INVALID" if str(exc) == "WORKSPACE_CLEAN_PATH_INVALID" else "INVALID_ARGUMENT"
                        return self.reply(400, {"code": code, "message": str(exc)})
                    return self.reply(200, {"ok": True, "generation": request["generation"], **cleaned})
                if tool.name == "scene.inspect_file" and args.get("asset_id"):
                    current = state.adapter.call("scene.get", {}, request_id=str(request["request_id"]),
                                                 scene_version=None, generation=request["generation"], deadline_ms=2000)
                    inspected = inspect_blend.inspect_file(args["asset_id"])
                    return self.reply(200, {"ok": True, "generation": current.get("generation", request["generation"]),
                                            "scene_version": current.get("scene_version", 0), **inspected})
                result = state.adapter.call(tool.operation, {k: v for k, v in args.items() if k != "scene_version"},
                                            request_id=str(request["request_id"]), scene_version=args.get("scene_version"),
                                            generation=request["generation"], deadline_ms=25000)
            # Startup provenance is for the authenticated Runtime observer only;
            # public scene operations must not expose private workload bindings.
            result.pop("startup", None)
            self.reply(200, result)
        except (ValueError, KeyError, TypeError):
            self.reply(400, {"code": "INVALID_ARGUMENT"})
        except residency.ResidencyError as exc:
            body = {
                "code": exc.code,
                "reason_code": exc.code,
                "ok": False,
                "settled": False,
                "released": False,
                "reclaimed": False,
                "instance_id": state.instance_id,
                "boot_id": self.server.residency.boot_id,
                "conflict": exc.status == 409,
                "unreclaimed": exc.status == 503,
            }
            try:
                gs = self.server.residency.gpu_state()
                body["gpu_epoch"] = gs["gpu_epoch"]
                body["residency_seq"] = gs["residency_seq"]
                body["residency_state"] = gs["residency_state"]
            except Exception:
                pass
            if exc.data is not None:
                body["data"] = exc.data
            self.reply(exc.status, body)
        except bridge.BridgeError as exc:
            self.reply(exc.status, {"code": exc.code, "message": exc.message})
        except drain.DrainError as exc:
            self.reply(400 if str(exc)=='INVALID_ARGUMENT' else 409, {'code':str(exc)})
        except project_check.ProjectCheckError as exc:
            self.reply(503 if str(exc) == "PROJECT_PREFLIGHT_UNAVAILABLE" else 409, {"code": str(exc)})
        except batch_run.BatchRunError as exc:
            status = 504 if exc.code in {"OPERATION_TIMEOUT", "BATCH_SCRIPT_TIMEOUT"} else 400 if exc.code in {"INVALID_ARGUMENT", "ASSET_NOT_FOUND"} else 409
            self.reply(status, {"code": exc.code, "data": exc.data})
        except inspect_blend.InspectBlendError as exc:
            status = 404 if exc.code == "ASSET_NOT_FOUND" else 400 if exc.code == "INVALID_ARGUMENT" else 503 if exc.code == "PROJECT_PREFLIGHT_UNAVAILABLE" else 409
            self.reply(status, {"code": exc.code})
        except (gui.LeaseError, OSError, gui.subprocess.TimeoutExpired) as exc:
            self.reply(409, {"code": str(exc) if isinstance(exc, gui.LeaseError) else "GUI_PROCESS_UNAVAILABLE"})

    def manage_drain(self, request):
        journal = self.server.drain
        journal.match(request, None)
        with self.server.gui.lock:
            if self.server.gui.session is not None:
                raise drain.DrainError('GUI_EDIT_LEASE_HELD')
            if request['action'] == 'prepare':
                # Confirm the input process exited even after a controller restart.
                self.server.gui.run('stop')
                self.server.residency.ensure_awake()
                record = journal.accept(request)
                operation = 'instance.drain' if record['phase']=='accepted' else None
            elif request['action'] == 'abort':
                record = journal.abort(request)
                operation = 'instance.resume' if record['phase']!='aborted' else None
            elif request['action'] == 'seal':
                journal.seal(request)
                operation = None
            else:
                operation = None
            if operation:
                try:
                    self.server.state.adapter.call(operation, request, request_id=request['operation_id'],
                        generation=request['generation'], scene_version=None, deadline_ms=2000)
                except bridge.BridgeError as exc:
                    # Self-STOP deliberately leaves the socket unanswered. Query
                    # durable evidence and /proc; a timeout is never success alone.
                    if exc.code not in {'OPERATION_TIMEOUT','UPSTREAM_UNAVAILABLE'}:
                        raise
            return self.reply(200, journal.status(request))


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, state, *, residency_kwargs=None):
        self.state = state
        gui_ttl = int(os.environ.get("BLENDER_GUI_HEARTBEAT_TIMEOUT_SECONDS", "60"))
        self.gui = gui.GUILease(ttl_seconds=gui_ttl)
        self.drain = drain.Journal()
        super().__init__(address, Handler)
        kwargs = dict(residency_kwargs or {})
        kwargs.setdefault(
            "handshake_url",
            f"http://127.0.0.1:{self.server_port}/internal/v1/gpu-residency/pre-cuda-check",
        )
        self.residency = residency.BlenderResidencyController(
            state, self.gui, self.drain, **kwargs
        )
        self.residency.batch_module = batch_run


if __name__ == "__main__":
    state = bridge.build_state_from_env()
    server = Server((os.environ.get("BLENDER_MCP_LISTEN_HOST", "0.0.0.0"), int(os.environ.get("BLENDER_MCP_PORT", "48084"))), state)
    gui.supervise("stop")  # Control-process restart must not orphan a live input stream.
    threading.Thread(target=server.gui.watchdog, daemon=True).start()
    threading.Thread(target=server.residency.watchdog, daemon=True).start()
    if os.environ.get("BLENDER_SUBREAPER_MANAGED") == "1":
        threading.Thread(target=server.residency.start_initial_blender, daemon=True).start()
    server.serve_forever()
