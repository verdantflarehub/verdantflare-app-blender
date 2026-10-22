"""Bounded headless Blender batch execution for Studio Project scripts."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any


SCRIPT_ASSET_PATTERN = re.compile(r"inbox/[A-Za-z0-9_-]{12,24}/[A-Za-z0-9._-]{1,64}\.py\Z")
SUBDIR_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
BLEND_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.blend\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
MAX_SCRIPT_BYTES = 4 * 1024 * 1024


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
        or (load_blend is not None and (not isinstance(load_blend, str) or not BLEND_NAME_PATTERN.fullmatch(load_blend)))
    ):
        raise BatchRunError("INVALID_ARGUMENT")

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
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "build.log"

    blender_bin = os.environ.get("BLENDER_EXECUTABLE")
    if not blender_bin:
        blender_bin = "/usr/local/bin/blender" if Path("/usr/local/bin/blender").is_file() else "/opt/blender/blender"
    timeout_seconds = int(os.environ.get("BLENDER_BATCH_TIMEOUT_SECONDS", "540"))

    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", "/run/user/1000")
    env.setdefault("WAYLAND_DISPLAY", "wayland-0")

    try:
        with log_path.open("wb") as log_stream:
            process = subprocess.run(
                [blender_bin, "-b", "--factory-startup", "--python-exit-code", "1", "-P", str(script_path)],
                cwd=str(output_dir),
                 env=env,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                timeout=timeout_seconds,
            )
            log_stream.flush()
            os.fsync(log_stream.fileno())
    except subprocess.TimeoutExpired:
        raise BatchRunError("OPERATION_TIMEOUT") from None
    except OSError:
        raise BatchRunError("BATCH_EXECUTION_UNAVAILABLE") from None

    log_bytes = log_path.read_bytes() if log_path.is_file() else b""
    log_tail = log_bytes[-4096:].decode("utf-8", errors="replace")
    files = collect_output_files(root, output_subdir)

    if process.returncode != 0:
        raise BatchRunError(
            "BATCH_SCRIPT_FAILED",
            data={"exit_code": process.returncode, "log_tail": log_tail, "files": files},
        )

    result: dict[str, Any] = {
        "output_subdir": output_subdir,
        "exit_code": process.returncode,
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
