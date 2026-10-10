"""Check immutable input bytes without loading them into the live scene."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


class ProjectCheckError(Exception):
    pass


def check(request):
    asset = request["asset_id"]
    if not re.fullmatch(r"inbox/[A-Za-z0-9_-]{16}/restore\.blend", asset):
        raise ProjectCheckError("INVALID_ARGUMENT")
    root = Path(os.environ.get("WORKSPACE_ROOT", "/workspace")).resolve()
    source = root / asset
    current = root
    for part in Path(asset).parts:
        current = current / part
        if current.is_symlink():
            raise ProjectCheckError("INVALID_ARGUMENT")
    if not source.resolve().is_relative_to(root) or not source.is_file():
        raise ProjectCheckError("INVALID_ARGUMENT")
    try:
        with source.open("rb") as stream:
            if source.stat().st_size != request["size"] or hashlib.file_digest(stream, "sha256").hexdigest() != request["sha256"]:
                raise ProjectCheckError("INVALID_ARGUMENT")
        with tempfile.TemporaryDirectory(prefix="blender-project-check-") as folder:
            result = Path(folder) / "result.json"
            process = subprocess.run(["/opt/blender/blender", "--background", "--disable-autoexec", "--factory-startup",
                "--python-exit-code", "1", "--python", str(Path(__file__).with_name("check_project.py")),
                "--", str(source), str(result)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=18)
            if process.returncode != 0 or not result.is_file():
                raise ProjectCheckError("PROJECT_FILE_INVALID")
            value = json.loads(result.read_text(encoding="utf-8"))
            if value.get("valid") is not True:
                raise ProjectCheckError("PROJECT_EXTERNAL_DEPENDENCIES")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise ProjectCheckError("PROJECT_PREFLIGHT_UNAVAILABLE") from None
