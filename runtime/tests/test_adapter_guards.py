"""Test process/deadline guards without importing Blender or starting sockets."""
import importlib.util
from pathlib import Path
import sys
import tempfile
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

    def test_cancelled_save_cannot_reuse_an_old_file_as_success(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'main.blend'
            target.write_bytes(b'previous-save')
            self.adapter.bpy.ops = types.SimpleNamespace(wm=types.SimpleNamespace(save_as_mainfile=lambda **kwargs: {'CANCELLED'}))
            with self.assertRaises(OSError):
                self.adapter._save(target)
            self.assertEqual(target.read_bytes(), b'previous-save')

    def test_checkpoint_is_one_save_and_an_exact_immutable_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            self.adapter._WORKSPACE = Path(directory)
            paths = []
            def save(filepath):
                paths.append(filepath)
                Path(filepath).write_bytes(b'blender-file-at-' + filepath.encode())
                return {'FINISHED'}
            self.adapter.bpy.ops = types.SimpleNamespace(wm=types.SimpleNamespace(save_as_mainfile=save))
            with patch.object(self.adapter._dependencies, 'external_dependencies', return_value=[]):
                result = self.adapter._execute(self.request(operation='scene.checkpoint'))
            self.assertTrue(result['ok'])
            target = Path(directory) / 'project/main.blend'
            snapshot = Path(directory) / result['asset_id']
            self.assertEqual(paths, [str(target)])
            self.assertEqual(snapshot.read_bytes(), target.read_bytes())
            target.write_bytes(b'later-edits')
            self.assertNotEqual(snapshot.read_bytes(), target.read_bytes())


if __name__ == "__main__":
    unittest.main()
