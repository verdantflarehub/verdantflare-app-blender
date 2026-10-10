"""Run inside Blender to verify the adapter operations against real bpy."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


root = Path(os.environ.get("BLENDER_MCP_ROOT", "/opt/beagle/blender-mcp"))
register_path = root / "blender-mcp-plugin" / "register.py"
spec = importlib.util.spec_from_file_location("blender_mcp_register_smoke", register_path)
if spec is None or spec.loader is None:
    raise RuntimeError("register.py could not be loaded")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
adapter = sys.modules["beagle_blender_mcp_plugin"]


def call(operation, args, version=None):
    request = {"protocol_version": 1, "request_id": operation, "deadline": 2**63 - 1, "operation": operation, "args": args, "generation": adapter._GENERATION}
    if version is not None:
        request["scene_version"] = version
    result = adapter._execute(request)
    if not result.get("ok"):
        raise RuntimeError(result)
    return result


scene = call("scene.get", {})
assert scene["scene_version"] == 0, scene
created = call("object.create", {"object_id": "MCP_Cube", "primitive": "cube"}, 0)
assert created["scene_version"] == 1, created
object_info = call("object.get", {"object_id": "MCP_Cube"})
assert object_info["object"]["object_id"] == "MCP_Cube", object_info
saved = call("scene.save", {}, 1)
assert (Path(os.environ.get("WORKSPACE_ROOT", "/workspace")) / "project" / "main.blend").is_file(), saved
checkpoint = call("scene.checkpoint", {}, 2)
assert checkpoint["asset_id"].startswith("checkpoints/"), checkpoint
working = Path(os.environ.get("WORKSPACE_ROOT", "/workspace")) / "project/main.blend"
snapshot = working.parents[1] / checkpoint["asset_id"]
assert working.read_bytes() == snapshot.read_bytes(), 'checkpoint differs from restart working copy'
assert Path(adapter.bpy.data.filepath).resolve() == working.resolve(), 'checkpoint changed active filepath'
exported = call("asset.export", {"asset_id": "exports/smoke.glb", "format": "glb"})
assert exported["sha256"] and exported["size"] > 0, exported
print("blender adapter smoke passed", exported["asset_id"], exported["size"])
