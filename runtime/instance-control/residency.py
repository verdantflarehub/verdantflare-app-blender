"""Zero-VRAM standby, checkpoint hibernate/wake, 6-tuple lease guard, and subreaper lineage for Blender workers."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

PR_SET_CHILD_SUBREAPER = 36
DEFAULT_HIBERNATE_IDLE_SECONDS = 180
DEFAULT_GUI_IDLE_HIBERNATE_SECONDS = 3600
DEFAULT_SOFT_EVICT_DEADLINE_MS = 10000
DEFAULT_MAX_CHECKPOINTS = 5
DEFAULT_WAKE_TIMEOUT_SECONDS = 30.0
GPU_UUID_RE = re.compile(r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
HEX64_RE = re.compile(r"([0-9a-f]{64})", re.IGNORECASE)
GEN_HEX_RE = re.compile(r"[0-9a-f]{32}\Z")
SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}\Z")
HIBERNATE_ASSET_RE = re.compile(r"checkpoints/auto_hibernate_gen[0-9a-f]{32}_v[0-9]+\.blend\Z")

RESIDENCY_STATES = frozenset({
    "active_compute",
    "human_dirty",
    "idle_evictable",
    "zero_vram_standby",
    "waking",
    "draining_hard",
    "reconciling",
    "quarantined",
})


class ResidencyError(Exception):
    def __init__(self, code: str, status: int = 409, data: dict | None = None):
        self.code = code
        self.status = status
        self.data = data
        super().__init__(code)


LOGGER = logging.getLogger(__name__)


def _fsync_directory(dir_path: Path) -> None:
    if not sys.platform.startswith("linux"):
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    dfd = os.open(str(dir_path), flags)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


@dataclass
class ProcessLineage:
    gpu_epoch: int
    root_pid: int
    root_starttime_ticks: int | None
    started_at: float
    adopted_descendants: dict[int, int] = field(default_factory=dict)


@dataclass
class ReapOutcome:
    reclaimed: bool
    reason: str = "reclaimed"
    survivor_pids: list[int] = field(default_factory=list)


def enable_child_subreaper() -> bool:
    """Register current process as POSIX child subreaper (non-privileged)."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        rc = libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
        return rc == 0
    except OSError:
        return False


def resolve_container_id(
    env: dict[str, str] | None = None,
    cgroup_path: Path = Path("/proc/self/cgroup"),
) -> str:
    """Resolve real container runtime ID from env or /proc/self/cgroup (never static 'worker')."""
    environ = os.environ if env is None else env
    for key in ("VF_CONTAINER_ID", "CONTAINER_ID"):
        raw = (environ.get(key) or "").strip()
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
    pod_uid = (environ.get("BLENDER_POD_UID") or environ.get("POD_UID") or "").strip()
    if pod_uid:
        return f"pod-{pod_uid}-blender"
    return f"local-pid-{os.getpid()}"


def parse_proc_stat(stat_text: str) -> dict[str, int | str]:
    """Parse /proc/<pid>/stat safely across comm names containing spaces or ')'."""
    lparen = stat_text.find("(")
    rparen = stat_text.rfind(")")
    if lparen < 0 or rparen <= lparen:
        raise ValueError("malformed /proc/stat")
    pid = int(stat_text[:lparen].strip())
    comm = stat_text[lparen + 1:rparen]
    rest = stat_text[rparen + 1:].split()
    if len(rest) < 20:
        raise ValueError("truncated /proc/stat")
    return {
        "pid": pid,
        "comm": comm,
        "state": rest[0],
        "ppid": int(rest[1]),
        "pgrp": int(rest[2]),
        "starttime_ticks": int(rest[19]),
    }


