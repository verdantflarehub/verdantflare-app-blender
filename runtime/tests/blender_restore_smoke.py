"""Run with Blender --background --disable-autoexec in a disposable workspace."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import uuid

import bpy

root = Path(os.environ["BLENDER_MCP_ROOT"])
workspace = Path(os.environ["WORKSPACE_ROOT"])
assert not (workspace / "project/main.blend").exists(), "requires a fresh workspace"
spec = importlib.util.spec_from_file_location("restore_smoke_register", root / "blender-mcp-plugin/register.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
adapter = sys.modules["beagle_blender_mcp_plugin"]


def call(operation, args=None, generation=None):
    result = adapter._execute({"protocol_version": 1, "deadline": 2**63 - 1, "operation": operation,
        "args": args or {}, "generation": generation or adapter._GENERATION, "scene_version": adapter._SCENE_VERSION})
    assert result.get("ok"), result
    return result


call("object.create", {"object_id": "Restored_Cube", "primitive": "cube", "location": [3, 4, 5]})
asset = "inbox/" + "a" * 16 + "/restore.blend"
source = workspace / asset
source.parent.mkdir(parents=True)
# A saved registered text must not be executed when restoring.
text = bpy.data.texts.new("restore-test.py")
marker = workspace / "autoexec-ran"
text.write("from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\n")
text.use_module = True
bpy.ops.wm.save_as_mainfile(filepath=str(source))
call("object.delete", {"object_id": "Restored_Cube"})
call("object.create", {"object_id": "Before_Restore", "primitive": "cube"})
old_generation = adapter._GENERATION
args = {"restore_id": str(uuid.uuid4()), "asset_id": asset,
    "size": source.stat().st_size, "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
restored = call("scene.restore", args)
assert restored["generation"] != old_generation
assert not marker.exists()
assert call("object.get", {"object_id": "Restored_Cube"})["object"]["location"] == [3, 4, 5]
assert "Before_Restore" not in bpy.data.objects
assert len(list((workspace / "checkpoints").glob("pre-restore-*.blend"))) == 1
call("scene.restore", args)  # Exact recovery does not load or checkpoint again.
assert len(list((workspace / "checkpoints").glob("pre-restore-*.blend"))) == 1
assert call("scene.get")["restore_id"] == args["restore_id"]
assert json.loads((workspace / "project/restore.json").read_text())["restore_id"] == args["restore_id"]
stale = adapter._execute({"protocol_version": 1, "deadline": 2**63 - 1, "operation": "object.delete",
    "args": {"object_id": "Restored_Cube"}, "generation": old_generation, "scene_version": 0})
assert stale["error"]["code"] == "STALE_GENERATION"
# Verify the receipt survives adapter/process initialization with the exact file.
adapter.unregister()
adapter._RESTORE_ID = None
adapter.register()
assert call("scene.get")["restore_id"] == args["restore_id"]
call("object.update_transform", {"object_id": "Restored_Cube", "location": [6, 7, 8]})
checkpoint = call("scene.checkpoint")
assert (workspace / checkpoint["asset_id"]).stat().st_size > 0
adapter.unregister()
print("RESTORE_SMOKE_OK: scene, scripts-disabled, checkpoint, generation, receipt, continuation")
