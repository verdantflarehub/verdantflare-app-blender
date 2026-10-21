"""Prepare disposable resources before a real-Blender smoke imports the addon."""
import os
from pathlib import Path
import sys
import tempfile


def prepare(bpy):
    # A test must never repurpose an interactive scene or an existing adapter.
    if not bpy.app.background or bpy.data.filepath or "beagle_blender_mcp_plugin" in sys.modules:
        raise RuntimeError("smoke requires a fresh background Blender without an adapter")
    temporary = tempfile.TemporaryDirectory(prefix="vf-bsmoke-", dir="/tmp")
    root = Path(temporary.name).resolve()
    workspace = root / "workspace"
    workspace.mkdir()
    # WORKSPACE_ROOT alone is insufficient: register/unregister also unlink the
    # socket. Never inherit that endpoint from a running instance's environment.
    os.environ.update(WORKSPACE_ROOT=str(workspace),
                      BLENDER_MCP_ADAPTER_SOCKET=str(root / "adapter.sock"),
                      BLENDER_USER_CONFIG=str(root / "config"))
    return temporary