def read_proc_stat(pid: int, proc_root: Path = Path("/proc")) -> dict[str, int | str] | None:
    if type(pid) is not int or pid < 1:
        return None
    stat_file = Path(proc_root) / str(pid) / "stat"
    try:
        return parse_proc_stat(stat_file.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def is_pid_identity_alive(
    pid: int,
    expected_ticks: int | None,
    proc_root: Path = Path("/proc"),
) -> bool:
    info = read_proc_stat(pid, proc_root=proc_root)
    if info is None:
        return False
    if expected_ticks is not None and int(info["starttime_ticks"]) != expected_ticks:
        return False
    return True


def list_adopted_descendants(
    owner_pid: int,
    root_pgrp: int | None = None,
    proc_root: Path = Path("/proc"),
    *,
    root_pid: int | None = None,
) -> dict[int, int]:
    """Return {pid: starttime_ticks} for transitive subtree, pgrp peers, and subreaper-adopted orphans."""
    all_procs: dict[int, dict[str, int | str]] = {}
    base = Path(proc_root)
    if not base.is_dir():
        return {}
    try:
        entries = list(base.iterdir())
    except OSError:
        return {}
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid <= 1 or pid == owner_pid:
            continue
        info = read_proc_stat(pid, proc_root=base)
        if info is not None:
            all_procs[pid] = info

    belonging: set[int] = set()
    for pid, info in all_procs.items():
        if (
            (root_pid is not None and pid == root_pid)
            or info["ppid"] == owner_pid
            or (root_pgrp is not None and root_pgrp > 1 and info["pgrp"] == root_pgrp)
        ):
            belonging.add(pid)

    changed = True
    while changed:
        changed = False
        for pid, info in all_procs.items():
            if pid not in belonging and int(info["ppid"]) in belonging:
                belonging.add(pid)
                changed = True

    return {pid: int(all_procs[pid]["starttime_ticks"]) for pid in sorted(belonging)}


def _resolve_visible_gpu_uuid(
    env: dict[str, str] | None = None,
    *,
    dev_root: Path = Path("/dev"),
    proc_gpus_root: Path = Path("/proc/driver/nvidia/gpus"),
    allow_host_probe: bool = False,
) -> str:
    environ = os.environ if env is None else env
    for key in ("BLENDER_GPU_UUID", "VF_VISIBLE_GPU_UUIDS", "NVIDIA_VISIBLE_DEVICES"):
        raw = (environ.get(key) or "").strip()
        if not raw or raw in {"all", "none", "void"}:
            continue
        for token in raw.split(","):
            cand = token.strip().split("::", 1)[0].strip()
            if GPU_UUID_RE.fullmatch(cand):
                return cand
    if not allow_host_probe:
        return ""
    try:
        minors = sorted(
            int(p.name[len("nvidia"):])
            for p in Path(dev_root).iterdir()
            if p.name.startswith("nvidia") and p.name[len("nvidia"):].isdigit()
        )
    except OSError:
        minors = []
    if not minors:
        return ""
    minor_to_uuid: dict[int, str] = {}
    try:
        for info_file in sorted(Path(proc_gpus_root).glob("*/information")):
            try:
                text = info_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            m_uuid = re.search(r"GPU UUID:\s*(GPU-[0-9a-fA-F-]+)", text)
            m_minor = re.search(r"Device Minor:\s*(\d+)", text)
            if m_uuid and m_minor and GPU_UUID_RE.fullmatch(m_uuid.group(1).strip()):
                minor_to_uuid[int(m_minor.group(1))] = m_uuid.group(1).strip()
    except OSError:
        return ""
    for minor in minors:
        if minor in minor_to_uuid:
            return minor_to_uuid[minor]
    return ""


def _list_container_resident_procs(
    supervisor_pid: int,
    proc_root: Path = Path("/proc"),
) -> list[dict[str, int]]:
    base = Path(proc_root)
    if not base.is_dir():
        return []
    procs: list[dict[str, int]] = []
    try:
        entries = list(base.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid <= 0 or pid == int(supervisor_pid):
            continue
        info = read_proc_stat(pid, proc_root=base)
        if info is None or info["state"] == "Z" or int(info["starttime_ticks"]) <= 0:
            continue
        procs.append({"pid": pid, "starttime_ticks": int(info["starttime_ticks"])})
        if len(procs) >= 128:
            break
    procs.sort(key=lambda item: item["pid"])
    return procs


def _list_nvidia_fd_pids(
    supervisor_pid: int,
    proc_root: Path = Path("/proc"),
) -> dict[int, int | None]:
    base = Path(proc_root)
    if not base.is_dir():
        return {}
    holders: dict[int, int | None] = {}
    try:
        entries = list(base.iterdir())
    except OSError:
        return {}
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid <= 1 or pid == int(supervisor_pid):
            continue
        fd_dir = entry / "fd"
        if not fd_dir.is_dir():
            continue
        has_nv = False
        try:
            for fd_entry in fd_dir.iterdir():
                try:
                    target = os.readlink(fd_entry)
                except OSError:
                    continue
                if target.startswith("/dev/nvidia"):
                    has_nv = True
                    break
        except OSError:
            continue
        if has_nv:
            info = read_proc_stat(pid, proc_root=base)
            if info is not None and info["state"] != "Z":
                holders[pid] = int(info["starttime_ticks"])
    return holders


def epoch_shm_dir(shm_root: Path, gpu_epoch: int) -> Path:
    return Path(shm_root) / f"vf-blender-epoch-{gpu_epoch}"


def epoch_shm_manifest_path(shm_root: Path, gpu_epoch: int) -> Path:
    return Path(shm_root) / f".vf-blender-manifest-epoch-{gpu_epoch}.json"


def register_epoch_shm_object(manifest_file: Path, name: str) -> None:
    if not isinstance(name, str) or not name or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError("invalid shm object name")
    mf = Path(manifest_file)
    mf.parent.mkdir(parents=True, exist_ok=True)
    items: list[str] = []
    if mf.is_file():
        try:
            loaded = json.loads(mf.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                items = [x for x in loaded if isinstance(x, str)]
        except (OSError, ValueError):
            items = []
    if name not in items:
        items.append(name)
        tmp = mf.with_name(f"{mf.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(items), encoding="utf-8")
        os.replace(tmp, mf)


def cleanup_scoped_shm(
    shm_root: Path = Path("/dev/shm"),
    *,
    gpu_epoch: int | None = None,
    manifest_file: Path | None = None,
) -> list[str]:
    """Delete ONLY the exited gpu_epoch's directory and producer-manifest shm objects."""
    cleaned: list[str] = []
    root = Path(shm_root)
    if not root.is_dir():
        return cleaned
    if gpu_epoch is not None:
        edir = epoch_shm_dir(root, gpu_epoch)
        if edir.exists() or edir.is_symlink():
            try:
                if edir.is_symlink() or edir.is_file():
                    edir.unlink(missing_ok=True)
                else:
                    shutil.rmtree(edir, ignore_errors=True)
                cleaned.append(edir.name)
            except OSError:
                pass
        if manifest_file is None:
            manifest_file = epoch_shm_manifest_path(root, gpu_epoch)
    if manifest_file is not None:
        mf = Path(manifest_file)
        if mf.is_file():
            try:
                names = json.loads(mf.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                names = []
            if isinstance(names, list):
                for name in names:
                    if not isinstance(name, str) or not name or "/" in name or "\\" in name or name in (".", ".."):
                        continue
                    target = root / name
                    try:
                        if target.is_symlink() or target.is_file():
                            target.unlink(missing_ok=True)
                            cleaned.append(name)
                        elif target.is_dir():
                            shutil.rmtree(target, ignore_errors=True)
                            cleaned.append(name)
                    except OSError:
                        pass
            try:
                mf.unlink(missing_ok=True)
            except OSError:
                pass
    return cleaned


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


def validate_launch_grant(
    grant: dict,
    *,
    expected_pod_uid: str = "",
    expected_container_id: str = "",
    expected_boot_id: str = "",
    expected_gpu_uuid: str = "",
    min_fencing_token: int = 0,
    consumed_fencing_token: int = 0,
    revoked_leases: set[str] | None = None,
    revoked_attempts: set[str] | None = None,
    now_epoch: float | None = None,
) -> dict:
    """Validate canonical 6-tuple (pod_uid, container_id, boot_id, attempt_id, lease_id, fencing_token) + expiry."""
    if not isinstance(grant, dict):
        raise ResidencyError("GPU_LEASE_INVALID", 403)
    fencing_token = grant.get("fencing_token")
    if fencing_token is None and type(grant.get("lease_token")) is int:
        fencing_token = grant["lease_token"]
    if (
        "fencing_token" in grant
        and "lease_token" in grant
        and grant["lease_token"] != grant["fencing_token"]
    ):
        raise ResidencyError("GPU_LEASE_INVALID", 403)
    required_str = ("pod_uid", "container_id", "boot_id", "attempt_id", "lease_id")
    for key in required_str:
        val = grant.get(key)
        if not isinstance(val, str) or not val.strip():
            raise ResidencyError("GPU_LEASE_INVALID", 403)
    if type(fencing_token) is not int or fencing_token <= 0 or "expires_at" not in grant:
        raise ResidencyError("GPU_LEASE_INVALID", 403)

    pod_uid = grant["pod_uid"].strip()
    container_id = grant["container_id"].strip()
    boot_id = grant["boot_id"].strip()
    attempt_id = grant["attempt_id"].strip()
    lease_id = grant["lease_id"].strip()
    gpu_uuid = grant.get("gpu_uuid")
    if gpu_uuid is not None or expected_gpu_uuid:
        if not isinstance(gpu_uuid, str) or not GPU_UUID_RE.fullmatch(gpu_uuid):
            raise ResidencyError("GPU_LEASE_INVALID", 403)
        if expected_gpu_uuid and gpu_uuid != expected_gpu_uuid:
            raise ResidencyError("GPU_LEASE_DEVICE_MISMATCH", 403)

    if (revoked_leases and lease_id in revoked_leases) or (revoked_attempts and attempt_id in revoked_attempts):
        raise ResidencyError("GPU_LEASE_REVOKED", 409)
    if fencing_token < min_fencing_token or fencing_token <= consumed_fencing_token:
        raise ResidencyError("STALE_FENCING_TOKEN", 409)
    if expected_pod_uid and pod_uid != expected_pod_uid:
        raise ResidencyError("GPU_LEASE_POD_MISMATCH", 403)
    if expected_container_id and container_id != expected_container_id:
        raise ResidencyError("GPU_LEASE_CONTAINER_MISMATCH", 403)
    if expected_boot_id and boot_id != expected_boot_id:
        raise ResidencyError("GPU_LEASE_BOOT_MISMATCH", 409)
    try:
        exp = _parse_expiry_epoch(grant["expires_at"])
    except ValueError:
        raise ResidencyError("GPU_LEASE_INVALID", 403) from None
    ref_now = time.time() if now_epoch is None else now_epoch
    if exp <= ref_now:
        raise ResidencyError("GPU_LEASE_EXPIRED", 403)

    normalized = {
        "pod_uid": pod_uid,
        "container_id": container_id,
        "boot_id": boot_id,
        "attempt_id": attempt_id,
        "lease_id": lease_id,
        "fencing_token": fencing_token,
        "lease_token": fencing_token,
        "expires_at": grant["expires_at"],
    }
    if isinstance(gpu_uuid, str) and gpu_uuid:
        normalized["gpu_uuid"] = gpu_uuid
    if isinstance(grant.get("node_uid"), str) and grant["node_uid"]:
        normalized["node_uid"] = grant["node_uid"]
    return normalized


def _safe_workspace_path(root: Path, relative: str) -> Path | None:
    current = root
    for part in Path(relative).parts:
        if part in ("", ".", ".."):
            return None
        current = current / part
        try:
            if current.is_symlink():
                return None
        except OSError:
            return None
    try:
        resolved = current.resolve()
    except OSError:
        return None
    if root not in resolved.parents:
        return None
    return resolved


def _file_digest(path: Path) -> tuple[int, str] | None:
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
            return None
        sha = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                sha.update(chunk)
        return info.st_size, sha.hexdigest()
    except OSError:
        return None


class BlenderResidencyController:
    """Coordinates Blender S1-S4 GPU residency, checkpoint hibernate, and subreaper-managed wake."""

    def __init__(
        self,
        state,
        gui_lease,
        drain_journal,
        *,
        workspace_root: Path | None = None,
        sentinel_path: Path | None = None,
        pid_state_path: Path | None = None,
        proc_root: Path = Path("/proc"),
        shm_root: Path = Path("/dev/shm"),
        cgroup_path: Path = Path("/proc/self/cgroup"),
        clock=time.monotonic,
        wall_clock=time.time,
        hibernate_idle_seconds: int | None = None,
        gui_idle_hibernate_seconds: int | None = None,
        soft_evict_deadline_ms: int | None = None,
        max_checkpoints: int | None = None,
        wake_timeout_seconds: float | None = None,
        grant_policy: str | None = None,
        boot_id: str | None = None,
        container_id: str | None = None,
        supervisor_pid: int | None = None,
        fence_state_path: Path | None = None,
        handshake_url: str | None = None,
        spawner=None,
        wake_hook=None,
        kill_fn=os.kill,
    ):
        self.state = state
        self.gui = gui_lease
        self.drain = drain_journal
        self.workspace_root = Path(
            workspace_root or os.environ.get("WORKSPACE_ROOT", "/workspace")
        ).resolve()
        self.sentinel_path = Path(
            sentinel_path or os.environ.get("BLENDER_HIBERNATE_SENTINEL", "/tmp/blender-hibernate.sentinel")
        )
        self.pid_state_path = Path(
            pid_state_path or os.environ.get("BLENDER_PROCESS_STATE_FILE", "/tmp/blender-process-state.json")
        )
        self.fence_state_path = Path(
            fence_state_path
            or os.environ.get("BLENDER_GPU_FENCE_STATE_FILE")
            or (self.workspace_root / ".verdantflare/gpu_fence_state.json")
        )
        self.proc_root = Path(proc_root)
        self.shm_root = Path(shm_root)
        self.cgroup_path = Path(cgroup_path)
        self.clock = clock
        self.wall_clock = wall_clock
        self.kill_fn = kill_fn
        self.supervisor_pid = int(supervisor_pid if supervisor_pid is not None else os.getpid())
        sup_stat = read_proc_stat(self.supervisor_pid, proc_root=self.proc_root)
        self.supervisor_starttime: int | None = int(sup_stat["starttime_ticks"]) if sup_stat else None
        self._fallback_supervisor_ticks = max(1, int(self.wall_clock() * 100))
        self.hibernate_idle_seconds = max(
            1,
            int(
                hibernate_idle_seconds
                if hibernate_idle_seconds is not None
                else os.environ.get("BLENDER_HIBERNATE_IDLE_SECONDS", str(DEFAULT_HIBERNATE_IDLE_SECONDS))
            ),
        )
        self.gui_idle_hibernate_seconds = max(
            60,
            int(
                gui_idle_hibernate_seconds
                if gui_idle_hibernate_seconds is not None
                else os.environ.get("BLENDER_GUI_IDLE_HIBERNATE_SECONDS", str(DEFAULT_GUI_IDLE_HIBERNATE_SECONDS))
            ),
        )
        self.soft_evict_deadline_ms = max(
            200,
            int(
                soft_evict_deadline_ms
                if soft_evict_deadline_ms is not None
                else os.environ.get("BLENDER_SOFT_EVICT_DEADLINE_MS", str(DEFAULT_SOFT_EVICT_DEADLINE_MS))
            ),
        )
        self.max_checkpoints = max(
            1,
            int(
                max_checkpoints
                if max_checkpoints is not None
                else os.environ.get("BLENDER_MAX_HIBERNATE_CHECKPOINTS", str(DEFAULT_MAX_CHECKPOINTS))
            ),
        )
        self.wake_timeout_seconds = float(
            wake_timeout_seconds
            if wake_timeout_seconds is not None
            else os.environ.get("BLENDER_WAKE_TIMEOUT_SECONDS", str(DEFAULT_WAKE_TIMEOUT_SECONDS))
        )

        raw_policy = (grant_policy or os.environ.get("BLENDER_GPU_GRANT_POLICY") or "").strip().lower()
        if not raw_policy:
            if (os.environ.get("STATION_RUNTIME_GPU_LEASE_REQUIRED") or "").strip().lower() == "true":
                raw_policy = "required"
            else:
                raw_policy = "compat_unmanaged"
        if raw_policy not in {"required", "compat_unmanaged", "cpu_exempt"}:
            raise ValueError(f"invalid grant_policy: {raw_policy}")
        self.grant_policy = raw_policy

        self.boot_id = (boot_id or os.environ.get("VF_BOOT_ID") or str(uuid.uuid4())).strip()
        self.container_id = (container_id or resolve_container_id(os.environ, cgroup_path=self.cgroup_path)).strip()
        self.handshake_url = handshake_url or os.environ.get("BLENDER_SUPERVISOR_HANDSHAKE_URL", "")
        self.handshake_token = secrets.token_urlsafe(32)
        self.residency_callback_token = os.environ.get("VF_RESIDENCY_CALLBACK_TOKEN", "").strip()
        self.spawner = spawner
        self.wake_hook = wake_hook
        self.subreaper_enabled = enable_child_subreaper()

        self.lock = threading.RLock()
        self.state_cv = threading.Condition(self.lock)
        self.lifecycle_lock = threading.RLock()
        self.gpu_epoch = max(1, int(os.environ.get("BLENDER_GPU_EPOCH", "1")))
        self.residency_seq = 1
        self.quarantined = False
        self.quarantine_reason = "none"
        self.hibernating = False
        self.hard_draining = False
        self.batch_cancel_seq = 0
        self.batch_module = None
        self.waking = False
        self._observed_state = "zero_vram_standby" if self.sentinel_path.is_file() else "idle_evictable"
        self.in_flight_ops = 0
        self.in_flight_batch = 0
        self.activity_seq = 0
        self.last_idle_at = self.clock()
        self.vram_allocated_mib: int | None = None
        self.colocated_heavy_queued = False
        self.last_known_generation: str | None = None
        self.lineage: ProcessLineage | None = None
        self.blender_proc = None

        self.min_fencing_token = 0
        self.consumed_fencing_token = 0
        self.installed_max_fencing_token = 0
        self.fencing_watermarks_by_gpu: dict[str, dict[str, int]] = {}
        self.revoked_leases: set[str] = set()
        self.revoked_attempts: set[str] = set()
        self.installed_leases: set[str] = set()
        self.installed_attempts: set[str] = set()
        self.pending_releases: dict[str, dict] = {}
        self.sealed_proofs_by_lease: dict[str, dict] = {}
        self.last_reclaim_proof: dict | None = None
        self.active_grant: dict | None = None
        self.residency_sync_enabled = os.environ.get("VF_GPU_RESIDENCY_SYNC") == "1"
        self._sync_wakeup = threading.Event()
        self._last_reported_key: tuple[str, int, int] | None = None
        self._last_heartbeat_mono: float = 0.0
        self._allow_host_gpu_probe = (
            spawner is None and wake_hook is None and self.proc_root == Path("/proc")
        )
        self._last_grant_gpu_uuid: str = _resolve_visible_gpu_uuid(
            allow_host_probe=self._allow_host_gpu_probe
        )
        self._active_watermark_gpu_uuid: str = (
            os.environ.get("BLENDER_GPU_UUID") or self._last_grant_gpu_uuid or ""
        ).strip()
        self._warm_grant_settled = False
        self.fence_state_corrupted = False
        self.fence_state_unhealthy: str | None = None
        self._ensure_epoch_shm_dir(self.gpu_epoch)
        self._refresh_pid_lineage()
        self._load_fence_state_on_boot()
        if self.pending_releases:
            self.flush_pending_releases()

    def _effective_supervisor_ticks(self) -> int:
        if type(self.supervisor_starttime) is int and self.supervisor_starttime > 0:
            return self.supervisor_starttime
        stat_info = read_proc_stat(self.supervisor_pid, proc_root=self.proc_root)
        if stat_info is not None and int(stat_info["starttime_ticks"]) > 0:
            self.supervisor_starttime = int(stat_info["starttime_ticks"])
            return self.supervisor_starttime
        return self._fallback_supervisor_ticks

    def _mark_fence_state_corrupted_locked(self, code: str, detail: str = "") -> None:
        self.fence_state_corrupted = True
        self.fence_state_unhealthy = code
        self.quarantined = True
        self.quarantine_reason = code
        LOGGER.error("Blender GPU fence state fail-closed (%s): %s", code, detail or self.fence_state_path)

    def _check_fence_state_healthy_locked(self) -> None:
        if self.fence_state_corrupted:
            raise ResidencyError(
                "FENCE_STATE_CORRUPTED",
                503,
                {"quarantine_reason": "FENCE_STATE_CORRUPTED", "gpu_epoch": self.gpu_epoch},
            )
        if self.fence_state_unhealthy is not None:
            raise ResidencyError(
                self.fence_state_unhealthy,
                503,
                {"quarantine_reason": self.fence_state_unhealthy, "gpu_epoch": self.gpu_epoch},
            )

    def _select_gpu_watermarks_locked(self, gpu_uuid: str) -> None:
        g = (gpu_uuid or "").strip()
        if not g:
            return
        if self._active_watermark_gpu_uuid and self._active_watermark_gpu_uuid != g:
            self.fencing_watermarks_by_gpu[self._active_watermark_gpu_uuid] = {
                "min": int(self.min_fencing_token),
                "consumed": int(self.consumed_fencing_token),
                "installed_max": int(self.installed_max_fencing_token),
            }
            wm = self.fencing_watermarks_by_gpu.get(g) or {}
            self.min_fencing_token = int(wm.get("min", 0))
            self.consumed_fencing_token = int(wm.get("consumed", 0))
            self.installed_max_fencing_token = int(wm.get("installed_max", 0))
        self._active_watermark_gpu_uuid = g

    def _load_fence_state_on_boot(self) -> None:
        if not os.path.lexists(self.fence_state_path):
            return
        if self.fence_state_path.is_symlink() or not self.fence_state_path.is_file():
            self._mark_fence_state_corrupted_locked("FENCE_STATE_CORRUPTED", "fence state path is symlink or non-file")
            return
        try:
            raw_bytes = self.fence_state_path.read_bytes()
            if len(raw_bytes) == 0 or len(raw_bytes) > (2 << 20):
                self._mark_fence_state_corrupted_locked("FENCE_STATE_CORRUPTED", "fence state file empty or oversized")
                return
            raw = json.loads(raw_bytes.decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            self._mark_fence_state_corrupted_locked("FENCE_STATE_CORRUPTED", str(exc))
            return
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            self._mark_fence_state_corrupted_locked("FENCE_STATE_CORRUPTED", "invalid fence state schema_version")
            return

        min_tok = raw.get("min_fencing_token", 0)
        consumed_tok = raw.get("consumed_fencing_token", 0)
        installed_max_tok = raw.get("installed_max_fencing_token", 0)
        if not all(type(v) is int and v >= 0 for v in (min_tok, consumed_tok, installed_max_tok)):
            self._mark_fence_state_corrupted_locked("FENCE_STATE_CORRUPTED", "invalid token watermarks")
            return
        for list_key in ("revoked_leases", "revoked_attempts", "installed_leases", "installed_attempts"):
            items = raw.get(list_key, [])
            if not isinstance(items, list) or any(not isinstance(x, str) or not x.strip() for x in items):
                self._mark_fence_state_corrupted_locked("FENCE_STATE_CORRUPTED", f"invalid list field {list_key}")
                return
        raw_pending = raw.get("pending_releases", {})
        raw_proofs = raw.get("sealed_proofs_by_lease", {})
        last_pr = raw.get("last_reclaim_proof")
        if (
            not isinstance(raw_pending, dict)
            or any(not isinstance(k, str) or not k.strip() or not isinstance(v, dict) for k, v in raw_pending.items())
            or not isinstance(raw_proofs, dict)
            or any(not isinstance(k, str) or not k.strip() or not isinstance(v, dict) for k, v in raw_proofs.items())
            or (last_pr is not None and not isinstance(last_pr, dict))
        ):
            self._mark_fence_state_corrupted_locked("FENCE_STATE_CORRUPTED", "invalid map or proof field")
            return

        raw_wm_map = raw.get("fencing_watermarks_by_gpu")
        if raw_wm_map is not None:
            if not isinstance(raw_wm_map, dict):
                self._mark_fence_state_corrupted_locked("FENCE_STATE_CORRUPTED", "invalid fencing_watermarks_by_gpu")
                return
            for gk, gv in raw_wm_map.items():
                if (
                    not isinstance(gk, str)
                    or not gk.strip()
                    or not isinstance(gv, dict)
                    or not all(type(gv.get(k, 0)) is int and gv.get(k, 0) >= 0 for k in ("min", "consumed", "installed_max"))
                ):
                    self._mark_fence_state_corrupted_locked("FENCE_STATE_CORRUPTED", f"invalid gpu watermark {gk!r}")
                    return
                self.fencing_watermarks_by_gpu[gk.strip()] = {
                    "min": int(gv.get("min", 0)),
                    "consumed": int(gv.get("consumed", 0)),
                    "installed_max": int(gv.get("installed_max", 0)),
                }

        current_gpu = self._active_watermark_gpu_uuid
        saved_gpu = str(raw.get("gpu_uuid") or "").strip() if isinstance(raw.get("gpu_uuid"), str) else ""
        saved_pod_uid = str((last_pr or {}).get("sealed_pod_uid") or "").strip() if isinstance(last_pr, dict) else ""
        current_pod_uid = (os.environ.get("BLENDER_POD_UID") or "").strip()

        if raw_wm_map is not None and current_gpu:
            wm_cur = self.fencing_watermarks_by_gpu.get(current_gpu) or {}
            min_tok = int(wm_cur.get("min", 0))
            consumed_tok = int(wm_cur.get("consumed", 0))
            installed_max_tok = int(wm_cur.get("installed_max", 0))
        elif saved_gpu and current_gpu and saved_gpu != current_gpu:
            self.fencing_watermarks_by_gpu[saved_gpu] = {
                "min": int(max(min_tok, consumed_tok, installed_max_tok)),
                "consumed": int(max(consumed_tok, installed_max_tok)),
                "installed_max": 0,
            }
            min_tok, consumed_tok, installed_max_tok = 0, 0, 0
        elif not saved_gpu and saved_pod_uid and current_pod_uid and saved_pod_uid != current_pod_uid:
            # Legacy v0.1.23 fence state without gpu_uuid across a replaced Pod:
            # scheduler fencing_token is per physical GPU card, while Pod replacement is already fenced by pod_uid & boot_id.
            min_tok, consumed_tok, installed_max_tok = 0, 0, 0

        if min_tok > 0:
            self.min_fencing_token = max(self.min_fencing_token, min_tok)
        if consumed_tok > 0:
            self.consumed_fencing_token = max(self.consumed_fencing_token, consumed_tok)
            self.min_fencing_token = max(self.min_fencing_token, consumed_tok)
        for item in raw.get("revoked_leases") or []:
            self.revoked_leases.add(item.strip())
        for item in raw.get("revoked_attempts") or []:
            self.revoked_attempts.add(item.strip())

        promoted = False
        # Any grant previously installed before a restart while lineage is absent/standby is consumed on boot
        if self.lineage is None or self.is_standby():
            if installed_max_tok > 0:
                self.consumed_fencing_token = max(self.consumed_fencing_token, installed_max_tok)
                self.min_fencing_token = max(self.min_fencing_token, installed_max_tok)
                self.installed_max_fencing_token = 0
                promoted = True
            for item in raw.get("installed_leases") or []:
                self.revoked_leases.add(item.strip())
                promoted = True
            for item in raw.get("installed_attempts") or []:
                self.revoked_attempts.add(item.strip())
                promoted = True
        else:
            if installed_max_tok > 0:
                self.installed_max_fencing_token = max(self.installed_max_fencing_token, installed_max_tok)
            for item in raw.get("installed_leases") or []:
                self.installed_leases.add(item.strip())
            for item in raw.get("installed_attempts") or []:
                self.installed_attempts.add(item.strip())

        for lid, rel_body in raw_pending.items():
            self.pending_releases[lid.strip()] = rel_body
        for lid, pr in raw_proofs.items():
            self.sealed_proofs_by_lease[lid.strip()] = pr
        if isinstance(last_pr, dict):
            self.last_reclaim_proof = last_pr
        if promoted:
            try:
                self._persist_fence_state_locked()
            except ResidencyError as exc:
                self._mark_fence_state_corrupted_locked(exc.code, str(exc))

    def _persist_fence_state_locked(self, *, candidate_overrides: dict | None = None) -> None:
        if self.fence_state_corrupted:
            raise ResidencyError("FENCE_STATE_CORRUPTED", 503)
        rev_leases = set(
            candidate_overrides["revoked_leases"]
            if candidate_overrides and "revoked_leases" in candidate_overrides
            else self.revoked_leases
        )
        rev_attempts = set(
            candidate_overrides["revoked_attempts"]
            if candidate_overrides and "revoked_attempts" in candidate_overrides
            else self.revoked_attempts
        )
        sealed_map = dict(
            candidate_overrides["sealed_proofs_by_lease"]
            if candidate_overrides and "sealed_proofs_by_lease" in candidate_overrides
            else self.sealed_proofs_by_lease
        )
        if len(rev_leases) > 4096:
            rev_leases = set(sorted(rev_leases)[-2048:])
        if len(rev_attempts) > 4096:
            rev_attempts = set(sorted(rev_attempts)[-2048:])
        if len(sealed_map) > 512:
            keep = list(sealed_map.keys())[-256:]
            sealed_map = {k: sealed_map[k] for k in keep}
        eff_min = int(
            candidate_overrides["min_fencing_token"]
            if candidate_overrides and "min_fencing_token" in candidate_overrides
            else self.min_fencing_token
        )
        eff_consumed = int(
            candidate_overrides["consumed_fencing_token"]
            if candidate_overrides and "consumed_fencing_token" in candidate_overrides
            else self.consumed_fencing_token
        )
        eff_inst_max = int(
            candidate_overrides["installed_max_fencing_token"]
            if candidate_overrides and "installed_max_fencing_token" in candidate_overrides
            else self.installed_max_fencing_token
        )
        wm_by_gpu = {k: dict(v) for k, v in self.fencing_watermarks_by_gpu.items()}
        active_gpu = (
            self._active_watermark_gpu_uuid
            or (self.active_grant or {}).get("gpu_uuid")
            or self._last_grant_gpu_uuid
            or ""
        ).strip()
        if active_gpu:
            wm_by_gpu[active_gpu] = {
                "min": eff_min,
                "consumed": eff_consumed,
                "installed_max": eff_inst_max,
            }
        payload = {
            "schema_version": 1,
            "boot_id": self.boot_id,
            "gpu_uuid": active_gpu,
            "fencing_watermarks_by_gpu": wm_by_gpu,
            "min_fencing_token": eff_min,
            "consumed_fencing_token": eff_consumed,
            "installed_max_fencing_token": eff_inst_max,
            "revoked_leases": sorted(rev_leases),
            "revoked_attempts": sorted(rev_attempts),
            "installed_leases": sorted(
                candidate_overrides["installed_leases"]
                if candidate_overrides and "installed_leases" in candidate_overrides
                else self.installed_leases
            ),
            "installed_attempts": sorted(
                candidate_overrides["installed_attempts"]
                if candidate_overrides and "installed_attempts" in candidate_overrides
                else self.installed_attempts
            ),
            "pending_releases": (
                candidate_overrides["pending_releases"]
                if candidate_overrides and "pending_releases" in candidate_overrides
                else self.pending_releases
            ),
            "sealed_proofs_by_lease": sealed_map,
            "last_reclaim_proof": (
                candidate_overrides["last_reclaim_proof"]
                if candidate_overrides and "last_reclaim_proof" in candidate_overrides
                else self.last_reclaim_proof
            ),
        }
        tmp: Path | None = None
        try:
            self.fence_state_path.parent.mkdir(parents=True, exist_ok=True)
            if self.fence_state_path.is_symlink():
                raise OSError("fence state target is a symlink")
            tmp = self.fence_state_path.with_name(f".{self.fence_state_path.name}.{secrets.token_hex(6)}.tmp")
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            with tmp.open("wb") as fh:
                fh.write(encoded)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.fence_state_path)
            tmp = None
            _fsync_directory(self.fence_state_path.parent)
            self.fencing_watermarks_by_gpu = wm_by_gpu
            self.revoked_leases = rev_leases
            self.revoked_attempts = rev_attempts
            self.sealed_proofs_by_lease = sealed_map
            self.fence_state_unhealthy = None
        except OSError as exc:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            self.fence_state_unhealthy = "FENCE_STATE_PERSISTENCE_FAILED"
            LOGGER.error("Failed to persist Blender GPU fence state %s: %s", self.fence_state_path, exc)
            raise ResidencyError("FENCE_STATE_PERSISTENCE_FAILED", 503, {"detail": str(exc)}) from exc

    def _record_grant_installed_locked(self, grant: dict | None) -> None:
        if not isinstance(grant, dict):
            return
        if self.fence_state_corrupted:
            raise ResidencyError("FENCE_STATE_CORRUPTED", 503)
        tok = int(grant.get("fencing_token") or 0)
        lease_id = str(grant.get("lease_id") or "").strip()
        attempt_id = str(grant.get("attempt_id") or "").strip()
        next_min = max(self.min_fencing_token, tok) if tok > 0 else self.min_fencing_token
        next_inst_max = max(self.installed_max_fencing_token, tok) if tok > 0 else self.installed_max_fencing_token
        next_inst_leases = set(self.installed_leases)
        next_inst_attempts = set(self.installed_attempts)
        if lease_id:
            next_inst_leases.add(lease_id)
        if attempt_id:
            next_inst_attempts.add(attempt_id)
        self._persist_fence_state_locked(
            candidate_overrides={
                "min_fencing_token": next_min,
                "installed_max_fencing_token": next_inst_max,
                "installed_leases": next_inst_leases,
                "installed_attempts": next_inst_attempts,
            }
        )
        self.min_fencing_token = next_min
        self.installed_max_fencing_token = next_inst_max
        self.installed_leases = next_inst_leases
        self.installed_attempts = next_inst_attempts

    def _prior_or_current_sealed_epoch_locked(self, lease_id: str) -> int:
        prior = self.sealed_proofs_by_lease.get(lease_id)
        if isinstance(prior, dict) and type(prior.get("sealed_gpu_epoch")) is int and prior["sealed_gpu_epoch"] > 0:
            return prior["sealed_gpu_epoch"]
        return max(1, int(self.gpu_epoch))

    def _build_reclaim_proof_locked(
        self,
        sealed_epoch: int,
        *,
        lease_id: str = "",
        attempt_id: str = "",
        revoked_token: int = 0,
        consumed_token: int | None = None,
    ) -> dict:
        pod_uid = os.environ.get("BLENDER_POD_UID", "")
        eff_epoch = max(1, int(sealed_epoch))
        sup_ticks = self._effective_supervisor_ticks()
        eff_consumed = int(self.consumed_fencing_token if consumed_token is None else consumed_token)
        proof: dict = {
            "sealed_pod_uid": pod_uid,
            "sealed_container_id": self.container_id,
            "sealed_boot_id": self.boot_id,
            "sealed_gpu_epoch": eff_epoch,
            "supervisor_pid": int(self.supervisor_pid),
            "supervisor_starttime_ticks": int(sup_ticks),
            "consumed_fencing_token": eff_consumed,
            "reclaimed": True,
        }
        if lease_id:
            proof["revoked_lease_id"] = lease_id
            proof["revoked_fencing_token"] = int(max(revoked_token, eff_consumed))
        if attempt_id:
            proof["revoked_attempt_id"] = attempt_id
        return proof

    def _seal_grant_and_journal_release_locked(
        self,
        grant: dict | None,
        sealed_epoch: int,
        *,
        explicit_lease_id: str = "",
        explicit_attempt_id: str = "",
        explicit_token: int = 0,
        enqueue_release: bool = True,
    ) -> dict:
        lease_id = explicit_lease_id.strip() if isinstance(explicit_lease_id, str) else ""
        attempt_id = explicit_attempt_id.strip() if isinstance(explicit_attempt_id, str) else ""
        token_val = int(explicit_token) if type(explicit_token) is int and explicit_token > 0 else 0
        if isinstance(grant, dict):
            g_lid = str(grant.get("lease_id") or "").strip()
            g_att = str(grant.get("attempt_id") or "").strip()
            g_tok = int(grant.get("fencing_token") or 0)
            if not lease_id and g_lid:
                lease_id = g_lid
            if not attempt_id and g_att:
                attempt_id = g_att
            if g_tok > token_val:
                token_val = g_tok
        if token_val > 0:
            self.min_fencing_token = max(self.min_fencing_token, token_val)
            self.consumed_fencing_token = max(self.consumed_fencing_token, token_val)
        if lease_id:
            self.revoked_leases.add(lease_id)
            self.installed_leases.discard(lease_id)
        if attempt_id:
            self.revoked_attempts.add(attempt_id)
            self.installed_attempts.discard(attempt_id)
        if not self.installed_leases and not self.installed_attempts:
            self.installed_max_fencing_token = 0

        eff_epoch = max(1, int(sealed_epoch))
        proof = self._build_reclaim_proof_locked(
            eff_epoch,
            lease_id=lease_id,
            attempt_id=attempt_id,
            revoked_token=token_val,
            consumed_token=self.consumed_fencing_token,
        )
        next_sealed_proofs = dict(self.sealed_proofs_by_lease)
        if lease_id:
            next_sealed_proofs[lease_id] = proof
        next_pending = dict(self.pending_releases)
        if enqueue_release and lease_id and token_val > 0:
            pod_uid = os.environ.get("BLENDER_POD_UID", "")
            existing = next_pending.get(lease_id)
            idem_key = (
                existing["idempotency_key"]
                if isinstance(existing, dict) and isinstance(existing.get("idempotency_key"), str) and existing["idempotency_key"]
                else f"blender-reap:{lease_id}:epoch:{eff_epoch}"
            )
            next_pending[lease_id] = {
                "idempotency_key": idem_key,
                "fencing_token": int(token_val),
                "worker_settled": True,
                "pod_uid": pod_uid,
                "container_id": self.container_id,
                "boot_id": self.boot_id,
                "gpu_epoch": eff_epoch,
                "supervisor_pid": int(self.supervisor_pid),
                "supervisor_starttime_ticks": int(proof["supervisor_starttime_ticks"]),
                "reclaim_mode": "epoch_unload",
                "reclaim_proof": proof,
            }
        self._persist_fence_state_locked(
            candidate_overrides={
                "pending_releases": next_pending,
                "sealed_proofs_by_lease": next_sealed_proofs,
                "last_reclaim_proof": proof,
            }
        )
        self.pending_releases = next_pending
        self.last_reclaim_proof = proof
        return proof

    def flush_pending_releases(self) -> int:
        scheduler_url = (
            os.environ.get("STATION_GPU_SCHEDULER_URL")
            or os.environ.get("STATION_RUNTIME_SCHEDULER_URL")
            or ""
        ).rstrip("/")
        if not scheduler_url:
            return 0
        with self.lock:
            items = [(lid, dict(body)) for lid, body in self.pending_releases.items()]
        if not items:
            return 0
        headers = {"Content-Type": "application/json"}
        sched_token = (
            os.environ.get("STATION_GPU_SCHEDULER_TOKEN")
            or os.environ.get("STATION_RUNTIME_SCHEDULER_TOKEN")
            or ""
        )
        if sched_token:
            headers["Authorization"] = f"Bearer {sched_token}"
        no_proxy_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        ack_ids: list[str] = []
        for lease_id, rel_body in items:
            if not lease_id or not isinstance(rel_body, dict):
                ack_ids.append(lease_id)
                continue
            payload = json.dumps(rel_body, separators=(",", ":")).encode("utf-8")
            req = urllib.request.Request(
                f"{scheduler_url}/internal/v1/gpu-scheduler/leases/{lease_id}/release",
                data=payload,
                headers=headers,
                method="POST",
            )
            try:
                with no_proxy_opener.open(req, timeout=5) as resp:
                    status_code = getattr(resp, "status", 200)
                    raw_bytes = resp.read(65536)
                    data = json.loads(raw_bytes.decode("utf-8")) if raw_bytes else None
                    if (
                        status_code == 200
                        and isinstance(data, dict)
                        and data.get("lease_id") == lease_id
                        and data.get("state") in {"released", "expired"}
                    ):
                        ack_ids.append(lease_id)
                    else:
                        LOGGER.warning(
                            "Retaining pending Blender GPU release for lease %s: unexpected response status=%s body=%r",
                            lease_id,
                            status_code,
                            data,
                        )
            except urllib.error.HTTPError as exc:
                LOGGER.warning(
                    "Retaining pending Blender GPU release for lease %s: scheduler HTTP %s",
                    lease_id,
                    exc.code,
                )
            except (OSError, ValueError) as exc:
                LOGGER.warning(
                    "Retaining pending Blender GPU release for lease %s: %s",
                    lease_id,
                    exc,
                )
        if not ack_ids:
            return 0
        with self.lock:
            next_pending = dict(self.pending_releases)
            removed = 0
            for lid in ack_ids:
                if lid in next_pending:
                    next_pending.pop(lid, None)
                    removed += 1
            if removed:
                try:
                    self._persist_fence_state_locked(candidate_overrides={"pending_releases": next_pending})
                    self.pending_releases = next_pending
                except ResidencyError as exc:
                    LOGGER.warning("Failed to persist Blender pending_releases flush completion: %s", exc)
                    return 0
            return removed

    def _ensure_epoch_shm_dir(self, epoch: int) -> Path:
        edir = epoch_shm_dir(self.shm_root, epoch)
        try:
            edir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return edir

    def _write_pid_state(self, pid: int, starttime_ticks: int | None, epoch: int) -> None:
        try:
            self.pid_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.pid_state_path.with_name(f"{self.pid_state_path.name}.{uuid.uuid4().hex}.tmp")
            payload = {"pid": pid, "gpu_epoch": epoch}
            if starttime_ticks is not None:
                payload["starttime_ticks"] = starttime_ticks
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self.pid_state_path)
        except OSError:
            pass

    def _register_spawned_process(self, proc_or_pid, epoch: int) -> None:
        """Immediately record spawned child lineage BEFORE waiting for adapter readiness (H2)."""
        pid = 0
        if hasattr(proc_or_pid, "pid"):
            self.blender_proc = proc_or_pid
            pid = int(proc_or_pid.pid)
        elif type(proc_or_pid) is int and proc_or_pid > 1:
            pid = proc_or_pid
        self.gpu_epoch = epoch
        if pid > 1:
            stat_info = read_proc_stat(pid, proc_root=self.proc_root)
            ticks = int(stat_info["starttime_ticks"]) if stat_info else None
            self.lineage = ProcessLineage(
                gpu_epoch=epoch,
                root_pid=pid,
                root_starttime_ticks=ticks,
                started_at=self.clock(),
            )
            self._write_pid_state(pid, ticks, epoch)
        else:
            self._refresh_pid_lineage()

    def _refresh_pid_lineage(self) -> None:
        if not self.pid_state_path.is_file():
            return
        try:
            raw = json.loads(self.pid_state_path.read_text(encoding="utf-8"))
            pid = int(raw.get("pid", 0))
            epoch = int(raw.get("gpu_epoch", self.gpu_epoch))
            if epoch > self.gpu_epoch:
                self.gpu_epoch = epoch
            if pid > 1:
                saved_ticks = raw.get("starttime_ticks")
                stat_info = read_proc_stat(pid, proc_root=self.proc_root)
                ticks = (
                    int(saved_ticks)
                    if type(saved_ticks) is int
                    else (int(stat_info["starttime_ticks"]) if stat_info else None)
                )
                if self.lineage is None or self.lineage.root_pid != pid or self.lineage.gpu_epoch != self.gpu_epoch:
                    self.lineage = ProcessLineage(
                        gpu_epoch=self.gpu_epoch,
                        root_pid=pid,
                        root_starttime_ticks=ticks,
                        started_at=self.clock(),
                    )
        except (OSError, ValueError, TypeError):
            pass

    def _guard_not_quarantined(self) -> None:
        self._check_fence_state_healthy_locked()
        if self.quarantined:
            code = (
                self.quarantine_reason
                if self.quarantine_reason in {"FENCE_STATE_CORRUPTED", "FENCE_STATE_PERSISTENCE_FAILED"}
                else "UNRECLAIMED_GPU_LINEAGE"
            )
            raise ResidencyError(
                code,
                503,
                {"quarantine_reason": self.quarantine_reason, "gpu_epoch": self.gpu_epoch},
            )

    def is_standby(self) -> bool:
        return (
            not self.quarantined
            and not self.hibernating
            and not self.waking
            and self.sentinel_path.is_file()
        )

    def read_hibernate_manifest(self) -> dict | None:
        """Read and verify hibernate.json + full checkpoint SHA-256 + instance_id + pod_uid (M1)."""
        manifest_path = _safe_workspace_path(self.workspace_root, ".verdantflare/hibernate.json")
        if manifest_path is None or not manifest_path.is_file():
            return None
        try:
            if manifest_path.stat().st_size > 65536:
                return None
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                not isinstance(data, dict)
                or data.get("schema_version") != 1
                or not isinstance(data.get("generation"), str)
                or not GEN_HEX_RE.fullmatch(data["generation"])
                or type(data.get("scene_version")) is not int
                or data["scene_version"] < 0
                or not isinstance(data.get("checkpoint_asset_id"), str)
                or not HIBERNATE_ASSET_RE.fullmatch(data["checkpoint_asset_id"])
                or not isinstance(data.get("sha256"), str)
                or not SHA256_HEX_RE.fullmatch(data["sha256"])
                or type(data.get("size")) is not int
                or data["size"] <= 0
            ):
                return None

            expected_instance = getattr(self.state, "instance_id", "") or os.environ.get("INSTANCE_ID", "")
            expected_pod_uid = os.environ.get("BLENDER_POD_UID", "")
            if expected_instance and data.get("instance_id") != expected_instance:
                return None
            if expected_pod_uid and data.get("pod_uid") != expected_pod_uid:
                return None

            ckpt = _safe_workspace_path(self.workspace_root, data["checkpoint_asset_id"])
            if ckpt is None:
                return None
            digest_pair = _file_digest(ckpt)
            if digest_pair != (data["size"], data["sha256"]):
                return None
            return data
        except (OSError, ValueError, TypeError):
            return None

    def standby_probe(self) -> dict | None:
        """Return cryptographically verified standby identity without waking Blender (zero fallback)."""
        with self.lock:
            if not self.is_standby():
                return None
            try:
                sentinel_data = json.loads(self.sentinel_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                sentinel_data = None
            if isinstance(sentinel_data, dict) and sentinel_data.get("hard_drained") is True:
                gen_val = str(sentinel_data.get("generation") or self.last_known_generation or ("0" * 32))
                return {
                    "generation": gen_val,
                    "scene_version": int(sentinel_data.get("scene_version") or 0),
                    "startup": None,
                    "checkpoint_asset_id": None,
                    "hard_drained": True,
                }
            manifest = self.read_hibernate_manifest()
            if manifest is None:
                return None
            self.last_known_generation = manifest["generation"]
            return {
                "generation": manifest["generation"],
                "scene_version": manifest["scene_version"],
                "startup": manifest.get("startup_proof"),
                "checkpoint_asset_id": manifest["checkpoint_asset_id"],
            }

    def _compute_state_and_reason(self) -> tuple[str, str, bool, bool]:
        gui_held = self.gui.session is not None
        if self.quarantined:
            return "quarantined", "unreclaimed_gpu_lineage", False, gui_held
        if self.waking:
            return "waking", "running_task", False, gui_held
        if self.is_standby():
            return "zero_vram_standby", "none", False, False
        if self.hibernating:
            return "active_compute", "checkpoint_pending", False, gui_held
        try:
            drain_record = self.drain.read()
        except Exception:
            drain_record = None
        if drain_record and drain_record.get("phase") != "aborted":
            return "draining_hard", "checkpoint_pending", False, gui_held
        if gui_held:
            return "human_dirty", "human_gui_active", False, True
        if self.in_flight_ops > 0 or self.in_flight_batch > 0:
            return "active_compute", "running_task", False, False
        return "idle_evictable", "none", True, False

    def _sync_state(self) -> tuple[str, str, bool, bool]:
        state_name, reason, evictable, human_protected = self._compute_state_and_reason()
        if state_name != self._observed_state:
            self._observed_state = state_name
            self.residency_seq += 1
            self._sync_wakeup.set()
        return state_name, reason, evictable, human_protected

    def gpu_state(self) -> dict:
        with self.lock:
            self._refresh_pid_lineage()
            state_name, reason, evictable, human_protected = self._sync_state()
            if state_name in {"active_compute", "waking", "draining_hard", "quarantined"}:
                idle_sec = 0
            elif state_name == "human_dirty":
                idle_sec = self.gui.input_idle_seconds()
            else:
                idle_sec = max(0, int(self.clock() - self.last_idle_at))
            if state_name == "quarantined":
                vram = None
            elif state_name == "zero_vram_standby":
                vram = 0
            else:
                vram = self.vram_allocated_mib
            return {
                "residency_state": state_name,
                "evictable": evictable,
                "human_protected": human_protected,
                "dirty": human_protected,
                "blocking_reason": reason,
                "residency_seq": self.residency_seq,
                "gpu_epoch": self.gpu_epoch,
                "vram_allocated_mib": vram,
                "idle_seconds": idle_sec,
            }

    def _get_batch_module(self):
        if self.batch_module is not None:
            return self.batch_module
        mod = sys.modules.get("blender_batch_run") or sys.modules.get("batch_run")
        if mod is not None:
            return mod
        try:
            import batch_run as _batch_run  # type: ignore[import-not-found]
            return _batch_run
        except ImportError:
            return None

    def authorize_batch_launch(self, expected_epoch: int, expected_cancel_seq: int) -> None:
        """Atomically verify batch subprocess launch authorization against epoch and force-drain barrier."""
        with self.lock:
            self._guard_not_quarantined()
            if self.is_standby():
                raise ResidencyError("INSTANCE_STANDBY", 409)
            if self.hibernating or self.hard_draining or self.batch_cancel_seq != expected_cancel_seq:
                raise ResidencyError("INSTANCE_DRAINING", 409)
            if self.gpu_epoch != expected_epoch:
                raise ResidencyError("STALE_GPU_EPOCH", 409, {"current_gpu_epoch": self.gpu_epoch})
            self._verify_active_grant_locked()

    @contextmanager
    def activity_scope(self, *, batch: bool = False):
        self.ensure_awake()
        self._ensure_grant_ready_outside_lock()
        with self.lock:
            self._guard_not_quarantined()
            if self.is_standby():
                raise ResidencyError("INSTANCE_STANDBY", 409)
            if self.hibernating or self.hard_draining:
                raise ResidencyError("INSTANCE_DRAINING", 409)
            self._verify_active_grant_locked()
            entry_epoch = self.gpu_epoch
            entry_cancel_seq = self.batch_cancel_seq
            self.activity_seq += 1
            if batch:
                self.in_flight_batch += 1
            else:
                self.in_flight_ops += 1
            self._sync_state()

        def _guard() -> None:
            self.authorize_batch_launch(entry_epoch, entry_cancel_seq)

        prev_guard = None
        batch_mod = self._get_batch_module() if batch else None
        if batch_mod is not None:
            prev_guard = batch_mod.bind_thread_batch_guard(_guard)
        try:
            yield _guard
        finally:
            if batch_mod is not None:
                batch_mod.bind_thread_batch_guard(prev_guard)
            with self.lock:
                if batch:
                    self.in_flight_batch = max(0, self.in_flight_batch - 1)
                else:
                    self.in_flight_ops = max(0, self.in_flight_ops - 1)
                if self.in_flight_ops == 0 and self.in_flight_batch == 0:
                    self.last_idle_at = self.clock()
                self._sync_state()
                self.state_cv.notify_all()

    def _is_active_grant_revoked_locked(self) -> bool:
        if self.grant_policy != "required":
            return False
        if self.active_grant is None:
            return True
        lease_id = self.active_grant.get("lease_id", "")
        attempt_id = self.active_grant.get("attempt_id", "")
        token = self.active_grant.get("fencing_token", 0)
        if (
            lease_id in self.revoked_leases
            or attempt_id in self.revoked_attempts
            or token < self.min_fencing_token
            or token <= self.consumed_fencing_token
        ):
            return True
        try:
            exp = _parse_expiry_epoch(self.active_grant.get("expires_at"))
        except ValueError:
            return True
        return exp <= self.wall_clock()

    def _verify_active_grant_locked(self) -> None:
        if self.grant_policy != "required":
            return
        if self.active_grant is None:
            raise ResidencyError("GPU_LEASE_REQUIRED", 503)
        validate_launch_grant(
            self.active_grant,
            expected_pod_uid=os.environ.get("BLENDER_POD_UID", ""),
            expected_container_id=self.container_id,
            expected_boot_id=self.boot_id,
            expected_gpu_uuid=os.environ.get("BLENDER_GPU_UUID", ""),
            min_fencing_token=self.min_fencing_token,
            consumed_fencing_token=self.consumed_fencing_token,
            revoked_leases=self.revoked_leases,
            revoked_attempts=self.revoked_attempts,
            now_epoch=self.wall_clock(),
        )

    def _ensure_grant_ready_outside_lock(self) -> None:
        if self.grant_policy != "required":
            return
        with self.lock:
            if self.active_grant is not None:
                try:
                    self._verify_active_grant_locked()
                    return
                except ResidencyError:
                    self.active_grant = None
        self._acquire_wake_grant_if_required()

    def install_launch_grant(self, request: dict) -> dict:
        with self.lock:
            if self.fence_state_corrupted:
                raise ResidencyError("FENCE_STATE_CORRUPTED", 503)
            if self.quarantined:
                self._guard_not_quarantined()
            if isinstance(request, dict) and isinstance(request.get("gpu_uuid"), str) and GPU_UUID_RE.fullmatch(request["gpu_uuid"].strip()):
                self._select_gpu_watermarks_locked(request["gpu_uuid"].strip())
            validated = validate_launch_grant(
                request,
                expected_pod_uid=os.environ.get("BLENDER_POD_UID", ""),
                expected_container_id=self.container_id,
                expected_boot_id=self.boot_id,
                expected_gpu_uuid=os.environ.get("BLENDER_GPU_UUID", ""),
                min_fencing_token=self.min_fencing_token,
                consumed_fencing_token=self.consumed_fencing_token,
                revoked_leases=self.revoked_leases,
                revoked_attempts=self.revoked_attempts,
                now_epoch=self.wall_clock(),
            )
            self._record_grant_installed_locked(validated)
            self.active_grant = validated
            self._warm_grant_settled = False
            if isinstance(validated.get("gpu_uuid"), str) and validated["gpu_uuid"]:
                self._last_grant_gpu_uuid = validated["gpu_uuid"]
            self._last_heartbeat_mono = self.clock()
            self._sync_wakeup.set()
            if "colocated_heavy_queued" in request and isinstance(request["colocated_heavy_queued"], bool):
                self.colocated_heavy_queued = request["colocated_heavy_queued"]
            return {
                "ok": True,
                "lease_id": validated["lease_id"],
                "fencing_token": validated["fencing_token"],
                "lease_token": validated["fencing_token"],
            }

    def pre_cuda_check(self, request: dict) -> dict:
        """Live atomic check invoked by blender-startup.py right before loading bpy/CUDA (H3)."""
        if not isinstance(request, dict):
            raise ResidencyError("INVALID_ARGUMENT", 400)
        with self.lock:
            self._guard_not_quarantined()
            if self.is_standby():
                raise ResidencyError("INSTANCE_STANDBY", 409)
            req_epoch = request.get("gpu_epoch")
            if type(req_epoch) is not int or req_epoch != self.gpu_epoch:
                raise ResidencyError("STALE_GPU_EPOCH", 409, {"current_gpu_epoch": self.gpu_epoch})
            req_cid = request.get("container_id")
            if isinstance(req_cid, str) and req_cid and req_cid != self.container_id:
                raise ResidencyError("GPU_LEASE_CONTAINER_MISMATCH", 403)
            req_boot = request.get("boot_id")
            if isinstance(req_boot, str) and req_boot and req_boot != self.boot_id:
                raise ResidencyError("GPU_LEASE_BOOT_MISMATCH", 409)

            grant_payload = request.get("grant")
            if self.grant_policy == "required" or grant_payload is not None:
                if self.active_grant is None:
                    raise ResidencyError("GPU_LEASE_REVOKED", 409)
                target_grant = grant_payload if isinstance(grant_payload, dict) else self.active_grant
                validated = validate_launch_grant(
                    target_grant,
                    expected_pod_uid=os.environ.get("BLENDER_POD_UID", ""),
                    expected_container_id=self.container_id,
                    expected_boot_id=self.boot_id,
                    expected_gpu_uuid=os.environ.get("BLENDER_GPU_UUID", ""),
                    min_fencing_token=self.min_fencing_token,
                    consumed_fencing_token=self.consumed_fencing_token,
                    revoked_leases=self.revoked_leases,
                    revoked_attempts=self.revoked_attempts,
                    now_epoch=self.wall_clock(),
                )
                if validated["lease_id"] != self.active_grant["lease_id"] or validated["fencing_token"] != self.active_grant["fencing_token"]:
                    raise ResidencyError("STALE_FENCING_TOKEN", 409)
            return {"allowed": True, "gpu_epoch": self.gpu_epoch, "boot_id": self.boot_id}

    @staticmethod
    def _extract_grant_from_decision(
        raw_resp: dict,
        *,
        expected_attempt_id: str,
        expected_pod_uid: str,
        expected_container_id: str,
        expected_boot_id: str,
        expected_gpu_uuid: str,
    ) -> dict:
        if not isinstance(raw_resp, dict):
            raise ResidencyError("GPU_LEASE_INVALID", 403)
        if raw_resp.get("attempt_id") != expected_attempt_id:
            raise ResidencyError("GPU_LEASE_ATTEMPT_MISMATCH", 403)
        if "state" not in raw_resp:
            return raw_resp
        state = raw_resp.get("state")
        if state not in {"leased", "running"}:
            raise ResidencyError("GPU_LEASE_NOT_GRANTED", 503, {"state": state})
        gpu_uuid = raw_resp.get("gpu_uuid") or ""
        gpu_uuids = raw_resp.get("gpu_uuids")
        if not gpu_uuid and isinstance(gpu_uuids, list) and gpu_uuids:
            gpu_uuid = str(gpu_uuids[0])
        if expected_gpu_uuid and gpu_uuid != expected_gpu_uuid:
            raise ResidencyError("GPU_LEASE_DEVICE_MISMATCH", 403)
        grant = {
            "pod_uid": raw_resp.get("pod_uid", ""),
            "container_id": raw_resp.get("container_id", ""),
            "boot_id": raw_resp.get("boot_id", ""),
            "attempt_id": raw_resp.get("attempt_id", ""),
            "lease_id": raw_resp.get("lease_id", ""),
            "fencing_token": raw_resp.get("fencing_token"),
            "expires_at": raw_resp.get("expires_at"),
        }
        if gpu_uuid:
            grant["gpu_uuid"] = gpu_uuid
        if isinstance(raw_resp.get("node_uid"), str) and raw_resp["node_uid"]:
            grant["node_uid"] = raw_resp["node_uid"]
        return grant

    def _acquire_wake_grant_if_required(self) -> None:
        if self.grant_policy != "required":
            return
        if self.pending_releases:
            self.flush_pending_releases()
        with self.lock:
            if self.active_grant is not None:
                try:
                    self._verify_active_grant_locked()
                    return
                except ResidencyError:
                    self.active_grant = None
            instance_id = getattr(self.state, "instance_id", "") or os.environ.get("INSTANCE_ID", "blender")
            gui_open = self.gui.session is not None
        scheduler_url = (
            os.environ.get("STATION_GPU_SCHEDULER_URL")
            or os.environ.get("STATION_RUNTIME_SCHEDULER_URL")
            or ""
        ).rstrip("/")
        if not scheduler_url:
            raise ResidencyError("GPU_LEASE_REQUIRED", 503)
        attempt_id = str(uuid.uuid4())
        pod_uid = os.environ.get("BLENDER_POD_UID", "")
        explicit_gpu_uuid = os.environ.get("BLENDER_GPU_UUID", "").strip()
        cand_gpu_uuid = (
            explicit_gpu_uuid
            or self._last_grant_gpu_uuid
            or _resolve_visible_gpu_uuid(allow_host_probe=self._allow_host_gpu_probe)
        )
        node_uid = (os.environ.get("BLENDER_NODE_UID") or os.environ.get("NODE_UID") or "").strip()
        idempotency_key = f"blender-wake:{instance_id}:{attempt_id}"
        req_dict: dict = {
            "idempotency_key": idempotency_key,
            "consumer_kind": "blender_human_wake" if gui_open else "blender_wake",
            "instance_id": instance_id,
            "task_id": f"wake-{attempt_id}",
            "attempt_id": attempt_id,
            "pod_uid": pod_uid,
            "container_id": self.container_id,
            "boot_id": self.boot_id,
            "candidate_gpu_uuids": [cand_gpu_uuid] if cand_gpu_uuid else [],
            "priority_class": "human_wake" if gui_open else "shared_slice",
        }
        if node_uid:
            req_dict["node_uid"] = node_uid
        payload = json.dumps(req_dict, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        sched_token = (
            os.environ.get("STATION_GPU_SCHEDULER_TOKEN")
            or os.environ.get("STATION_RUNTIME_SCHEDULER_TOKEN")
            or ""
        )
        if sched_token:
            headers["Authorization"] = f"Bearer {sched_token}"

        queue_timeout = float(
            os.environ.get("BLENDER_WAKE_QUEUE_TIMEOUT_SECONDS", str(min(self.wake_timeout_seconds, 30.0)))
        )
        no_proxy_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = self.clock() + max(0.5, queue_timeout)
        last_decision: dict | None = None
        while True:
            req = urllib.request.Request(
                f"{scheduler_url}/internal/v1/gpu-scheduler/leases/acquire",
                data=payload,
                headers=headers,
                method="POST",
            )
            try:
                with no_proxy_opener.open(req, timeout=15) as resp:
                    status_code = getattr(resp, "status", 200)
                    raw_decision = json.loads(resp.read(65536).decode("utf-8"))
            except (OSError, ValueError) as exc:
                raise ResidencyError("GPU_LEASE_UNAVAILABLE", 503) from exc

            if not isinstance(raw_decision, dict):
                raise ResidencyError("GPU_LEASE_INVALID", 403)
            last_decision = raw_decision
            state = raw_decision.get("state")
            if status_code == 202 or state in {"queued", "draining"}:
                if self.clock() >= deadline:
                    lease_id = raw_decision.get("lease_id")
                    if isinstance(lease_id, str) and lease_id:
                        try:
                            rel_payload = json.dumps({
                                "idempotency_key": f"cancel:{idempotency_key}",
                                "fencing_token": int(raw_decision.get("fencing_token") or 0),
                                "worker_settled": True,
                            }).encode("utf-8")
                            rel_req = urllib.request.Request(
                                f"{scheduler_url}/internal/v1/gpu-scheduler/leases/{lease_id}/release",
                                data=rel_payload,
                                headers=headers,
                                method="POST",
                            )
                            with no_proxy_opener.open(rel_req, timeout=5):
                                pass
                        except OSError:
                            pass
                    raise ResidencyError(
                        "GPU_LEASE_QUEUE_TIMEOUT",
                        503,
                        {
                            "lease_id": lease_id,
                            "blocking_reason": raw_decision.get("blocking_reason", "queued"),
                            "queue_position": raw_decision.get("queue_position", 0),
                        },
                    )
                time.sleep(0.2)
                continue
            break

        canonical_grant = self._extract_grant_from_decision(
            last_decision,
            expected_attempt_id=attempt_id,
            expected_pod_uid=pod_uid,
            expected_container_id=self.container_id,
            expected_boot_id=self.boot_id,
            expected_gpu_uuid=explicit_gpu_uuid,
        )
        self.install_launch_grant(canonical_grant)

    def _build_child_env(self, epoch: int, *, wake_restore: bool) -> dict[str, str]:
        env = dict(os.environ)
        edir = self._ensure_epoch_shm_dir(epoch)
        mfile = epoch_shm_manifest_path(self.shm_root, epoch)
        env["INSTANCE_ID"] = env.get("INSTANCE_ID") or getattr(self.state, "instance_id", "")
        env["VF_BOOT_ID"] = self.boot_id
        env["VF_CONTAINER_ID"] = self.container_id
        env["BLENDER_GPU_EPOCH"] = str(epoch)
        env["BLENDER_GPU_GRANT_POLICY"] = self.grant_policy
        env["BLENDER_WAKE_RESTORE"] = "1" if wake_restore else "0"
        env["BLENDER_SHM_EPOCH_DIR"] = str(edir)
        env["BLENDER_SHM_MANIFEST_FILE"] = str(mfile)
        env["TMPDIR"] = str(edir)
        if self.handshake_url:
            env["BLENDER_SUPERVISOR_HANDSHAKE_URL"] = self.handshake_url
        env["BLENDER_SUPERVISOR_TOKEN"] = self.handshake_token
        if self.active_grant is not None:
            env["VF_GPU_LAUNCH_GRANT_JSON"] = json.dumps(self.active_grant)
        return env

    def authorized_handshake(self, header: str | None) -> bool:
        if not header or not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[7:].strip(), self.handshake_token)

    def authorized_residency_callback(self, header: str | None) -> bool:
        if not header or not header.startswith("Bearer ") or len(self.residency_callback_token) < 32:
            return False
        return hmac.compare_digest(header[7:].strip(), self.residency_callback_token)

    def _default_blender_spawner(self, epoch: int, wake_restore: bool, child_env: dict[str, str]):
        sock_raw = child_env.get("BLENDER_MCP_ADAPTER_SOCKET", "")
        if sock_raw:
            Path(sock_raw).unlink(missing_ok=True)
        if os.environ.get("BLENDER_SUBREAPER_MANAGED") == "1" and Path("/tmp/supervisor.sock").exists():
            subprocess.run(
                ["supervisorctl", "-s", "unix:///tmp/supervisor.sock", "start", "smithay-display", "kwin-wayland"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        xdg_dir = child_env.get("XDG_RUNTIME_DIR", "")
        smithay_ready = child_env.get("BDWIND_SMITHAY_READY_FILE", "")
        smithay_sock = child_env.get("BDWIND_SMITHAY_CONTROL_SOCKET", "")
        wayland_disp = child_env.get("WAYLAND_DISPLAY", "")
        if xdg_dir and smithay_ready:
            wait_end = self.clock() + min(15.0, self.wake_timeout_seconds)
            while self.clock() < wait_end:
                if (
                    Path(xdg_dir, "bus").exists()
                    and Path(smithay_ready).is_file()
                    and (not smithay_sock or Path(smithay_sock).exists())
                    and (not wayland_disp or Path(xdg_dir, wayland_disp).exists())
                ):
                    break
                time.sleep(0.1)
        startup_script = child_env.get(
            "BLENDER_STARTUP_SCRIPT", "/opt/beagle/blender-mcp/blender-startup.py"
        )
        blender_bin = child_env.get("BLENDER_BIN", "blender")
        popen_kwargs: dict = {"env": child_env}
        if hasattr(os, "setsid"):
            popen_kwargs["start_new_session"] = True
        elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(
            [blender_bin, "--disable-autoexec", "--factory-startup", "--python", startup_script],
            **popen_kwargs,
        )
        threading.Thread(
            target=self._watch_blender_exit,
            args=(proc, epoch),
            daemon=True,
        ).start()
        return proc

    def _watch_blender_exit(self, proc, epoch: int) -> None:
        rc = proc.wait()
        with self.lock:
            if self.hibernating or self.waking or self.quarantined or self.sentinel_path.is_file():
                return
            if self.gpu_epoch != epoch:
                return
            self.pid_state_path.unlink(missing_ok=True)
            self.lineage = None
            self.blender_proc = None
            cleanup_scoped_shm(shm_root=self.shm_root, gpu_epoch=epoch)
        if os.environ.get("BLENDER_SUBREAPER_MANAGED") == "1":
            subprocess.run(
                ["supervisorctl", "-s", "unix:///tmp/supervisor.sock", "shutdown"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if rc != 0:
                os._exit(rc)

    def start_initial_blender(self) -> None:
        """Spawn initial Blender child under instance-control subreaper when not starting in standby."""
        for attempt in range(1, 7):
            try:
                with self.lifecycle_lock:
                    with self.lock:
                        self._guard_not_quarantined()
                        if self.is_standby() or self.blender_proc is not None:
                            return
                    self._acquire_wake_grant_if_required()
                    self._spawn_and_wait_ready(wake_restore=False, manifest=None, expected_gen=None)
                    return
            except ResidencyError as exc:
                LOGGER.error("Initial Blender subreaper startup attempt %d failed (%s): %s", attempt, exc.code, exc.data)
                if exc.code not in {"GPU_LEASE_UNAVAILABLE", "GPU_LEASE_QUEUE_TIMEOUT", "WAKE_TIMEOUT"} or attempt == 6:
                    raise
                time.sleep(2.0)

    def _spawn_and_wait_ready(
        self,
        *,
        wake_restore: bool,
        manifest: dict | None,
        expected_gen: str | None,
        bump_epoch: bool | None = None,
    ) -> None:
        advance_epoch = wake_restore if bump_epoch is None else bool(bump_epoch)
        with self.lock:
            self._guard_not_quarantined()
            if self._is_active_grant_revoked_locked():
                raise ResidencyError("GPU_LEASE_REVOKED", 409)
            next_epoch = self.gpu_epoch + 1 if advance_epoch else self.gpu_epoch
            child_env = self._build_child_env(next_epoch, wake_restore=wake_restore)
            self.waking = True
            self._sync_state()
        try:
            if wake_restore or advance_epoch:
                self.sentinel_path.unlink(missing_ok=True)
            spawned = None
            if self.spawner is not None:
                spawned = self.spawner(next_epoch, wake_restore, child_env)
            elif self.wake_hook is not None:
                spawned = self.wake_hook(manifest, next_epoch)
            elif os.environ.get("BLENDER_SUBREAPER_MANAGED") == "1":
                spawned = self._default_blender_spawner(next_epoch, wake_restore, child_env)

            # Register child PID/ticks/epoch IMMEDIATELY under state lock, then release state lock
            # BEFORE waiting so child HTTP pre_cuda_check handshake never deadlocks (P0-H1 / H2).
            with self.lock:
                self._register_spawned_process(spawned, next_epoch)

            deadline = self.clock() + self.wake_timeout_seconds
            last_err: Exception | None = None
            while True:
                with self.lock:
                    if self._is_active_grant_revoked_locked():
                        raise ResidencyError("GPU_LEASE_REVOKED", 409)
                    proc = self.blender_proc
                if proc is not None and proc.poll() is not None:
                    rc = proc.returncode
                    code = "GPU_LEASE_REVOKED" if rc == 78 else "BLENDER_STARTUP_FAILED"
                    status = 409 if rc == 78 else 503
                    raise ResidencyError(code, status, {"exit_code": rc})
                try:
                    res = self.state.adapter.call(
                        "scene.get",
                        {},
                        request_id=str(uuid.uuid4()),
                        generation=expected_gen,
                        scene_version=None,
                        deadline_ms=2000,
                    )
                    if res.get("ok") is True and res.get("generation"):
                        if expected_gen and res["generation"] != expected_gen:
                            raise ResidencyError("WAKE_GENERATION_MISMATCH", 503)
                        with self.lock:
                            if self._is_active_grant_revoked_locked():
                                raise ResidencyError("GPU_LEASE_REVOKED", 409)
                            self.last_known_generation = res["generation"]
                            self._refresh_pid_lineage()
                            self.last_idle_at = self.clock()
                        return
                except ResidencyError:
                    raise
                except Exception as exc:
                    last_err = exc
                if self.clock() >= deadline:
                    raise ResidencyError("WAKE_TIMEOUT", 503) from last_err
                time.sleep(0.05)
        except Exception as startup_exc:
            # Always reap any spawned child tree on startup/readiness failure or timeout (H2).
            reap_err: ResidencyError | None = None
            try:
                self._reap_gpu_lineage(min(5.0, self.soft_evict_deadline_ms / 1000.0))
            except ResidencyError as r_exc:
                reap_err = r_exc
            if reap_err is not None:
                raise reap_err from startup_exc
            with self.lock:
                is_quarantined = self.quarantined
                had_grant = False
                persist_err: ResidencyError | None = None
                if not is_quarantined and self.active_grant is not None:
                    grant_to_seal = self.active_grant
                    self.active_grant = None
                    try:
                        self._seal_grant_and_journal_release_locked(
                            grant_to_seal,
                            next_epoch,
                            enqueue_release=True,
                        )
                        had_grant = True
                    except ResidencyError as p_exc:
                        self.quarantined = True
                        self.quarantine_reason = p_exc.code
                        is_quarantined = True
                        persist_err = p_exc
            if persist_err is not None:
                raise persist_err from startup_exc
            if had_grant:
                self.flush_pending_releases()
            if wake_restore and manifest is not None and not is_quarantined:
                try:
                    self.sentinel_path.parent.mkdir(parents=True, exist_ok=True)
                    self.sentinel_path.write_text(
                        json.dumps({
                            "generation": manifest["generation"],
                            "scene_version": manifest["scene_version"],
                            "checkpoint_asset_id": manifest["checkpoint_asset_id"],
                            "sha256": manifest["sha256"],
                            "size": manifest["size"],
                        }),
                        encoding="utf-8",
                    )
                except OSError:
                    pass
            if isinstance(startup_exc, ResidencyError):
                raise
            raise ResidencyError("WAKE_FAILED", 503) from startup_exc
        finally:
            with self.lock:
                self.waking = False
                self._sync_state()

    def ensure_awake(self) -> None:
        """Transparently wake Blender from S3 zero-VRAM standby only from a verified checkpoint or clean hard-drain standby (M1)."""
        with self.lifecycle_lock:
            hard_drained = False
            manifest: dict | None = None
            expected_gen: str | None = None
            with self.lock:
                self._guard_not_quarantined()
                if not self.sentinel_path.is_file():
                    return
                try:
                    sentinel_data = json.loads(self.sentinel_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    sentinel_data = None
                if isinstance(sentinel_data, dict) and sentinel_data.get("hard_drained") is True:
                    hard_drained = True
                else:
                    manifest = self.read_hibernate_manifest()
                    if manifest is None:
                        raise ResidencyError("HIBERNATE_MANIFEST_INVALID", 503)
                    expected_gen = manifest["generation"]
            self._acquire_wake_grant_if_required()
            if hard_drained:
                self._spawn_and_wait_ready(wake_restore=False, manifest=None, expected_gen=None, bump_epoch=True)
            else:
                self._spawn_and_wait_ready(wake_restore=True, manifest=manifest, expected_gen=expected_gen, bump_epoch=True)

    def _reap_zombies_nonblocking(self) -> None:
        if self.blender_proc is not None:
            try:
                self.blender_proc.poll()
            except Exception:
                pass
        if hasattr(os, "waitpid") and hasattr(os, "WNOHANG"):
            while True:
                try:
                    wpid, _ = os.waitpid(-1, os.WNOHANG)
                    if wpid <= 0:
                        break
                except OSError:
                    break

    def _collect_lineage_targets(self) -> tuple[dict[int, int | None], int | None]:
        self._refresh_pid_lineage()
        targets: dict[int, int | None] = {}
        root_pid = self.lineage.root_pid if (self.lineage and self.lineage.root_pid > 1) else None
        pgrp: int | None = None
        if root_pid is not None:
            targets[root_pid] = self.lineage.root_starttime_ticks
            stat_info = read_proc_stat(root_pid, proc_root=self.proc_root)
            if stat_info is not None:
                cand_pgrp = int(stat_info["pgrp"])
                own_pgrp = os.getpgid(0) if hasattr(os, "getpgid") else os.getpid()
                if cand_pgrp > 1 and cand_pgrp != own_pgrp:
                    pgrp = cand_pgrp
        for cpid, cticks in list_adopted_descendants(
            self.supervisor_pid,
            root_pgrp=pgrp,
            proc_root=self.proc_root,
            root_pid=root_pid,
        ).items():
            targets.setdefault(cpid, cticks)
        if os.environ.get("BLENDER_SUBREAPER_MANAGED") == "1" and self.kill_fn is os.kill:
            for nv_pid, nv_ticks in _list_nvidia_fd_pids(self.supervisor_pid, proc_root=self.proc_root).items():
                targets.setdefault(nv_pid, nv_ticks)
        return targets, pgrp

    def _reap_gpu_lineage(self, deadline_sec: float) -> ReapOutcome:
        """Reap entire Blender subtree; quarantine on timeout or EPERM without clearing lineage/shm (H1/H4)."""
        epoch = self.gpu_epoch
        if os.environ.get("BLENDER_SUBREAPER_MANAGED") == "1" and self.kill_fn is os.kill:
            if Path("/tmp/supervisor.sock").exists():
                subprocess.run(
                    [
                        "supervisorctl",
                        "-s",
                        "unix:///tmp/supervisor.sock",
                        "stop",
                        "start-webrtc",
                        "kwin-wayland",
                        "smithay-display",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=8,
                    check=False,
                )
            smithay_ready = os.environ.get("BDWIND_SMITHAY_READY_FILE", "")
            if smithay_ready:
                try:
                    Path(smithay_ready).unlink(missing_ok=True)
                except OSError:
                    pass
        permission_denied = False
        sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
        phases = (
            (signal.SIGTERM, max(0.05, deadline_sec * 0.7), False),
            (sigkill, max(0.05, min(1.0, deadline_sec * 0.3)), True),
        )

        for sig, wait_window, force_phase in phases:
            self._reap_zombies_nonblocking()
            targets, pgrp = self._collect_lineage_targets()
            proc_alive = self.blender_proc is not None and self.blender_proc.poll() is None

            if self.blender_proc is not None and proc_alive and self.kill_fn is os.kill:
                try:
                    if force_phase:
                        self.blender_proc.kill()
                    else:
                        self.blender_proc.terminate()
                except PermissionError:
                    permission_denied = True
                except OSError:
                    pass

            if pgrp is not None and hasattr(os, "killpg") and self.kill_fn is os.kill:
                try:
                    os.killpg(pgrp, sig)
                except PermissionError:
                    permission_denied = True
                except OSError:
                    pass

            alive_pids: list[int] = []
            for pid, expected_ticks in targets.items():
                if not is_pid_identity_alive(pid, expected_ticks, proc_root=self.proc_root):
                    continue
                alive_pids.append(pid)
                try:
                    self.kill_fn(pid, sig)
                except PermissionError:
                    permission_denied = True
                except ProcessLookupError:
                    pass
                except OSError:
                    pass

            if not alive_pids and not (self.blender_proc is not None and self.blender_proc.poll() is None):
                break

            end = self.clock() + wait_window
            while self.clock() < end:
                self._reap_zombies_nonblocking()
                cur_targets, _ = self._collect_lineage_targets()
                still_alive = [
                    p for p, t in cur_targets.items()
                    if is_pid_identity_alive(p, t, proc_root=self.proc_root)
                ]
                proc_still = self.blender_proc is not None and self.blender_proc.poll() is None
                if not still_alive and not proc_still:
                    break
                time.sleep(0.02)

        survivors_set: set[int] = set()
        for _ in range(10):
            self._reap_zombies_nonblocking()
            cur_targets, _ = self._collect_lineage_targets()
            pass_survivors = [
                p for p, t in cur_targets.items()
                if is_pid_identity_alive(p, t, proc_root=self.proc_root)
            ]
            if self.blender_proc is not None and self.blender_proc.poll() is None:
                if int(self.blender_proc.pid) not in pass_survivors:
                    pass_survivors.append(int(self.blender_proc.pid))
            if not pass_survivors:
                survivors_set.clear()
                break
            survivors_set.update(pass_survivors)
            for pid in pass_survivors:
                try:
                    self.kill_fn(pid, sigkill)
                except PermissionError:
                    permission_denied = True
                except OSError:
                    pass
            time.sleep(0.01)

        survivors = sorted(survivors_set)
        with self.lock:
            if permission_denied or survivors:
                reason_code = "SIGNAL_PERMISSION_DENIED" if permission_denied else "REAP_TIMEOUT_UNRECLAIMED"
                self.quarantined = True
                self.quarantine_reason = reason_code
                self.vram_allocated_mib = None
                # Remove any premature sentinel written inside Blender before reap failed so standby is never claimed,
                # while strictly preserving self.lineage, self.blender_proc, self.pid_state_path, and /dev/shm!
                try:
                    self.sentinel_path.unlink(missing_ok=True)
                except OSError:
                    pass
                self._sync_state()
                raise ResidencyError(
                    reason_code,
                    503,
                    {"survivor_pids": survivors, "gpu_epoch": epoch},
                )

            self.pid_state_path.unlink(missing_ok=True)
            self.lineage = None
            self.blender_proc = None
            cleanup_scoped_shm(shm_root=self.shm_root, gpu_epoch=epoch)
            return ReapOutcome(reclaimed=True, reason="reclaimed", survivor_pids=[])

    def hibernate(
        self,
        *,
        reason: str = "IDLE_WARM_TTL",
        expected_boot_id: str | None = None,
        expected_residency_seq: int | None = None,
        expected_gpu_epoch: int | None = None,
        force_gui_idle: bool = False,
        force_hard: bool = False,
        colocated_heavy_queued: bool | None = None,
        deadline_ms: int | None = None,
    ) -> dict:
        instance_id = getattr(self.state, "instance_id", "") or os.environ.get("INSTANCE_ID", "")
        with self.gui.lock, self.lifecycle_lock:
            already_standby_resp: dict | None = None
            with self.lock:
                if self.fence_state_corrupted:
                    raise ResidencyError("FENCE_STATE_CORRUPTED", 503)
                if (
                    self.quarantined
                    and self.quarantine_reason == "FENCE_STATE_PERSISTENCE_FAILED"
                    and not self.fence_state_corrupted
                    and self.lineage is None
                    and self.sentinel_path.is_file()
                ):
                    self._seal_grant_and_journal_release_locked(
                        None,
                        self.gpu_epoch,
                        enqueue_release=False,
                    )
                    self.quarantined = False
                    self.quarantine_reason = "none"
                    self.vram_allocated_mib = 0
                if not force_hard:
                    self._guard_not_quarantined()
                self.gui.expire()
                if colocated_heavy_queued is not None:
                    self.colocated_heavy_queued = bool(colocated_heavy_queued)
                self._sync_state()
                if expected_boot_id and expected_boot_id != self.boot_id:
                    raise ResidencyError("BOOT_ID_MISMATCH", 409, {"current_boot_id": self.boot_id})
                if expected_residency_seq is not None and expected_residency_seq != self.residency_seq:
                    raise ResidencyError("RESIDENCY_SEQ_CONFLICT", 409, {"current_residency_seq": self.residency_seq})
                if expected_gpu_epoch is not None and expected_gpu_epoch != self.gpu_epoch:
                    raise ResidencyError("STALE_GPU_EPOCH", 409, {"current_gpu_epoch": self.gpu_epoch})

                if self.is_standby():
                    probe = self.standby_probe()
                    if probe is None:
                        raise ResidencyError("HIBERNATE_MANIFEST_INVALID", 503)
                    proof = self.last_reclaim_proof
                    if proof is None or int(proof.get("sealed_gpu_epoch") or 0) != max(1, int(self.gpu_epoch)):
                        proof = self._seal_grant_and_journal_release_locked(
                            None,
                            self.gpu_epoch,
                            enqueue_release=False,
                        )
                    already_standby_resp = {
                        "ok": True,
                        "settled": True,
                        "released": True,
                        "already_standby": True,
                        "instance_id": instance_id,
                        "boot_id": self.boot_id,
                        "supervisor_pid": int(self.supervisor_pid),
                        "supervisor_starttime_ticks": int(proof["supervisor_starttime_ticks"]),
                        "sealed_gpu_epoch": int(proof["sealed_gpu_epoch"]),
                        "reclaim_proof": proof,
                        "reason": reason,
                        "reason_code": "ALREADY_STANDBY",
                        "residency_state": "zero_vram_standby",
                        "residency_seq": self.residency_seq,
                        "gpu_epoch": self.gpu_epoch,
                        "generation": probe["generation"],
                    }
                else:
                    drain_record = self.drain.read()
                    if drain_record and drain_record.get("phase") != "aborted":
                        raise ResidencyError("INSTANCE_DRAINING", 409)

                    gui_was_held = self.gui.session is not None
                    if not force_hard:
                        if gui_was_held:
                            can_hibernate_gui = (
                                force_gui_idle
                                and self.colocated_heavy_queued
                                and self.gui.input_idle_seconds() >= self.gui_idle_hibernate_seconds
                            )
                            if not can_hibernate_gui:
                                raise ResidencyError("GUI_EDIT_LEASE_HELD", 409)

                        if self.in_flight_ops > 0 or self.in_flight_batch > 0 or self.waking:
                            raise ResidencyError("WORKER_BUSY", 409)

                    snap_activity = self.activity_seq
                    self.hibernating = True
                    if force_hard:
                        self.hard_draining = True
                        self.batch_cancel_seq += 1
                    self._sync_state()
                    self.state_cv.notify_all()

            if already_standby_resp is not None:
                if self.pending_releases:
                    self.flush_pending_releases()
                return already_standby_resp

            if force_hard:
                hard_deadline_sec = max(
                    0.2,
                    min(5.0, float(deadline_ms if deadline_ms is not None else self.soft_evict_deadline_ms) / 1000.0),
                )
                try:
                    if gui_was_held or self.gui.session is not None:
                        try:
                            self.gui.run("stop")
                        except Exception:
                            pass
                        self.gui.session = None
                    _batch_run = self._get_batch_module()
                    if _batch_run is not None:
                        try:
                            _batch_run.abort_active_batch(deadline_sec=hard_deadline_sec)
                        except _batch_run.BatchRunError as b_exc:
                            with self.lock:
                                self.quarantined = True
                                self.quarantine_reason = b_exc.code
                                self.vram_allocated_mib = None
                                try:
                                    self.sentinel_path.unlink(missing_ok=True)
                                except OSError:
                                    pass
                                self._sync_state()
                            raise ResidencyError(b_exc.code, 503, b_exc.data) from b_exc

                    with self.lock:
                        drain_end = time.monotonic() + 0.05
                        while self.in_flight_batch > 0 and time.monotonic() < drain_end:
                            self.state_cv.wait(timeout=max(0.005, drain_end - time.monotonic()))

                    self._reap_gpu_lineage(0.0)

                    gen_str = self.last_known_generation or ("0" * 32)
                    self.sentinel_path.parent.mkdir(parents=True, exist_ok=True)
                    self.sentinel_path.write_text(
                        json.dumps({
                            "hard_drained": True,
                            "generation": gen_str,
                            "scene_version": 0,
                        }),
                        encoding="utf-8",
                    )

                    with self.lock:
                        grant_to_release = self.active_grant
                        reaped_epoch = self.gpu_epoch
                        self.active_grant = None
                        try:
                            proof = self._seal_grant_and_journal_release_locked(
                                grant_to_release,
                                reaped_epoch,
                                enqueue_release=bool(grant_to_release is not None),
                            )
                        except ResidencyError as p_exc:
                            self.quarantined = True
                            self.quarantine_reason = p_exc.code
                            self.vram_allocated_mib = None
                            try:
                                self.sentinel_path.unlink(missing_ok=True)
                            except OSError:
                                pass
                            raise
                        self.quarantined = False
                        self.quarantine_reason = "none"
                        self.vram_allocated_mib = 0
                        self.last_idle_at = self.clock()
                    if self.pending_releases:
                        self.flush_pending_releases()
                finally:
                    with self.lock:
                        self.hibernating = False
                        self.hard_draining = False
                        self._sync_state()
                        self.state_cv.notify_all()

                with self.lock:
                    proof = self.last_reclaim_proof
                    if proof is None:
                        raise ResidencyError("FENCE_STATE_PERSISTENCE_FAILED", 503)
                    return {
                        "ok": True,
                        "settled": True,
                        "released": True,
                        "already_standby": False,
                        "instance_id": instance_id,
                        "boot_id": self.boot_id,
                        "supervisor_pid": int(self.supervisor_pid),
                        "supervisor_starttime_ticks": int(proof["supervisor_starttime_ticks"]),
                        "sealed_gpu_epoch": int(proof["sealed_gpu_epoch"]),
                        "reclaim_proof": proof,
                        "reason": reason,
                        "reason_code": "HARD_EVICTED",
                        "residency_state": "zero_vram_standby",
                        "residency_seq": self.residency_seq,
                        "gpu_epoch": self.gpu_epoch,
                        "generation": self.last_known_generation or ("0" * 32),
                        "checkpoint_asset_id": None,
                    }

            eff_deadline_ms = (
                deadline_ms
                if (type(deadline_ms) is int and 200 <= deadline_ms <= 120000)
                else self.soft_evict_deadline_ms
            )
            try:
                probe = self.state.adapter.call(
                    "scene.get",
                    {},
                    request_id=str(uuid.uuid4()),
                    generation=None,
                    scene_version=None,
                    deadline_ms=2000,
                )
                if probe.get("ok") is not True or not probe.get("generation"):
                    raise ResidencyError("WORKER_NOT_READY", 503)
                gen = probe["generation"]
                with self.lock:
                    self.last_known_generation = gen
                    if self.activity_seq != snap_activity or self.in_flight_ops > 0 or self.in_flight_batch > 0:
                        raise ResidencyError("WORKER_BUSY", 409)

                # When GUI session is open, preflight dirty unpacked images / active render / modal op
                # BEFORE stopping WebRTC so any rejection preserves the human's GUI session intact (H5)!
                if gui_was_held:
                    preflight = self.state.adapter.call(
                        "instance.hibernate",
                        {"max_checkpoints": self.max_checkpoints, "quit": False, "preflight_only": True},
                        request_id=str(uuid.uuid4()),
                        generation=gen,
                        scene_version=None,
                        deadline_ms=2000,
                    )
                    if preflight.get("ok") is not True:
                        err = preflight.get("error") or {}
                        raise ResidencyError(err.get("code", "WORKER_BUSY"), 409, err.get("data"))
                    if self.gui.input_idle_seconds() < self.gui_idle_hibernate_seconds:
                        raise ResidencyError("GUI_EDIT_LEASE_HELD", 409)
                    self.gui.run("stop")
                    self.gui.session = None

                res = self.state.adapter.call(
                    "instance.hibernate",
                    {"max_checkpoints": self.max_checkpoints, "quit": True},
                    request_id=str(uuid.uuid4()),
                    generation=gen,
                    scene_version=None,
                    deadline_ms=eff_deadline_ms,
                )
                if res.get("ok") is not True:
                    err = res.get("error") or {}
                    raise ResidencyError(err.get("code", "WORKER_BUSY"), 409, err.get("data"))

                verified_manifest = self.read_hibernate_manifest()
                if verified_manifest is None or verified_manifest["generation"] != gen:
                    try:
                        self.sentinel_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise ResidencyError("HIBERNATE_MANIFEST_INVALID", 503)

                self._reap_gpu_lineage(eff_deadline_ms / 1000.0)

                if not self.sentinel_path.is_file():
                    self.sentinel_path.parent.mkdir(parents=True, exist_ok=True)
                    self.sentinel_path.write_text(
                        json.dumps({
                            "generation": gen,
                            "scene_version": verified_manifest["scene_version"],
                            "checkpoint_asset_id": verified_manifest["checkpoint_asset_id"],
                            "sha256": verified_manifest["sha256"],
                            "size": verified_manifest["size"],
                        }),
                        encoding="utf-8",
                    )

                with self.lock:
                    grant_to_release = self.active_grant
                    reaped_epoch = self.gpu_epoch
                    self.active_grant = None
                    try:
                        proof = self._seal_grant_and_journal_release_locked(
                            grant_to_release,
                            reaped_epoch,
                            enqueue_release=bool(grant_to_release is not None),
                        )
                    except ResidencyError as p_exc:
                        self.quarantined = True
                        self.quarantine_reason = p_exc.code
                        self.vram_allocated_mib = None
                        raise
                    self.vram_allocated_mib = 0
                    self.last_idle_at = self.clock()
                if self.pending_releases:
                    self.flush_pending_releases()
            except Exception as exc:
                if isinstance(exc, ResidencyError):
                    raise
                code = getattr(exc, "code", "WORKER_BUSY")
                status = getattr(exc, "status", 409)
                raise ResidencyError(code, status) from exc
            finally:
                with self.lock:
                    self.hibernating = False
                    self._sync_state()

            with self.lock:
                proof = self.last_reclaim_proof
                if proof is None:
                    raise ResidencyError("FENCE_STATE_PERSISTENCE_FAILED", 503)
                return {
                    "ok": True,
                    "settled": True,
                    "released": True,
                    "already_standby": False,
                    "instance_id": instance_id,
                    "boot_id": self.boot_id,
                    "supervisor_pid": int(self.supervisor_pid),
                    "supervisor_starttime_ticks": int(proof["supervisor_starttime_ticks"]),
                    "sealed_gpu_epoch": int(proof["sealed_gpu_epoch"]),
                    "reclaim_proof": proof,
                    "reason": reason,
                    "reason_code": reason,
                    "residency_state": "zero_vram_standby",
                    "residency_seq": self.residency_seq,
                    "gpu_epoch": self.gpu_epoch,
                    "generation": gen,
                    "checkpoint_asset_id": verified_manifest["checkpoint_asset_id"],
                }

    def revoke_lease(self, request: dict) -> dict:
        """Bind lease revocation to exact identity/epoch and never destroy unsaved data (H6)."""
        if not isinstance(request, dict):
            raise ResidencyError("INVALID_ARGUMENT", 400)
        instance_id = getattr(self.state, "instance_id", "") or os.environ.get("INSTANCE_ID", "")
        lease_id = request.get("lease_id")
        fencing_token = request.get("fencing_token")
        if (
            not isinstance(lease_id, str)
            or not lease_id.strip()
            or type(fencing_token) is not int
            or fencing_token <= 0
        ):
            raise ResidencyError("INVALID_ARGUMENT", 400)
        lease_id = lease_id.strip()
        req_attempt = request.get("attempt_id")
        attempt_str = req_attempt.strip() if isinstance(req_attempt, str) and req_attempt.strip() else ""

        req_pod = request.get("pod_uid")
        expected_pod = os.environ.get("BLENDER_POD_UID", "")
        if isinstance(req_pod, str) and req_pod and expected_pod and req_pod != expected_pod:
            raise ResidencyError("GPU_LEASE_POD_MISMATCH", 403)
        req_cid = request.get("container_id")
        if isinstance(req_cid, str) and req_cid and req_cid != self.container_id:
            raise ResidencyError("GPU_LEASE_CONTAINER_MISMATCH", 403)
        req_boot = request.get("boot_id")
        if isinstance(req_boot, str) and req_boot and req_boot != self.boot_id:
            raise ResidencyError("GPU_LEASE_BOOT_MISMATCH", 409)

        revoke_reason = str(request.get("reason") or request.get("hard_reason") or "LEASE_REVOKED")
        is_hard_revoke = bool(request.get("force_hard", False)) or revoke_reason.upper() in {"ADMIN_FORCE_DRAIN", "GPU_FAULT_XID"}

        # Record revocation immediately under leaf state lock so any concurrent waking loop
        # or pre_cuda_check aborts without waiting for lifecycle_lock!
        with self.lock:
            if self.fence_state_corrupted:
                raise ResidencyError("FENCE_STATE_CORRUPTED", 503)
            if not is_hard_revoke and self.quarantined and self.quarantine_reason != "FENCE_STATE_PERSISTENCE_FAILED":
                self._guard_not_quarantined()
            self.revoked_leases.add(lease_id)
            if attempt_str:
                self.revoked_attempts.add(attempt_str)
            if fencing_token > self.min_fencing_token:
                self.min_fencing_token = fencing_token
            if fencing_token > self.consumed_fencing_token:
                self.consumed_fencing_token = fencing_token
            try:
                self._persist_fence_state_locked()
            except ResidencyError:
                pass

        with self.gui.lock, self.lifecycle_lock:
            early_resp: dict | None = None
            with self.lock:
                if (
                    self.quarantined
                    and self.quarantine_reason == "FENCE_STATE_PERSISTENCE_FAILED"
                    and not self.fence_state_corrupted
                    and self.lineage is None
                    and self.sentinel_path.is_file()
                ):
                    self._seal_grant_and_journal_release_locked(
                        None,
                        self._prior_or_current_sealed_epoch_locked(lease_id),
                        explicit_lease_id=lease_id,
                        explicit_attempt_id=attempt_str,
                        explicit_token=fencing_token,
                        enqueue_release=False,
                    )
                    self.quarantined = False
                    self.quarantine_reason = "none"
                    self.vram_allocated_mib = 0
                if not is_hard_revoke and self.quarantined:
                    self._guard_not_quarantined()
                self.gui.expire()
                req_epoch = request.get("expected_gpu_epoch")
                if (
                    req_epoch is not None
                    and type(req_epoch) is int
                    and req_epoch < self.gpu_epoch
                ):
                    state_name, _, _, _ = self._sync_state()
                    proof = self._seal_grant_and_journal_release_locked(
                        None,
                        self._prior_or_current_sealed_epoch_locked(lease_id),
                        explicit_lease_id=lease_id,
                        explicit_attempt_id=attempt_str,
                        explicit_token=fencing_token,
                        enqueue_release=False,
                    )
                    early_resp = {
                        "ok": True,
                        "revoked": True,
                        "settled": True,
                        "reclaimed": True,
                        "already_superseded": True,
                        "instance_id": instance_id,
                        "boot_id": self.boot_id,
                        "supervisor_pid": int(self.supervisor_pid),
                        "supervisor_starttime_ticks": int(proof["supervisor_starttime_ticks"]),
                        "sealed_gpu_epoch": int(proof["sealed_gpu_epoch"]),
                        "reclaim_proof": proof,
                        "lease_id": lease_id,
                        "fencing_token": fencing_token,
                        "residency_state": state_name,
                        "residency_seq": self.residency_seq,
                        "gpu_epoch": self.gpu_epoch,
                        "min_fencing_token": self.min_fencing_token,
                    }
                elif (
                    self.active_grant is not None
                    and self.active_grant["lease_id"] != lease_id
                    and self.active_grant["fencing_token"] > fencing_token
                ):
                    # Stale revocation for a superseded lease must fence the old token WITHOUT killing a newer epoch!
                    state_name, _, _, _ = self._sync_state()
                    proof = self._seal_grant_and_journal_release_locked(
                        None,
                        self._prior_or_current_sealed_epoch_locked(lease_id),
                        explicit_lease_id=lease_id,
                        explicit_attempt_id=attempt_str,
                        explicit_token=fencing_token,
                        enqueue_release=False,
                    )
                    early_resp = {
                        "ok": True,
                        "revoked": True,
                        "settled": True,
                        "reclaimed": True,
                        "already_superseded": True,
                        "instance_id": instance_id,
                        "boot_id": self.boot_id,
                        "supervisor_pid": int(self.supervisor_pid),
                        "supervisor_starttime_ticks": int(proof["supervisor_starttime_ticks"]),
                        "sealed_gpu_epoch": int(proof["sealed_gpu_epoch"]),
                        "reclaim_proof": proof,
                        "lease_id": lease_id,
                        "fencing_token": fencing_token,
                        "residency_state": state_name,
                        "residency_seq": self.residency_seq,
                        "gpu_epoch": self.gpu_epoch,
                        "min_fencing_token": self.min_fencing_token,
                    }
                elif self.is_standby():
                    existing_grant = (
                        self.active_grant
                        if (self.active_grant is not None and self.active_grant["lease_id"] == lease_id)
                        else None
                    )
                    probe = self.standby_probe()
                    if probe is None:
                        raise ResidencyError("HIBERNATE_MANIFEST_INVALID", 503)
                    proof = self._seal_grant_and_journal_release_locked(
                        existing_grant,
                        self._prior_or_current_sealed_epoch_locked(lease_id),
                        explicit_lease_id=lease_id,
                        explicit_attempt_id=attempt_str,
                        explicit_token=fencing_token,
                        enqueue_release=False,
                    )
                    if existing_grant is not None:
                        self.active_grant = None
                    early_resp = {
                        "ok": True,
                        "revoked": True,
                        "settled": True,
                        "reclaimed": True,
                        "instance_id": instance_id,
                        "boot_id": self.boot_id,
                        "supervisor_pid": int(self.supervisor_pid),
                        "supervisor_starttime_ticks": int(proof["supervisor_starttime_ticks"]),
                        "sealed_gpu_epoch": int(proof["sealed_gpu_epoch"]),
                        "reclaim_proof": proof,
                        "lease_id": lease_id,
                        "fencing_token": fencing_token,
                        "residency_state": "zero_vram_standby",
                        "residency_seq": self.residency_seq,
                        "gpu_epoch": self.gpu_epoch,
                        "min_fencing_token": self.min_fencing_token,
                    }

            if early_resp is not None:
                if self.pending_releases:
                    self.flush_pending_releases()
                return early_resp

            # Hibernate via safe copy=True checkpoint unless hard revocation (ADMIN_FORCE_DRAIN / GPU_FAULT_XID).
            hib = self.hibernate(
                reason=revoke_reason,
                force_gui_idle=bool(request.get("force_gui_idle", False)),
                force_hard=is_hard_revoke,
                colocated_heavy_queued=request.get("colocated_heavy_queued"),
            )
            if self.pending_releases:
                self.flush_pending_releases()
            with self.lock:
                try:
                    proof = self._seal_grant_and_journal_release_locked(
                        None,
                        self._prior_or_current_sealed_epoch_locked(lease_id),
                        explicit_lease_id=lease_id,
                        explicit_attempt_id=attempt_str,
                        explicit_token=fencing_token,
                        enqueue_release=False,
                    )
                except ResidencyError as p_exc:
                    self.quarantined = True
                    self.quarantine_reason = p_exc.code
                    self.vram_allocated_mib = None
                    raise
                return {
                    "ok": True,
                    "revoked": True,
                    "settled": True,
                    "reclaimed": True,
                    "instance_id": instance_id,
                    "boot_id": self.boot_id,
                    "supervisor_pid": int(self.supervisor_pid),
                    "supervisor_starttime_ticks": int(proof["supervisor_starttime_ticks"]),
                    "sealed_gpu_epoch": int(proof["sealed_gpu_epoch"]),
                    "reclaim_proof": proof,
                    "lease_id": lease_id,
                    "fencing_token": fencing_token,
                    "residency_state": hib["residency_state"],
                    "residency_seq": self.residency_seq,
                    "gpu_epoch": self.gpu_epoch,
                    "min_fencing_token": self.min_fencing_token,
                    "checkpoint_asset_id": hib.get("checkpoint_asset_id"),
                }

    def idle_watchdog_step(self) -> dict | None:
        if self.pending_releases:
            self.flush_pending_releases()
        with self.gui.lock:
            with self.lock:
                if self.quarantined:
                    return None
                self.gui.expire()
                if self.is_standby() or self.hibernating or self.waking:
                    return None
                gui_open = self.gui.session is not None
                can_gui_hib = (
                    gui_open
                    and self.colocated_heavy_queued
                    and self.gui.input_idle_seconds() >= self.gui_idle_hibernate_seconds
                )
                can_warm_hib = (
                    not gui_open
                    and self.in_flight_ops == 0
                    and self.in_flight_batch == 0
                    and (self.clock() - self.last_idle_at) >= self.hibernate_idle_seconds
                )
            if can_gui_hib:
                try:
                    return self.hibernate(reason="GUI_IDLE_CONTENTION_HIBERNATE", force_gui_idle=True)
                except ResidencyError:
                    return None
            if can_warm_hib:
                try:
                    return self.hibernate(reason="IDLE_WARM_TTL")
                except ResidencyError:
                    return None
            return None

    def mark_evict_callback_handled(self) -> None:
        with self.lock:
            self._last_reported_key = (self.boot_id, int(self.gpu_epoch), int(self.residency_seq))

    def _settle_warm_grant_locked(self, grant: dict) -> bool:
        if self.fence_state_corrupted:
            return False
        lease_id = str(grant.get("lease_id") or "").strip()
        attempt_id = str(grant.get("attempt_id") or "").strip()
        token_val = int(grant.get("fencing_token") or 0)
        if not lease_id or token_val <= 0:
            return False
        pod_uid = os.environ.get("BLENDER_POD_UID", "").strip()
        if not pod_uid:
            return False
        sup_ticks = int(self._effective_supervisor_ticks())
        eff_epoch = max(1, int(self.gpu_epoch))
        next_min = max(self.min_fencing_token, token_val)
        next_consumed = max(self.consumed_fencing_token, token_val)
        next_rev_leases = set(self.revoked_leases) | {lease_id}
        next_rev_attempts = set(self.revoked_attempts) | ({attempt_id} if attempt_id else set())
        next_inst_leases = set(self.installed_leases) - {lease_id}
        next_inst_attempts = set(self.installed_attempts) - ({attempt_id} if attempt_id else set())
        next_inst_max = 0 if (not next_inst_leases and not next_inst_attempts) else self.installed_max_fencing_token
        resident_procs = _list_container_resident_procs(self.supervisor_pid, proc_root=self.proc_root)
        warm_proof: dict = {
            "proof_source": "receiver_guard",
            "token_sealed": True,
            "reclaimed": True,
            "active_tasks": 0,
            "supervisor_pid": int(self.supervisor_pid),
            "supervisor_starttime_ticks": sup_ticks,
            "sealed_pod_uid": pod_uid,
            "sealed_container_id": self.container_id,
            "sealed_boot_id": self.boot_id,
            "sealed_gpu_epoch": eff_epoch,
            "revoked_lease_id": lease_id,
            "consumed_fencing_token": int(next_consumed),
            "revoked_fencing_token": int(max(token_val, next_consumed)),
            "resident_procs": resident_procs,
        }
        if attempt_id:
            warm_proof["revoked_attempt_id"] = attempt_id
        next_pending = dict(self.pending_releases)
        next_pending[lease_id] = {
            "idempotency_key": f"blender-warm-settle:{lease_id}:epoch:{eff_epoch}",
            "fencing_token": int(token_val),
            "worker_settled": True,
            "pod_uid": pod_uid,
            "container_id": self.container_id,
            "boot_id": self.boot_id,
            "gpu_epoch": eff_epoch,
            "supervisor_pid": int(self.supervisor_pid),
            "supervisor_starttime_ticks": sup_ticks,
            "reclaim_mode": "warm_idle_settled",
            "reclaim_proof": warm_proof,
        }
        next_sealed = dict(self.sealed_proofs_by_lease)
        next_sealed[lease_id] = warm_proof
        self._persist_fence_state_locked(
            candidate_overrides={
                "min_fencing_token": next_min,
                "consumed_fencing_token": next_consumed,
                "installed_max_fencing_token": next_inst_max,
                "revoked_leases": next_rev_leases,
                "revoked_attempts": next_rev_attempts,
                "installed_leases": next_inst_leases,
                "installed_attempts": next_inst_attempts,
                "pending_releases": next_pending,
                "sealed_proofs_by_lease": next_sealed,
                "last_reclaim_proof": warm_proof,
            }
        )
        self.min_fencing_token = next_min
        self.consumed_fencing_token = next_consumed
        self.installed_max_fencing_token = next_inst_max
        self.revoked_leases = next_rev_leases
        self.revoked_attempts = next_rev_attempts
        self.installed_leases = next_inst_leases
        self.installed_attempts = next_inst_attempts
        self.pending_releases = next_pending
        self.last_reclaim_proof = warm_proof
        self.active_grant = None
        self._warm_grant_settled = True
        return True

    def residency_sync_step(self) -> None:
        if not self.residency_sync_enabled:
            return
        scheduler_url = (
            os.environ.get("STATION_GPU_SCHEDULER_URL")
            or os.environ.get("STATION_RUNTIME_SCHEDULER_URL")
            or ""
        ).rstrip("/")
        if not scheduler_url:
            return
        sched_token = (
            os.environ.get("STATION_GPU_SCHEDULER_TOKEN")
            or os.environ.get("STATION_RUNTIME_SCHEDULER_TOKEN")
            or ""
        )
        headers = {"Content-Type": "application/json"}
        if sched_token:
            headers["Authorization"] = f"Bearer {sched_token}"
        no_proxy_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        report_body: dict | None = None
        report_key: tuple[str, int, int] | None = None
        hb_target: tuple[str, dict] | None = None
        pod_uid = os.environ.get("BLENDER_POD_UID", "").strip()
        node_uid = (os.environ.get("BLENDER_NODE_UID") or os.environ.get("NODE_UID") or "").strip()

        with self.lock:
            state_name, reason, _, human_protected = self._sync_state()
            gui_alive = self.gui.session is not None
            instance_id = getattr(self.state, "instance_id", "") or os.environ.get("INSTANCE_ID", "")
            gpu_uuid = (
                (self.active_grant or {}).get("gpu_uuid")
                or self._last_grant_gpu_uuid
                or _resolve_visible_gpu_uuid(allow_host_probe=self._allow_host_gpu_probe)
            )
            if isinstance(self.active_grant, dict) and self.active_grant.get("node_uid") and not node_uid:
                node_uid = str(self.active_grant["node_uid"]).strip()
            in_transition = self.hibernating or self.hard_draining
            cur_key = (self.boot_id, int(self.gpu_epoch), int(self.residency_seq))
            if (
                not in_transition
                and instance_id
                and pod_uid
                and gpu_uuid
                and cur_key != self._last_reported_key
            ):
                if state_name in {"active_compute", "waking", "draining_hard", "quarantined"}:
                    idle_sec = 0
                elif state_name == "human_dirty":
                    idle_sec = self.gui.input_idle_seconds()
                else:
                    idle_sec = max(0, int(self.clock() - self.last_idle_at))
                vram = 0 if state_name == "zero_vram_standby" else (None if state_name == "quarantined" else self.vram_allocated_mib)
                port = os.environ.get("BLENDER_MCP_PORT", "48084")
                report_key = cur_key
                report_body = {
                    "instance_id": instance_id,
                    "node_uid": node_uid,
                    "pod_uid": pod_uid,
                    "container_name": os.environ.get("BLENDER_CONTAINER_NAME", "worker"),
                    "container_id": self.container_id,
                    "boot_id": self.boot_id,
                    "gpu_epoch": int(self.gpu_epoch),
                    "residency_seq": int(self.residency_seq),
                    "gpu_uuid": gpu_uuid,
                    "residency_state": state_name,
                    "human_protected": bool(human_protected),
                    "gui_connection_alive": bool(gui_alive),
                    "dirty": bool(human_protected),
                    "snapshot_in_progress": False,
                    "snapshot_required": bool(state_name != "zero_vram_standby"),
                    "blocking_reason": reason,
                    "pinned_budget_mib": 6144 if human_protected else 0,
                    "vram_allocated_mib": vram,
                    "idle_seconds": int(idle_sec),
                    "callback_endpoint": f"http://127.0.0.1:{port}",
                    "template_upgraded": True,
                }
            if (
                isinstance(self.active_grant, dict)
                and (self.waking or self.in_flight_ops > 0 or self.in_flight_batch > 0)
                and (self.clock() - self._last_heartbeat_mono) >= 15.0
            ):
                lid = str(self.active_grant.get("lease_id") or "").strip()
                ftok = int(self.active_grant.get("fencing_token") or 0)
                if lid and ftok > 0:
                    hb_target = (
                        lid,
                        {
                            "idempotency_key": f"blender-hb:{lid}:{int(self.wall_clock())}",
                            "fencing_token": ftok,
                            "pod_uid": pod_uid,
                            "container_id": self.container_id,
                            "boot_id": self.boot_id,
                        },
                    )

        if report_body is not None and report_key is not None:
            req = urllib.request.Request(
                f"{scheduler_url}/internal/v1/gpu-scheduler/residency/report",
                data=json.dumps(report_body, separators=(",", ":")).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                with no_proxy_opener.open(req, timeout=5) as resp:
                    if getattr(resp, "status", 200) in (200, 204):
                        with self.lock:
                            self._last_reported_key = report_key
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    with self.lock:
                        self._last_reported_key = report_key
            except OSError:
                pass

        if hb_target is not None:
            lid, hb_body = hb_target
            req = urllib.request.Request(
                f"{scheduler_url}/internal/v1/gpu-scheduler/leases/{lid}/heartbeat",
                data=json.dumps(hb_body, separators=(",", ":")).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                with no_proxy_opener.open(req, timeout=5) as resp:
                    raw = resp.read(65536)
                    data = json.loads(raw.decode("utf-8")) if raw else None
                    with self.lock:
                        self._last_heartbeat_mono = self.clock()
                        if (
                            isinstance(data, dict)
                            and data.get("expires_at")
                            and isinstance(self.active_grant, dict)
                            and self.active_grant.get("lease_id") == lid
                        ):
                            self.active_grant["expires_at"] = data["expires_at"]
            except OSError:
                pass

        need_flush = False
        with self.lock:
            state_name, _, _, _ = self._sync_state()
            cur_key = (self.boot_id, int(self.gpu_epoch), int(self.residency_seq))
            if (
                state_name in {"idle_evictable", "human_dirty"}
                and not self.waking
                and not self.hibernating
                and not self.hard_draining
                and self.in_flight_ops == 0
                and self.in_flight_batch == 0
                and isinstance(self.active_grant, dict)
                and self._last_reported_key == cur_key
            ):
                try:
                    need_flush = self._settle_warm_grant_locked(self.active_grant)
                except ResidencyError:
                    need_flush = False
        if need_flush:
            self.flush_pending_releases()

    def watchdog(self, stop_event: threading.Event | None = None) -> None:
        while stop_event is None or not stop_event.is_set():
            self._sync_wakeup.wait(timeout=1.0)
            self._sync_wakeup.clear()
            try:
                self.idle_watchdog_step()
                if self.residency_sync_enabled:
                    self.residency_sync_step()
            except Exception:
                pass
