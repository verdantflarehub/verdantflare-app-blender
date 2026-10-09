"""Load and register the bundled Blender MCP addon when Blender starts."""

from __future__ import annotations

import importlib.util
import os
import sys


PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
MODULE_NAME = "beagle_blender_mcp_plugin"

if MODULE_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(MODULE_NAME, os.path.join(PLUGIN_DIR, "__init__.py"))
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Blender MCP addon")
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
else:
    module = sys.modules[MODULE_NAME]

module.register()
