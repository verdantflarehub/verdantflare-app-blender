"""Test process/deadline guards without importing Blender or starting sockets."""
import importlib.util
from pathlib import Path
import sys
import time
import types
import unittest
from unittest.mock import patch


class AdapterGuards(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("adapter_guard_fixture", Path(__file__).parents[1] / "blender-mcp-plugin/__init__.py")
        self.adapter = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.adapter
        with patch.dict(sys.modules, {"bpy": types.SimpleNamespace()}):
            spec.loader.exec_module(self.adapter)

    def request(self, **values):
        return dict({"protocol_version": 1, "generation": self.adapter._GENERATION, "deadline": int(time.time()*1000)+10000, "operation": "object.create", "args": {}, "scene_version": 0}, **values)

    def test_expired_and_previous_process_commands_never_reach_bpy(self):
        self.assertEqual(self.adapter._execute(self.request(deadline=0))["error"]["code"], "OPERATION_TIMEOUT")
        self.assertEqual(self.adapter._execute(self.request(generation="old"))["error"]["code"], "STALE_GENERATION")
        self.assertEqual(self.adapter._execute(self.request(scene_version=True))["error"]["code"], "SCENE_VERSION_CONFLICT")

    def test_asset_import_obeys_scene_lock(self):
        result = self.adapter._execute(self.request(operation="asset.import", scene_version=7))
        self.assertEqual(result["error"]["code"], "SCENE_VERSION_CONFLICT")

    def test_transform_validation_is_atomic(self):
        obj = types.SimpleNamespace(location=(0, 0, 0), rotation_euler=(0, 0, 0), scale=(1, 1, 1))
        self.adapter.bpy.data = types.SimpleNamespace(objects={"Cube": obj})
        result = self.adapter._execute(self.request(operation="object.update_transform", args={"object_id": "Cube", "location": [1,2,3], "scale": [1,2]}))
        self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")
        self.assertEqual(obj.location, (0,0,0))


if __name__ == "__main__":
    unittest.main()
