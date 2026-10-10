"""Executed only in a separate, script-disabled Blender preflight process."""
import json
from pathlib import Path
import sys

import bpy

sys.path.insert(0, str(Path(__file__).parents[1]))
from project_dependencies import external_dependencies

source, result = sys.argv[sys.argv.index("--") + 1:]
bpy.ops.wm.open_mainfile(filepath=source, load_ui=False, use_scripts=False)
count = len(external_dependencies(bpy))
Path(result).write_text(json.dumps({"valid": count == 0, "external_count": count}), encoding="utf-8")
