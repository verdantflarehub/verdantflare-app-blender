"""Structured scene inspection for live and reopened Blender .blend files."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any


PREFIXES = ("SET_", "GRID_", "PROP_", "ACTOR_", "CAM_", "LABEL_")


def _round_vec(values: Any) -> list[float]:
    try:
        return [round(float(x), 4) for x in values]
    except Exception:
        return []


def _extract_keyframe_frames(obj: Any) -> list[int]:
    frames: set[int] = set()
    try:
        anim = getattr(obj, "animation_data", None)
        action = getattr(anim, "action", None) if anim else None
        if not action:
            return []
        fcurves = list(getattr(action, "fcurves", []) or [])
        for layer in getattr(action, "layers", []) or []:
            for strip in getattr(layer, "strips", []) or []:
                for bag in getattr(strip, "channelbags", []) or []:
                    fcurves.extend(getattr(bag, "fcurves", []) or [])
        for fc in fcurves:
            for kp in getattr(fc, "keyframe_points", []) or []:
                co = getattr(kp, "co", None)
                if co is not None and len(co) >= 1:
                    frames.add(int(round(float(co[0]))))
    except Exception:
        pass
    return sorted(frames)


def inspect_bpy(bpy_module: Any) -> dict[str, Any]:
    sc = bpy_module.context.scene
    objects = sorted(sc.objects, key=lambda o: o.name)
    prefix_counts: dict[str, int] = {p: 0 for p in PREFIXES}
    prefix_counts["OTHER"] = 0
    prefix_groups: dict[str, list[str]] = {p: [] for p in PREFIXES}
    prefix_groups["OTHER"] = []

    cameras: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    key_objects: list[dict[str, Any]] = []

    for obj in objects:
        matched = False
        for p in PREFIXES:
            if obj.name.startswith(p):
                prefix_counts[p] += 1
                prefix_groups[p].append(obj.name)
                matched = True
                break
        if not matched:
            prefix_counts["OTHER"] += 1
            prefix_groups["OTHER"].append(obj.name)

        if obj.type == "CAMERA" or obj.name.startswith("CAM_"):
            data = getattr(obj, "data", None)
            cameras.append({
                "name": obj.name,
                "type": getattr(data, "type", None),
                "lens": round(float(getattr(data, "lens", 0.0)), 4) if data is not None else None,
                "ortho_scale": round(float(getattr(data, "ortho_scale", 0.0)), 4) if data is not None else None,
                "location": _round_vec(getattr(obj, "location", (0.0, 0.0, 0.0))),
                "rotation_euler": _round_vec(getattr(obj, "rotation_euler", (0.0, 0.0, 0.0))),
                "keyframe_frames": _extract_keyframe_frames(obj),
            })
        elif obj.type == "FONT" or obj.name.startswith("LABEL_"):
            data = getattr(obj, "data", None)
            labels.append({
                "name": obj.name,
                "body": getattr(data, "body", "") if data is not None else "",
                "location": _round_vec(getattr(obj, "location", (0.0, 0.0, 0.0))),
                "hide_render": bool(getattr(obj, "hide_render", False)),
            })
        elif obj.name.startswith(("SET_", "PROP_", "ACTOR_")):
            key_objects.append({
                "name": obj.name,
                "type": obj.type,
                "location": _round_vec(getattr(obj, "location", (0.0, 0.0, 0.0))),
                "dimensions": _round_vec(getattr(obj, "dimensions", (0.0, 0.0, 0.0))),
                "scale": _round_vec(getattr(obj, "scale", (1.0, 1.0, 1.0))),
            })

    compact_groups: dict[str, Any] = {}
    for k, names in prefix_groups.items():
        if k == "GRID_" and len(names) > 12:
            compact_groups[k] = {"count": len(names), "sample_first": names[:6], "sample_last": names[-6:]}
        else:
            compact_groups[k] = names

    unit_settings = getattr(sc, "unit_settings", None)
    render = getattr(sc, "render", None)
    active_cam = getattr(sc, "camera", None)
    filepath = str(getattr(bpy_module.data, "filepath", "") or "")
    return {
        "scene": {
            "name": getattr(sc, "name", "Scene"),
            "filepath": Path(filepath).name if filepath else "",
            "unit_system": getattr(unit_settings, "system", None),
            "scale_length": round(float(getattr(unit_settings, "scale_length", 1.0)), 6) if unit_settings else 1.0,
            "render_engine": getattr(render, "engine", None),
            "resolution_x": int(getattr(render, "resolution_x", 0)) if render else 0,
            "resolution_y": int(getattr(render, "resolution_y", 0)) if render else 0,
            "fps": int(getattr(render, "fps", 0)) if render else 0,
            "frame_start": int(getattr(sc, "frame_start", 1)),
            "frame_end": int(getattr(sc, "frame_end", 1)),
            "active_camera": getattr(active_cam, "name", None) if active_cam else None,
        },
        "object_count": len(objects),
        "prefix_counts": prefix_counts,
        "prefix_groups": compact_groups,
        "cameras": cameras,
        "labels": labels,
        "key_objects": key_objects,
    }


def _main() -> int:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    if len(argv) != 2:
        return 2
    blend_path, out_json = Path(argv[0]), Path(argv[1])
    import bpy  # type: ignore

    bpy.ops.wm.open_mainfile(filepath=str(blend_path), load_ui=False, use_scripts=False)
    report = inspect_bpy(bpy)
    report["reopened_cleanly"] = True
    out_json.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
