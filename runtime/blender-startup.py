"""Deterministic Blender startup for the MCP image."""

from __future__ import annotations

import bpy
import importlib.util
import os
import sys


startup_spec = importlib.util.spec_from_file_location("blender_startup_workspace", os.path.join(os.path.dirname(os.path.abspath(__file__)), "startup_workspace.py"))
startup = importlib.util.module_from_spec(startup_spec)
sys.modules[startup_spec.name] = startup
startup_spec.loader.exec_module(startup)
startup.proof = startup.load(bpy, os.environ)

plugin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blender-mcp-plugin", "register.py")
spec = importlib.util.spec_from_file_location("beagle_blender_mcp_register", plugin)
if spec is None or spec.loader is None:
    raise RuntimeError("cannot load Blender MCP register script")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
