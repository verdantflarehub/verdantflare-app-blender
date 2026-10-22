"""Workspace-only file helpers shared by the Blender MCP file agent."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from pathlib import Path


SUBDIR_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


class WorkspaceError(ValueError):
    pass


def workspace_root(value: str | os.PathLike[str]) -> Path:
    root = Path(value).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def asset_path(root: Path, asset_id: str, *, must_exist: bool = True) -> Path:
    """Resolve an asset ID without following a symlink out of ``root``."""
    if not asset_id or "\x00" in asset_id:
        raise WorkspaceError("asset_id is invalid")
    relative = Path(asset_id)
    if relative.is_absolute() or ".." in relative.parts:
        raise WorkspaceError("asset_id must be relative to the workspace")
    unresolved = root / relative
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise WorkspaceError("symlink assets are not allowed")
    candidate = unresolved.resolve(strict=False)
    if candidate != root and root not in candidate.parents:
        raise WorkspaceError("asset_id escapes the workspace")
    if must_exist and not candidate.is_file():
        raise FileNotFoundError(asset_id)
    return candidate


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_filename(name: str) -> str:
    value = Path(name).name
    if value in {"", ".", ".."} or value != name or "\x00" in value:
        raise WorkspaceError("filename is invalid")
    return value


def _ensure_no_symlinks_tree(target: Path) -> None:
    if target.is_symlink():
        raise WorkspaceError("symlink paths are not allowed")
    if target.is_dir():
        for dirpath, dirnames, filenames in os.walk(target, followlinks=False):
            base = Path(dirpath)
            for entry in [*dirnames, *filenames]:
                if (base / entry).is_symlink():
                    raise WorkspaceError("symlink paths are not allowed")


def clean_workspace(
    root: Path,
    *,
    remove_stray_home_c_drive: bool = True,
    export_subdirs: list[str] | None = None,
    home_dir: Path | None = None,
) -> dict[str, object]:
    """Bounded cleanup of stray $HOME/C: and specified /workspace/exports/<subdir>."""
    if type(remove_stray_home_c_drive) is not bool:
        raise WorkspaceError("remove_stray_home_c_drive must be a boolean")
    if export_subdirs is None:
        export_subdirs = []
    if not isinstance(export_subdirs, list) or len(export_subdirs) > 32:
        raise WorkspaceError("export_subdirs must be a bounded list")
    for subdir in export_subdirs:
        if not isinstance(subdir, str) or not SUBDIR_PATTERN.fullmatch(subdir):
            raise WorkspaceError("invalid export_subdir")

    root = root.resolve()
    exports_root = root / "exports"
    exports_root.mkdir(parents=True, exist_ok=True)
    home_root = (home_dir or Path(os.environ.get("BLENDER_HOME_DIR", os.environ.get("HOME", "/home/beagle")))).resolve()

    cleaned: list[str] = []
    initial_home_entries = sorted(os.listdir(home_root)) if home_root.is_dir() else []
    stray_exists = "C:" in initial_home_entries

    if remove_stray_home_c_drive and stray_exists:
        stray_path = home_root / "C:"
        _ensure_no_symlinks_tree(stray_path)
        resolved_stray = stray_path.resolve(strict=False)
        if home_root not in resolved_stray.parents:
            raise WorkspaceError("stray path escapes home")
        if stray_path.is_dir():
            shutil.rmtree(stray_path)
        else:
            stray_path.unlink(missing_ok=True)
        cleaned.append(str(stray_path).replace("\\", "/"))

    for subdir in export_subdirs:
        target = asset_path(root, f"exports/{subdir}", must_exist=False)
        if target.exists() or target.is_symlink():
            _ensure_no_symlinks_tree(target)
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
            cleaned.append(f"exports/{subdir}")

    home_entries = sorted(os.listdir(home_root)) if home_root.is_dir() else []
    exports_entries = sorted(os.listdir(exports_root)) if exports_root.is_dir() else []
    return {
        "cleaned_paths": cleaned,
        "home_stray_c_exists": "C:" in home_entries,
        "home_entries": home_entries,
        "exports_entries": exports_entries,
    }
