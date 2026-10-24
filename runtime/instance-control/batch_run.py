"""Bounded headless Blender batch execution for Studio Project scripts."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
from typing import Any


SCRIPT_ASSET_PATTERN = re.compile(r"inbox/[A-Za-z0-9_-]{12,24}/[A-Za-z0-9._-]{1,64}\.py\Z")
SUBDIR_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
BLEND_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.blend\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
MAX_SCRIPT_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 300
MIN_TIMEOUT_SECONDS = 10
MAX_TIMEOUT_SECONDS = 600
MAX_FILES_PER_SUBDIR = 64
MAX_BYTES_PER_SUBDIR = 2 * 1024 * 1024 * 1024
MAX_EXPORT_SUBDIRS = 16
MAX_BYTES_EXPORTS_TOTAL = 10 * 1024 * 1024 * 1024
BATCH_LOCK = threading.Lock()


class BatchRunError(Exception):
    def __init__(self, code: str, data: dict[str, Any] | None = None):
        super().__init__(code)
        self.code = code
        self.data = data


def _check_no_symlinks(root: Path, relative: str) -> Path:
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise BatchRunError("INVALID_ARGUMENT")
    resolved = current.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise BatchRunError("INVALID_ARGUMENT")
    return resolved


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _dir_stats(directory: Path) -> tuple[int, int]:
    file_count = 0
    total_bytes = 0
    if not directory.is_dir():
        return 0, 0
    for dirpath, _, filenames in os.walk(directory, followlinks=False):
        base = Path(dirpath)
        for name in filenames:
            candidate = base / name
            if candidate.is_symlink() or not candidate.is_file():
                continue
            file_count += 1
            total_bytes += candidate.stat().st_size
    return file_count, total_bytes


def _check_exports_quota(root: Path, output_subdir: str, *, pre_run: bool) -> None:
    exports_root = _check_no_symlinks(root, "exports")
    exports_root.mkdir(parents=True, exist_ok=True)
    max_files = int(os.environ.get("BLENDER_EXPORT_MAX_FILES_PER_SUBDIR", str(MAX_FILES_PER_SUBDIR)))
    max_subdir_bytes = int(os.environ.get("BLENDER_EXPORT_MAX_BYTES_PER_SUBDIR", str(MAX_BYTES_PER_SUBDIR)))
    max_subdirs = int(os.environ.get("BLENDER_EXPORT_MAX_SUBDIRS", str(MAX_EXPORT_SUBDIRS)))
    max_total_bytes = int(os.environ.get("BLENDER_EXPORT_MAX_BYTES_TOTAL", str(MAX_BYTES_EXPORTS_TOTAL)))

    subdirs = [entry for entry in exports_root.iterdir() if entry.is_dir() and not entry.is_symlink()]
    if pre_run:
        other_subdirs = [entry for entry in subdirs if entry.name != output_subdir]
        if len(other_subdirs) >= max_subdirs:
            raise BatchRunError(
                "EXPORT_QUOTA_EXCEEDED",
                data={"reason": "max_export_subdirs_exceeded", "subdir_count": len(other_subdirs), "max_subdirs": max_subdirs},
            )
        other_bytes = sum(_dir_stats(entry)[1] for entry in other_subdirs)
        if other_bytes >= max_total_bytes:
            raise BatchRunError(
                "EXPORT_QUOTA_EXCEEDED",
                data={"reason": "max_total_export_bytes_exceeded", "total_bytes": other_bytes, "max_total_bytes": max_total_bytes},
            )
        return

    output_dir = exports_root / output_subdir
    file_count, subdir_bytes = _dir_stats(output_dir)
    total_bytes = sum(_dir_stats(entry)[1] for entry in subdirs)
    if file_count > max_files or subdir_bytes > max_subdir_bytes or len(subdirs) > max_subdirs or total_bytes > max_total_bytes:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise BatchRunError(
            "EXPORT_QUOTA_EXCEEDED",
            data={
                "file_count": file_count,
                "max_files_per_subdir": max_files,
                "subdir_bytes": subdir_bytes,
                "max_bytes_per_subdir": max_subdir_bytes,
                "total_bytes": total_bytes,
                "max_total_bytes": max_total_bytes,
            },
        )


def collect_output_files(root: Path, output_subdir: str) -> list[dict[str, Any]]:
    output_dir = _check_no_symlinks(root, f"exports/{output_subdir}")
    if not output_dir.is_dir():
        return []
    files: list[dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(output_dir, followlinks=False):
        dirnames.sort()
        filenames.sort()
        base = Path(dirpath)
        for name in filenames:
            candidate = base / name
            if candidate.is_symlink() or not candidate.is_file():
                continue
            rel = candidate.relative_to(output_dir).as_posix()
            asset_id = candidate.relative_to(root).as_posix()
            files.append({
                "asset_id": asset_id,
                "name": rel,
                "size": candidate.stat().st_size,
                "sha256": _sha256(candidate),
            })
    return files


def run(request: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise BatchRunError("INVALID_ARGUMENT")
    script_asset = request.get("script_asset_id")
    script_sha = request.get("script_sha256")
    script_size = request.get("script_size")
    output_subdir = request.get("output_subdir")
    clean_output = request.get("clean_output_subdir", False)
    load_blend = request.get("load_blend_as_active")
    default_timeout = int(os.environ.get("BLENDER_BATCH_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)))
    min_timeout = int(os.environ.get("BLENDER_BATCH_MIN_TIMEOUT_SECONDS", str(MIN_TIMEOUT_SECONDS)))
    timeout_seconds = request.get("timeout_seconds", default_timeout)

    if (
        not isinstance(script_asset, str)
        or not SCRIPT_ASSET_PATTERN.fullmatch(script_asset)
        or not isinstance(script_sha, str)
        or not SHA256_PATTERN.fullmatch(script_sha)
        or type(script_size) is not int
        or not 0 < script_size <= MAX_SCRIPT_BYTES
        or not isinstance(output_subdir, str)
        or not SUBDIR_PATTERN.fullmatch(output_subdir)
        or type(clean_output) is not bool
        or type(timeout_seconds) is not int
        or not min_timeout <= timeout_seconds <= MAX_TIMEOUT_SECONDS
        or (load_blend is not None and (not isinstance(load_blend, str) or not BLEND_NAME_PATTERN.fullmatch(load_blend)))
    ):
        raise BatchRunError("INVALID_ARGUMENT")

    if not BATCH_LOCK.acquire(blocking=False):
        raise BatchRunError("WORKER_BUSY")
    try:
        root = Path(os.environ.get("WORKSPACE_ROOT", "/workspace")).resolve()
        script_path = _check_no_symlinks(root, script_asset)
        if not script_path.is_file():
            raise BatchRunError("ASSET_NOT_FOUND")
        if script_path.stat().st_size != script_size or _sha256(script_path) != script_sha:
            raise BatchRunError("INVALID_ARGUMENT")

        output_dir = _check_no_symlinks(root, f"exports/{output_subdir}")
        if clean_output and output_dir.exists():
            for dirpath, dirnames, filenames in os.walk(output_dir, followlinks=False):
                base = Path(dirpath)
                for entry in [*dirnames, *filenames]:
                    if (base / entry).is_symlink():
                        raise BatchRunError("INVALID_ARGUMENT")
            shutil.rmtree(output_dir)
        _check_exports_quota(root, output_subdir, pre_run=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        log_path = output_dir / "build.log"

        blender_bin = os.environ.get("BLENDER_EXECUTABLE")
        if not blender_bin:
            blender_bin = "/usr/local/bin/blender" if Path("/usr/local/bin/blender").is_file() else "/opt/blender/blender"

        env = dict(os.environ)
        env.setdefault("XDG_RUNTIME_DIR", "/run/user/1000")
        env.setdefault("WAYLAND_DISPLAY", "wayland-0")

        try:
            with log_path.open("wb") as log_stream:
                popen_kwargs: dict[str, Any] = {
                    "cwd": str(output_dir),
                    "env": env,
                    "stdout": log_stream,
                    "stderr": subprocess.STDOUT,
                }
                if os.name == "posix":
                    popen_kwargs["start_new_session"] = True
                proc = subprocess.Popen(
                    [blender_bin, "-b", "--factory-startup", "--python-exit-code", "1", "-P", str(script_path)],
                    **popen_kwargs,
                )
                try:
                    returncode = proc.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    if os.name == "posix":
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    log_stream.flush()
                    os.fsync(log_stream.fileno())
                    log_bytes = log_path.read_bytes() if log_path.is_file() else b""
                    log_tail = log_bytes[-4096:].decode("utf-8", errors="replace")
                    raise BatchRunError(
                        "BATCH_SCRIPT_TIMEOUT",
                        data={"timeout_seconds": timeout_seconds, "log_tail": log_tail},
                    ) from None
                log_stream.flush()
                os.fsync(log_stream.fileno())
        except BatchRunError:
            raise
        except OSError:
            raise BatchRunError("BATCH_EXECUTION_UNAVAILABLE") from None

        _check_exports_quota(root, output_subdir, pre_run=False)
        log_bytes = log_path.read_bytes() if log_path.is_file() else b""
        log_tail = log_bytes[-4096:].decode("utf-8", errors="replace")
        files = collect_output_files(root, output_subdir)

        if returncode != 0:
            raise BatchRunError(
                "BATCH_SCRIPT_FAILED",
                data={"exit_code": returncode, "log_tail": log_tail, "files": files},
            )

        result: dict[str, Any] = {
            "output_subdir": output_subdir,
            "exit_code": returncode,
            "timeout_seconds": timeout_seconds,
            "log_asset_id": f"exports/{output_subdir}/build.log",
            "log_tail": log_tail,
            "files": files,
        }
        if load_blend is not None:
            blend_path = _check_no_symlinks(root, f"exports/{output_subdir}/{load_blend}")
            if not blend_path.is_file() or blend_path.stat().st_size == 0:
                raise BatchRunError("BATCH_OUTPUT_BLEND_MISSING", data=result)
            result["load_blend_asset_id"] = f"exports/{output_subdir}/{load_blend}"
        return result
    finally:
        BATCH_LOCK.release()
