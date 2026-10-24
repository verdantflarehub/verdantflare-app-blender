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
            # Match the runtime's canonical workspace on macOS (/var -> /private/var).
            directory = str(Path(directory).resolve())
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

    def test_scene_inspect_and_open_file_preserve_generation_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            self.adapter._WORKSPACE = Path(directory)
            export_blend = Path(directory) / "exports/saw-bathroom-v1/SAW_bathroom_set_v1.blend"
            export_blend.parent.mkdir(parents=True, exist_ok=True)
            export_blend.write_bytes(b"BLENDER-v450-saw-set")

            cam_data = types.SimpleNamespace(type="PERSP", lens=28.0, ortho_scale=6.8)
            kp1 = types.SimpleNamespace(co=(1.0, 0.0))
            kp2 = types.SimpleNamespace(co=(120.0, 0.25))
            fcurve = types.SimpleNamespace(keyframe_points=[kp1, kp2])
            cam_obj = types.SimpleNamespace(
                name="CAM_C1", type="CAMERA", data=cam_data,
                location=(2.5, 0.45, 0.95), rotation_euler=(1.53, 0.0, 0.78), scale=(1.0, 1.0, 1.0),
                animation_data=types.SimpleNamespace(action=types.SimpleNamespace(fcurves=[fcurve], layers=[])),
            )
            wall_obj = types.SimpleNamespace(
                name="SET_wall_north", type="MESH",
                location=(0.0, 5.0, 1.5), dimensions=(6.0, 0.12, 3.0), scale=(1.0, 1.0, 1.0),
            )
            label_obj = types.SimpleNamespace(
                name="LABEL_ADAM", type="FONT", data=types.SimpleNamespace(body="ADAM"),
                location=(-2.9, 1.9, 2.0), hide_render=True, scale=(1.0, 1.0, 1.0),
            )
            scene = types.SimpleNamespace(
                name="Scene",
                objects=[cam_obj, wall_obj, label_obj],
                unit_settings=types.SimpleNamespace(system="METRIC", scale_length=1.0),
                render=types.SimpleNamespace(engine="BLENDER_WORKBENCH", resolution_x=1344, resolution_y=768, fps=24),
                frame_start=1, frame_end=120, camera=cam_obj,
            )
            self.adapter.bpy.context = types.SimpleNamespace(scene=scene)
            self.adapter.bpy.data = types.SimpleNamespace(filepath=str(export_blend), objects={o.name: o for o in scene.objects})

            inspected = self.adapter._execute(self.request(operation="scene.inspect_file", args={}, scene_version=None))
            self.assertTrue(inspected["ok"], inspected)
            self.assertEqual(inspected["scene"]["unit_system"], "METRIC")
            self.assertEqual(inspected["prefix_counts"]["CAM_"], 1)
            self.assertEqual(inspected["prefix_counts"]["SET_"], 1)
            self.assertEqual(inspected["prefix_counts"]["LABEL_"], 1)
            self.assertEqual(inspected["cameras"][0]["keyframe_frames"], [1, 120])
            self.assertEqual(inspected["labels"][0]["body"], "ADAM")

            initial_gen = self.adapter._GENERATION
            def save_main(filepath, copy=False):
                Path(filepath).parent.mkdir(parents=True, exist_ok=True)
                Path(filepath).write_bytes(b"saved-blend")
                return {"FINISHED"}
            def open_main(filepath, load_ui=False, use_scripts=False):
                self.adapter.bpy.data.filepath = filepath
                return {"FINISHED"}
            self.adapter.bpy.ops = types.SimpleNamespace(wm=types.SimpleNamespace(save_as_mainfile=save_main, open_mainfile=open_main))
            with patch.object(self.adapter._dependencies, "external_dependencies", return_value=[]):
                opened = self.adapter._execute(
                    self.request(
                        operation="scene.open_file",
                        args={"asset_id": "exports/saw-bathroom-v1/SAW_bathroom_set_v1.blend", "save_as_project_main": True},
                        scene_version=0,
                    )
                )
            self.assertTrue(opened["ok"], opened)
            self.assertEqual(opened["generation"], initial_gen)
            self.assertEqual(opened["scene_version"], 1)
            self.assertTrue((Path(directory) / "project/main.blend").is_file())


if __name__ == "__main__":
    unittest.main()
