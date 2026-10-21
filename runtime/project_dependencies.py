"""The single-file Project contract excludes unpacked user resources."""
from pathlib import Path
from collections import Counter
import os


def external_dependencies(bpy):
    # Blender's packed=False excludes embedded bytes (despite some releases'
    # inverted Python docstring). Real-bpy regression covers this behavior.
    bundled = (Path(bpy.utils.resource_path("LOCAL")) / "datafiles").resolve()
    paths = bpy.utils.blend_paths(absolute=True, packed=False, local=False)
    # Append provenance survives independently of its source .blend. Blender's
    # missing-files checker skips it, but blend_paths includes it. Subtract one
    # occurrence per owner, not every matching path: an actual linked library
    # may use the very same file and must still prevent single-file export.
    weak_paths = Counter()
    for owner in bpy.data.user_map():
        reference = owner.library_weak_reference
        if reference and reference.filepath:
            absolute = bpy.path.abspath(reference.filepath, library=owner.library)
            weak_paths[os.path.normpath(absolute)] += 1
    required = []
    for raw in paths:
        key = os.path.normpath(raw) if raw else ""
        if weak_paths[key]:
            weak_paths[key] -= 1
        else:
            required.append(raw)
    return sorted({raw for raw in required if raw and not (
        Path(raw).is_file() and Path(raw).resolve().is_relative_to(bundled))})
