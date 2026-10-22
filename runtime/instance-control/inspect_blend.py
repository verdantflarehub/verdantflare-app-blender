"""Reopen a workspace .blend file in a headless child process and inspect its structure."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any


BLEND_ASSET_PATTERN = re.compile(r"(?:project|exports)/[A-Za-z0-9._/-]{1,180}\.blend\Z")


class InspectBlendError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def inspect_file(asset_id: str) -> dict[str, Any]:
    if not isinstance(asset_id, str) or not BLEND_ASSET_PATTERN.fullmatch(asset_id) or ".." in Path(asset_id).parts:
        raise InspectBlendError("INVALID_ARGUMENT")
    root = Path(os.environ.get("WORKSPACE_ROOT", "/workspace")).resolve()
    current = root
    for part in Path(asset_id).parts:
        current = current / part
        if current.is_symlink():
            raise InspectBlendError("INVALID_ARGUMENT")
    source = current.resolve(strict=False)
    if source != root and root not in source.parents:
        raise InspectBlendError("INVALID_ARGUMENT")
    if not source.is_file():
        raise InspectBlendError("ASSET_NOT_FOUND")

    size = source.stat().st_size
    with source.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()

    blender_bin = os.environ.get("BLENDER_EXECUTABLE")
    if not blender_bin:
        blender_bin = "/opt/blender/blender" if Path("/opt/blender/blender").is_file() else "/usr/local/bin/blender"
    inspector_script = Path(__file__).parents[1] / "scene_inspect.py"

    try:
        with tempfile.TemporaryDirectory(prefix="blender-inspect-") as folder:
            out_json = Path(folder) / "inspect.json"
            process = subprocess.run(
                [
                    blender_bin,
                    "--background",
                    "--disable-autoexec",
                    "--factory-startup",
                    "--python-exit-code",
                    "1",
                    "--python",
                    str(inspector_script),
                    "--",
                    str(source),
                    str(out_json),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
            if process.returncode != 0 or not out_json.is_file():
                raise InspectBlendError("PROJECT_FILE_INVALID")
            report = json.loads(out_json.read_text(encoding="utf-8"))
    except InspectBlendError:
        raise
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise InspectBlendError("PROJECT_PREFLIGHT_UNAVAILABLE") from None

    report["asset_id"] = asset_id
    report["size"] = size
    report["sha256"] = sha
    return report
