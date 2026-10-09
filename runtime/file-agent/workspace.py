"""Workspace-only file helpers shared by the Blender MCP file agent."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


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
