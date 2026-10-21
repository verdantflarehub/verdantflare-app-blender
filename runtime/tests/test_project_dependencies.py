"""Dependency provenance must not mask a real resource at the same path."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS
import unittest

spec = importlib.util.spec_from_file_location('dependencies', Path(__file__).parents[1] / 'project_dependencies.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Owner:
    def __init__(self, path):
        self.library_weak_reference = NS(filepath=path) if path else None
        self.library = None


def fake(paths, weak):
    owners = {Owner(path): set() for path in weak}
    return NS(utils=NS(resource_path=lambda kind: '/blender', blend_paths=lambda **kwargs: paths),
              data=NS(user_map=lambda: owners), path=NS(abspath=lambda path, **kwargs: path))


class DependenciesTests(unittest.TestCase):
    def test_appended_missing_source_is_not_required(self):
        self.assertEqual(module.external_dependencies(fake(['/missing/source.blend'] * 2,
                                                          ['/missing/source.blend'] * 2)), [])

    def test_real_link_with_same_path_is_still_required(self):
        self.assertEqual(module.external_dependencies(fake(['/missing/source.blend'] * 3,
                                                          ['/missing/source.blend'] * 2)), ['/missing/source.blend'])

    def test_ordinary_missing_texture_still_rejected(self):
        self.assertEqual(module.external_dependencies(fake(['/missing/texture.png'], [])), ['/missing/texture.png'])

    def test_unrelated_weak_reference_does_not_hide_resource(self):
        self.assertEqual(module.external_dependencies(fake(['/missing/texture.png'], ['/source.blend'])), ['/missing/texture.png'])

    def test_owner_metadata_is_not_modified(self):
        bpy = fake(['/source.blend'], ['/source.blend'])
        owner = next(iter(bpy.data.user_map()))
        module.external_dependencies(bpy)
        self.assertEqual(owner.library_weak_reference.filepath, '/source.blend')


if __name__ == '__main__':
    unittest.main()
