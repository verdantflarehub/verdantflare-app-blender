"""Deterministic Blender startup with pre-CUDA 6-tuple handshake and epoch shm scoping."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request

HEX64_RE = re.compile(r"([0-9a-f]{64})", re.IGNORECASE)
GPU_UUID_RE = re.compile(r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")


def _resolve_container_id(env: dict[str, str], cgroup_path: Path = Path("/proc/self/cgroup")) -> str:
    for key in ("VF_CONTAINER_ID", "CONTAINER_ID"):
        raw = (env.get(key) or "").strip()
        if raw:
            for prefix in ("containerd://", "docker://", "cri-o://"):
                if raw.startswith(prefix):
                    raw = raw[len(prefix):]
            if raw:
                return raw
    try:
        text = Path(cgroup_path).read_text(encoding="utf-8", errors="replace")
        matches = HEX64_RE.findall(text)
        if matches:
            return matches[-1].lower()
    except OSError:
        pass
    pod_uid = (env.get("BLENDER_POD_UID") or env.get("POD_UID") or "").strip()
    if pod_uid:
        return f"pod-{pod_uid}-blender"
    return f"local-pid-{os.getpid()}"


def _parse_expiry_epoch(value) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    raise ValueError("invalid expires_at")


def setup_epoch_shm_hooks(env: dict[str, str] | None = None) -> None:
    environ = os.environ if env is None else env
    shm_dir = (environ.get("BLENDER_SHM_EPOCH_DIR") or "").strip()
    if shm_dir:
        Path(shm_dir).mkdir(parents=True, exist_ok=True)
        tempfile.tempdir = shm_dir
    manifest_raw = (environ.get("BLENDER_SHM_MANIFEST_FILE") or "").strip()
    if not manifest_raw:
        return
    try:
        from multiprocessing import shared_memory as _shm
    except ImportError:
        return
    orig_cls = _shm.SharedMemory
    if getattr(orig_cls, "_vf_epoch_hooked", False):
        return

    def _record_name(name: str) -> None:
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            return
        mf = Path(manifest_raw)
        try:
            mf.parent.mkdir(parents=True, exist_ok=True)
            items: list[str] = []
            if mf.is_file():
                loaded = json.loads(mf.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    items = [x for x in loaded if isinstance(x, str)]
            if name not in items:
                items.append(name)
                tmp = mf.with_name(f"{mf.name}.{os.getpid()}.tmp")
                tmp.write_text(json.dumps(items), encoding="utf-8")
                os.replace(tmp, mf)
        except (OSError, ValueError):
            pass

    class EpochScopedSharedMemory(orig_cls):  # type: ignore[misc,valid-type]
        _vf_epoch_hooked = True

        def __init__(self, name=None, create=False, size=0, *args, **kwargs):
            super().__init__(name=name, create=create, size=size, *args, **kwargs)
            if create:
                _record_name(self.name)

    _shm.SharedMemory = EpochScopedSharedMemory  # type: ignore[misc]


def verify_pre_cuda_gate(
    env: dict[str, str] | None = None,
    *,
    cgroup_path: Path = Path("/proc/self/cgroup"),
    now_epoch: float | None = None,
) -> dict | None:
    environ = dict(os.environ if env is None else env)
    policy = (environ.get("BLENDER_GPU_GRANT_POLICY") or "").strip().lower()
    if not policy:
        if (environ.get("STATION_RUNTIME_GPU_LEASE_REQUIRED") or "").strip().lower() == "true":
            policy = "required"
        else:
            policy = "compat_unmanaged"
    if policy not in {"required", "compat_unmanaged", "cpu_exempt"}:
        raise RuntimeError(f"unsupported BLENDER_GPU_GRANT_POLICY: {policy}")

    grant_raw = (environ.get("VF_GPU_LAUNCH_GRANT_JSON") or "").strip()
    grant: dict | None = None
    container_id = _resolve_container_id(environ, cgroup_path=cgroup_path)
    expected_pod_uid = (environ.get("BLENDER_POD_UID") or environ.get("POD_UID") or "").strip()
    expected_boot_id = (environ.get("VF_BOOT_ID") or "").strip()
    expected_gpu_uuid = (environ.get("BLENDER_GPU_UUID") or "").strip()

    if policy == "required" or grant_raw:
        if not grant_raw:
            raise RuntimeError("GPU_LEASE_REQUIRED: missing VF_GPU_LAUNCH_GRANT_JSON")
        parsed = json.loads(grant_raw)
        if not isinstance(parsed, dict):
            raise RuntimeError("GPU_LEASE_INVALID: malformed JSON object")
        fencing_token = parsed.get("fencing_token")
        if fencing_token is None and type(parsed.get("lease_token")) is int:
            fencing_token = parsed["lease_token"]
        for key in ("pod_uid", "container_id", "boot_id", "attempt_id", "lease_id"):
            val = parsed.get(key)
            if not isinstance(val, str) or not val.strip():
                raise RuntimeError(f"GPU_LEASE_INVALID: missing {key}")
        if type(fencing_token) is not int or fencing_token <= 0:
            raise RuntimeError("GPU_LEASE_INVALID: invalid fencing_token")
        if expected_pod_uid and parsed["pod_uid"] != expected_pod_uid:
            raise RuntimeError("GPU_LEASE_POD_MISMATCH")
        if parsed["container_id"] != container_id:
            raise RuntimeError("GPU_LEASE_CONTAINER_MISMATCH")
        if expected_boot_id and parsed["boot_id"] != expected_boot_id:
            raise RuntimeError("GPU_LEASE_BOOT_MISMATCH")
        gpu_uuid = parsed.get("gpu_uuid")
        if isinstance(gpu_uuid, str) and gpu_uuid:
            if not GPU_UUID_RE.fullmatch(gpu_uuid):
                raise RuntimeError("GPU_LEASE_INVALID: malformed gpu_uuid")
            if expected_gpu_uuid and gpu_uuid != expected_gpu_uuid:
                raise RuntimeError("GPU_LEASE_DEVICE_MISMATCH")
        exp = _parse_expiry_epoch(parsed.get("expires_at"))
        ref_now = time.time() if now_epoch is None else now_epoch
        if exp <= ref_now:
            raise RuntimeError("GPU_LEASE_EXPIRED")
        grant = {
            "pod_uid": parsed["pod_uid"],
            "container_id": parsed["container_id"],
            "boot_id": parsed["boot_id"],
            "attempt_id": parsed["attempt_id"],
            "lease_id": parsed["lease_id"],
            "fencing_token": fencing_token,
            "expires_at": parsed["expires_at"],
        }
        if isinstance(gpu_uuid, str) and gpu_uuid:
            grant["gpu_uuid"] = gpu_uuid

    handshake_url = (environ.get("BLENDER_SUPERVISOR_HANDSHAKE_URL") or "").strip()
    if handshake_url:
        gpu_epoch = int(environ.get("BLENDER_GPU_EPOCH") or "0")
        payload = {
            "gpu_epoch": gpu_epoch,
            "container_id": container_id,
            "pod_uid": expected_pod_uid,
            "boot_id": expected_boot_id,
            "grant": grant,
        }
        headers = {"Content-Type": "application/json"}
        sup_token = (environ.get("BLENDER_SUPERVISOR_TOKEN") or environ.get("BLENDER_WORKER_AUTH_TOKEN") or "").strip()
        if sup_token:
            headers["Authorization"] = f"Bearer {sup_token}"
        req = urllib.request.Request(
            handshake_url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                reply = json.loads(resp.read(16384))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise RuntimeError(f"PRE_CUDA_HANDSHAKE_REJECTED: {exc}") from exc
        if not isinstance(reply, dict) or reply.get("allowed") is not True:
            raise RuntimeError("PRE_CUDA_HANDSHAKE_DENIED")
    elif policy == "required":
        raise RuntimeError("PRE_CUDA_HANDSHAKE_URL_MISSING")

    return grant


def run_blender_startup() -> None:
    setup_epoch_shm_hooks(os.environ)
    try:
        verify_pre_cuda_gate(os.environ)
    except Exception as exc:
        sys.stderr.write(f"blender pre-cuda gate rejected startup: {exc}\n")
        sys.stderr.flush()
        os._exit(78)

    import bpy
    import importlib.util

    base_dir = os.path.dirname(os.path.abspath(__file__))
    startup_spec = importlib.util.spec_from_file_location(
        "blender_startup_workspace",
        os.path.join(base_dir, "startup_workspace.py"),
    )
    if startup_spec is None or startup_spec.loader is None:
        raise RuntimeError("cannot load Blender startup workspace module")
    startup = importlib.util.module_from_spec(startup_spec)
    sys.modules[startup_spec.name] = startup
    startup_spec.loader.exec_module(startup)
    startup.proof = startup.load(bpy, os.environ)

    plugin = os.path.join(base_dir, "blender-mcp-plugin", "register.py")
    spec = importlib.util.spec_from_file_location("beagle_blender_mcp_register", plugin)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Blender MCP register script")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)


if __name__ == "__main__" or "bpy" in sys.modules:
    run_blender_startup()
