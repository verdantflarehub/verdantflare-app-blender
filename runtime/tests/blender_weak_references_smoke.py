"""Run with real Blender in an isolated, disposable dev workspace."""
import json
import os
from pathlib import Path
import sys
import bpy

root = Path(os.environ['BLENDER_MCP_ROOT'])
workspace = Path(os.environ['WORKSPACE_ROOT'])
workspace.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(root))
from project_dependencies import external_dependencies

source = workspace / 'append-source.blend'
bpy.data.libraries.write(str(source), {bpy.data.objects['Cube']})
with bpy.data.libraries.load(str(source), link=False) as (available, selected):
    selected.objects = ['Cube']
bpy.context.scene.collection.objects.link(selected.objects[0])
checkpoint = workspace / 'appended.blend'
bpy.ops.wm.save_as_mainfile(filepath=str(checkpoint))
bpy.ops.wm.open_mainfile(filepath=str(checkpoint), load_ui=False, use_scripts=False)
weak = [owner for owner in bpy.data.user_map() if owner.library_weak_reference]
assert weak, 'fixture must actually create append provenance'
assert not external_dependencies(bpy), {'required': external_dependencies(bpy), 'weak_paths': [owner.library_weak_reference.filepath for owner in weak], 'paths': bpy.utils.blend_paths(absolute=True, packed=False, local=False)}
content = source.read_bytes()
source.unlink()
assert not external_dependencies(bpy), 'missing append source must not break portability'
checkpoint = workspace / 'appended.blend'
bpy.ops.wm.save_as_mainfile(filepath=str(checkpoint))
bpy.ops.wm.open_mainfile(filepath=str(checkpoint), load_ui=False, use_scripts=False)
assert any(owner.library_weak_reference for owner in bpy.data.user_map())
assert not external_dependencies(bpy)
source.write_bytes(content)
with bpy.data.libraries.load(str(source), link=True) as (available, selected):
    selected.objects = ['Cube']
bpy.context.scene.collection.objects.link(selected.objects[0])
assert str(source) in external_dependencies(bpy), 'real link sharing weak path must be rejected'
source.unlink()
assert str(source) in external_dependencies(bpy)
print(json.dumps({'weak_reference_owners': len(weak), 'append_and_missing_source_pass': True,
                  'checkpoint_reopen_pass': True, 'same_path_real_link_rejected': True}))
