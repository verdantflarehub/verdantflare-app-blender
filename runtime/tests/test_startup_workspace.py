"""Managed startup must prove the requested content loaded before becoming ready."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock
import uuid

spec = importlib.util.spec_from_file_location('startup_test', Path(__file__).parents[1] / 'startup_workspace.py')
startup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(startup)


class StartupWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'project').mkdir()
        (self.root / '.verdantflare').mkdir()
        self.target = self.root / 'project/main.blend'
        self.env = {'WORKSPACE_ROOT': str(self.root), 'BLENDER_START_EMPTY': 'false'}
        for field in ('BLENDER_START_OPERATION_ID', 'BLENDER_CREATE_OPERATION_ID', 'INSTANCE_ID', 'BLENDER_PROJECT_ID', 'BLENDER_POD_UID', 'BLENDER_SOURCE_REVISION_ID'):
            self.env[field] = str(uuid.uuid4())
        self.data = b'fixture-bytes-are-not-a-real-blend'
        self.target.write_bytes(self.data)
        self.env.update(BLENDER_START_SHA256=hashlib.sha256(self.data).hexdigest(), BLENDER_START_SIZE=str(len(self.data)))
        self.prepared = {'state': 'prepared', 'prepare_id': self.env['BLENDER_CREATE_OPERATION_ID'], 'instance_id': self.env['INSTANCE_ID'],
            'project_id': self.env['BLENDER_PROJECT_ID'], 'source_revision_id': self.env['BLENDER_SOURCE_REVISION_ID'],
            'asset_id': 'inbox/0123456789abcdef/restore.blend', 'sha256': self.env['BLENDER_START_SHA256'], 'size': len(self.data)}
        self.write_journal()
        self.bpy = types.SimpleNamespace(data=types.SimpleNamespace(filepath=''), ops=types.SimpleNamespace(wm=types.SimpleNamespace()))
        def opened(**kwargs):
            self.bpy.data.filepath = kwargs['filepath']
            return {'FINISHED'}
        self.bpy.ops.wm.open_mainfile = Mock(side_effect=opened)
        self.bpy.ops.wm.read_factory_settings = Mock(return_value={'FINISHED'})

    def write_journal(self):
        (self.root / '.verdantflare/prepare.json').write_text(json.dumps(self.prepared), encoding='utf-8')

    def test_exact_load_returns_identity_and_scripts_stay_disabled(self):
        proof = startup.load(self.bpy, self.env)
        self.assertEqual(proof['sha256'], self.env['BLENDER_START_SHA256'])
        self.assertEqual(proof['pod_uid'], self.env['BLENDER_POD_UID'])
        self.assertEqual(proof['start_operation_id'], self.env['BLENDER_START_OPERATION_ID'])
        self.bpy.ops.wm.open_mainfile.assert_called_once_with(filepath=str(self.target.resolve()), load_ui=False, use_scripts=False)
        self.bpy.ops.wm.read_factory_settings.assert_not_called()

    def test_missing_or_changed_project_never_loads_blank(self):
        for content in (None, b'changed'):
            with self.subTest(content=content):
                if content is None:
                    self.target.unlink()
                else:
                    self.target.write_bytes(content)
                with self.assertRaises((ValueError, OSError)):
                    startup.load(self.bpy, self.env)
        self.bpy.ops.wm.open_mainfile.assert_not_called()
        self.bpy.ops.wm.read_factory_settings.assert_not_called()

    def test_cross_project_and_unprepared_journal_rejected(self):
        for field, value in (('project_id', str(uuid.uuid4())), ('state', 'preparing')):
            prior = self.prepared[field]
            self.prepared[field] = value
            self.write_journal()
            with self.assertRaises(ValueError):
                startup.load(self.bpy, self.env)
            self.prepared[field] = prior
        self.bpy.ops.wm.open_mainfile.assert_not_called()

    def test_cancelled_failed_wrong_path_and_during_load_mutation_have_no_proof(self):
        for action in ('cancel', 'exception', 'wrong-path', 'changed'):
            with self.subTest(action=action):
                self.target.write_bytes(self.data)
                self.bpy.data.filepath = ''
                def load(**kwargs):
                    if action == 'exception':
                        raise RuntimeError('incompatible project')
                    self.bpy.data.filepath = kwargs['filepath'] if action != 'wrong-path' else '/other.blend'
                    if action == 'changed':
                        self.target.write_bytes(b'changed while loading')
                    return {'CANCELLED'} if action == 'cancel' else {'FINISHED'}
                self.bpy.ops.wm.open_mainfile.side_effect = load
                with self.assertRaises((ValueError, RuntimeError)):
                    startup.load(self.bpy, self.env)

    def test_empty_is_explicit_and_never_overwrites_existing_work(self):
        self.env.update(BLENDER_START_EMPTY='true', BLENDER_START_SIZE='0', BLENDER_START_SHA256='')
        self.prepared.update(asset_id=None, sha256=None, size=0)
        self.write_journal()
        with self.assertRaises(ValueError):
            startup.load(self.bpy, self.env)
        self.bpy.ops.wm.read_factory_settings.assert_not_called()
        self.target.unlink()
        proof = startup.load(self.bpy, self.env)
        self.assertTrue(proof['empty'])
        self.bpy.ops.wm.read_factory_settings.assert_called_once_with(use_empty=True)

    def test_saved_working_copy_can_differ_from_original_creation_revision(self):
        self.prepared['sha256'] = 'a' * 64
        self.write_journal()
        self.assertEqual(startup.load(self.bpy, self.env)['sha256'], self.env['BLENDER_START_SHA256'])

    def test_symlink_project_and_missing_source_type_rejected(self):
        outside = self.root / 'outside.blend'
        outside.write_bytes(self.data)
        self.target.unlink()
        self.target.symlink_to(outside)
        with self.assertRaises(ValueError):
            startup.load(self.bpy, self.env)
        self.target.unlink()
        self.target.write_bytes(self.data)
        del self.env['BLENDER_START_EMPTY']
        with self.assertRaises(ValueError):
            startup.load(self.bpy, self.env)


if __name__ == '__main__':
    unittest.main()
