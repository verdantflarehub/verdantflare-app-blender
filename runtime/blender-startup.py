"""Deterministic Blender startup for the MCP image."""

from __future__ import annotations

import bpy
import importlib.util
import os
import sys


project = os.path.realpath(os.path.join(os.environ.get("WORKSPACE_ROOT", "/workspace"), "project", "main.blend"))
workspace = os.path.realpath(os.environ.get("WORKSPACE_ROOT", "/workspace"))
if os.path.isfile(project) and (project == workspace or project.startswith(workspace + os.sep)):
    bpy.ops.wm.open_mainfile(filepath=project)

plugin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blender-mcp-plugin", "register.py")
spec = importlib.util.spec_from_file_location("beagle_blender_mcp_register", plugin)
if spec is None or spec.loader is None:
    raise RuntimeError("cannot load Blender MCP register script")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
