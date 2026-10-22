from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "file-agent"))
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
                try:
                    (root / "project" / "link").symlink_to(outside)
                except OSError as exc:
                    if getattr(exc, "winerror", None) == 1314:
                        return
                    raise
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

    def test_extended_extensions_and_workspace_clean_http(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as home_dir:
            root = workspace.workspace_root(directory)
            state = file_agent_server.FileState(root, "secret-token", 10 * 1024 * 1024)
            server = file_agent_server.FileServer(("127.0.0.1", 0), state)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                for filename, payload in [
                    ("saw_set_v1.py", b"import bpy\nprint('DONE')\n"),
                    ("previs_C1.mp4", b"\x00\x00\x00\x18ftypmp42"),
                    ("set_v1.json", b'{"cameras": 9}'),
                    ("build.log", b"Fra:1 Mem:24M\nDONE\n"),
                ]:
                    sha = hashlib.sha256(payload).hexdigest()
                    req = urllib.request.Request(
                        base + "/upload",
                        data=payload,
                        method="POST",
                        headers={
                            "Authorization": "Bearer secret-token",
                            "Content-Type": "application/octet-stream",
                            "Content-Length": str(len(payload)),
                            "X-Asset-Name": filename,
                            "X-Asset-SHA256": sha,
                        },
                    )
                    with urllib.request.urlopen(req) as resp:
                        self.assertEqual(resp.status, 201)
                        uploaded = json.loads(resp.read())
                    self.assertTrue(uploaded["asset_id"].endswith("/" + filename))
                    get_req = urllib.request.Request(
                        base + "/assets/" + uploaded["asset_id"],
                        headers={"Authorization": "Bearer secret-token"},
                    )
                    with urllib.request.urlopen(get_req) as resp:
                        self.assertEqual(resp.read(), payload)
                        self.assertEqual(resp.headers["X-Asset-SHA256"], sha)

                # Prepare exports subdirectories and protected files
                (root / "project" / "main.blend").write_bytes(b"main-blend")
                (root / "exports" / "saw-bathroom-v1").mkdir(parents=True, exist_ok=True)
                (root / "exports" / "saw-bathroom-v1" / "stale.mp4").write_bytes(b"stale")
                (root / "exports" / "keep-other").mkdir(parents=True, exist_ok=True)
                (Path(home_dir) / ".bashrc").write_text("# bashrc", encoding="utf-8")

                cleaned = workspace.clean_workspace(
                    root,
                    remove_stray_home_c_drive=True,
                    export_subdirs=["saw-bathroom-v1"],
                    home_dir=Path(home_dir),
                )
                self.assertIn("exports/saw-bathroom-v1", cleaned["cleaned_paths"])
                self.assertFalse(cleaned["home_stray_c_exists"])
                self.assertIn(".bashrc", cleaned["home_entries"])
                self.assertEqual(cleaned["exports_entries"], ["keep-other"])
                self.assertTrue((root / "project" / "main.blend").is_file())

                for bad in [["../project"], ["a/b"], [".hidden"]]:
                    with self.assertRaises(workspace.WorkspaceError):
                        workspace.clean_workspace(root, export_subdirs=bad, home_dir=Path(home_dir))

                clean_body = json.dumps({"remove_stray_home_c_drive": True, "export_subdirs": ["keep-other"]}).encode()
                clean_req = urllib.request.Request(
                    base + "/internal/workspace/clean",
                    data=clean_body,
                    method="POST",
                    headers={
                        "Authorization": "Bearer secret-token",
                        "Content-Type": "application/json",
                        "Content-Length": str(len(clean_body)),
                    },
                )
                with urllib.request.urlopen(clean_req) as resp:
                    self.assertEqual(resp.status, 200)
                    body = json.loads(resp.read())
                self.assertTrue(body["ok"])
                self.assertEqual(body["cleaned_paths"], ["exports/keep-other"])
                self.assertEqual(body["exports_entries"], [])
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
