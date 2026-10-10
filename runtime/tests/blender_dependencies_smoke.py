"""Real Blender, disposable workspace only; no GPU or production scene."""
import hashlib
import importlib.util
import os
from pathlib import Path
import sys
import uuid
import bpy

root = Path(os.environ["BLENDER_MCP_ROOT"])
workspace = Path(os.environ["WORKSPACE_ROOT"])
workspace.mkdir(parents=True, exist_ok=True)
assert not (workspace / "project/main.blend").exists()
sys.path.insert(0, str(root))
from project_dependencies import external_dependencies

spec = importlib.util.spec_from_file_location("dependency_register", root / "blender-mcp-plugin/register.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
adapter = sys.modules["beagle_blender_mcp_plugin"]
spec = importlib.util.spec_from_file_location("dependency_check", root / "instance-control/project_check.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

def execute(operation, args=None):
    return adapter._execute({"protocol_version": 1, "deadline": 2**63 - 1, "operation": operation,
        "args": args or {}, "generation": adapter._GENERATION, "scene_version": adapter._SCENE_VERSION})

def request(source):
    return {"restore_id": str(uuid.uuid4()), "asset_id": source.relative_to(workspace).as_posix(),
            "size": source.stat().st_size, "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}

def rejected(source):
    generation, version = adapter._GENERATION, adapter._SCENE_VERSION
    objects = sorted(bpy.data.objects.keys())
    checkpoints = list((workspace / "checkpoints").glob("*.blend"))
    try:
        check.check(request(source))
        raise AssertionError("accepted an external dependency")
    except check.ProjectCheckError as exc:
        assert str(exc) == "PROJECT_EXTERNAL_DEPENDENCIES"
    assert (adapter._GENERATION, adapter._SCENE_VERSION) == (generation, version)
    assert sorted(bpy.data.objects.keys()) == objects
    assert list((workspace / "checkpoints").glob("*.blend")) == checkpoints

assert not external_dependencies(bpy), "factory bundled assets must be accepted"
texture = workspace / "texture.png"
image = bpy.data.images.new("DependencyTexture", width=2, height=2)
image.pixels = [1, 0, 0, 1] * 4
image.filepath_raw = str(texture)
image.file_format = "PNG"
image.save()
image.source = "FILE"
image.use_fake_user = True
external = workspace / ("inbox/" + "a" * 16 + "/restore.blend")
external.parent.mkdir(parents=True)
bpy.ops.wm.save_as_mainfile(filepath=str(external))
assert external_dependencies(bpy)
result = execute("scene.checkpoint")
assert result["error"]["code"] == "PROJECT_EXTERNAL_DEPENDENCIES", result
assert not list((workspace / "checkpoints").glob("*.blend"))
rejected(external)  # Even a currently accessible user file is not portable.
image.pack()
packed = workspace / ("inbox/" + "b" * 16 + "/restore.blend")
packed.parent.mkdir(parents=True)
bpy.ops.wm.save_as_mainfile(filepath=str(packed))
texture.unlink()
rejected(external)
assert not external_dependencies(bpy)
check.check(request(packed))
assert execute("scene.restore", request(packed))["ok"]
image = bpy.data.images["DependencyTexture"]
assert image.packed_file and len(image.pixels) == 16
assert list(image.pixels) == [1, 0, 0, 1] * 4
saved = execute("scene.checkpoint")
assert saved["ok"], saved
assert (workspace / saved["asset_id"]).is_file()

# A linked user library is rejected; bundled Blender libraries remain allowed.
library = workspace / "user-library.blend"
bpy.data.libraries.write(str(library), {bpy.data.objects["Cube"]})
with bpy.data.libraries.load(str(library), link=True) as (available, selected):
    selected.objects = [available.objects[0]]
bpy.context.scene.collection.objects.link(selected.objects[0])
assert external_dependencies(bpy)
assert execute("scene.checkpoint")["error"]["code"] == "PROJECT_EXTERNAL_DEPENDENCIES"
linked = workspace / ("inbox/" + "c" * 16 + "/restore.blend")
linked.parent.mkdir(parents=True)
bpy.ops.wm.save_as_mainfile(filepath=str(linked))
rejected(linked)
adapter.unregister()
print("DEPENDENCIES_SMOKE_OK: existing/missing image and linked library rejected; packed pixels restored; live scene unchanged on rejection")
