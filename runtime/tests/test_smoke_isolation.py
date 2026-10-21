import os
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_isolation import prepare


def scene(background=True, filepath=""):
    return NS(app=NS(background=background), data=NS(filepath=filepath))


class IsolationTests(unittest.TestCase):
    def test_inherited_workspace_and_live_socket_survive(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as original, patch.dict(os.environ):
            root = Path(original)
            saved = root / "main.blend"
            saved.write_bytes(b"existing project")
            endpoint = root / "adapter.sock"
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(str(endpoint))
                listener.listen(1)
                os.environ.update(WORKSPACE_ROOT=original,
                                  BLENDER_MCP_ADAPTER_SOCKET=str(endpoint),
                                  BLENDER_USER_CONFIG=str(root / "config"))
                inode = endpoint.stat().st_ino
                resources = prepare(scene())
                isolated = Path(os.environ["BLENDER_MCP_ADAPTER_SOCKET"])
                self.assertNotEqual(isolated, endpoint)
                self.assertTrue(isolated.is_relative_to(Path(resources.name).resolve()))
                # Simulate addon register/unregister on the isolated endpoint.
                with socket.socket(socket.AF_UNIX) as test_listener:
                    test_listener.bind(str(isolated))
                isolated.unlink()
                resources.cleanup()
                self.assertEqual(endpoint.stat().st_ino, inode)
                self.assertEqual(saved.read_bytes(), b"existing project")
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(1)
                    client.connect(str(endpoint))
                    connection, _ = listener.accept()
                    connection.close()

    def test_rejects_loaded_project_before_changing_environment(self):
        with patch.dict(os.environ):
            before = dict(os.environ)
            with self.assertRaises(RuntimeError):
                prepare(scene(filepath="existing.blend"))
            self.assertEqual(dict(os.environ), before)

    def test_rejects_interactive_blender(self):
        with self.assertRaises(RuntimeError):
            prepare(scene(background=False))

    def test_rejects_existing_adapter(self):
        with patch.dict(sys.modules, {"beagle_blender_mcp_plugin": object()}):
            with self.assertRaises(RuntimeError):
                prepare(scene())

    def test_separate_invocations_have_separate_resources(self):
        with patch.dict(os.environ):
            first = prepare(scene())
            try:
                second = prepare(scene())
                try:
                    self.assertNotEqual(first.name, second.name)
                finally:
                    second.cleanup()
            finally:
                first.cleanup()


if __name__ == "__main__":
    unittest.main()
