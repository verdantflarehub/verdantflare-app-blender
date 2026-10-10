"""The single-file Project contract excludes unpacked user resources."""
from pathlib import Path


def external_dependencies(bpy):
    # Blender's packed=False excludes embedded bytes (despite some releases'
    # inverted Python docstring). Real-bpy regression covers this behavior.
    bundled = (Path(bpy.utils.resource_path("LOCAL")) / "datafiles").resolve()
    paths = bpy.utils.blend_paths(absolute=True, packed=False, local=False)
    return sorted({raw for raw in paths if raw and not (
        Path(raw).is_file() and Path(raw).resolve().is_relative_to(bundled))})
