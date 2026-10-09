from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("workspace", ROOT / "file-agent" / "workspace.py")
assert SPEC and SPEC.loader
workspace = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = workspace
SPEC.loader.exec_module(workspace)

SERVER_SPEC = importlib.util.spec_from_file_location("file_agent_server", ROOT / "file-agent" / "server.py")
assert SERVER_SPEC and SERVER_SPEC.loader
file_agent_server = importlib.util.module_from_spec(SERVER_SPEC)
sys.modules[SERVER_SPEC.name] = file_agent_server
SERVER_SPEC.loader.exec_module(file_agent_server)


class WorkspaceTest(unittest.TestCase):
    def test_asset_path_rejects_escape_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = workspace.workspace_root(directory)
            (root / "project").mkdir()
            (root / "project" / "scene.blend").write_bytes(b"blend")
            self.assertEqual(workspace.asset_path(root, "project/scene.blend").name, "scene.blend")
            with self.assertRaises(workspace.WorkspaceError):
                workspace.asset_path(root, "../outside")
            outside = Path(directory).parent / "outside-asset"
            outside.write_text("private")
            try:
                (root / "project" / "link").symlink_to(outside)
                with self.assertRaises(workspace.WorkspaceError):
                    workspace.asset_path(root, "project/link")
            finally:
                outside.unlink(missing_ok=True)

    def test_sha256_and_filename_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "asset.bin"
            path.write_bytes(b"abc")
            self.assertEqual(workspace.sha256_file(path), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
            self.assertEqual(workspace.safe_filename("asset.bin"), "asset.bin")
            with self.assertRaises(workspace.WorkspaceError):
                workspace.safe_filename("../asset.bin")

    def test_file_agent_requires_nonempty_token(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                file_agent_server.FileState(Path(directory), "", 1024)
            state = file_agent_server.FileState(Path(directory), "token", 1024)
            self.assertTrue(state.authorized("Bearer token"))
            self.assertFalse(state.authorized("Bearer "))


if __name__ == "__main__":
    unittest.main()
