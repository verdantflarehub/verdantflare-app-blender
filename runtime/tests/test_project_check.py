import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("project_check_test", Path(__file__).parents[1] / "instance-control/project_check.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

batch_spec = importlib.util.spec_from_file_location("batch_run_test", Path(__file__).parents[1] / "instance-control/batch_run.py")
batch_run = importlib.util.module_from_spec(batch_spec)
batch_spec.loader.exec_module(batch_run)


class ProjectCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, WORKSPACE_ROOT=self.temp.name)
        env.start()
        self.addCleanup(env.stop)
        self.source = Path(self.temp.name) / ("inbox/" + "a" * 16 + "/restore.blend")
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b"test blend bytes")
        self.request = {"asset_id": self.source.relative_to(self.temp.name).as_posix(),
                        "size": self.source.stat().st_size, "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest()}

    def test_hash_and_path_fence_before_child_execution(self):
        with patch.object(check.subprocess, "run") as run:
            for changed in ({"size": 1}, {"sha256": "a" * 64}, {"asset_id": "../escape.blend"}):
                with self.assertRaisesRegex(check.ProjectCheckError, "^INVALID_ARGUMENT$"):
                    check.check(dict(self.request, **changed))
            run.assert_not_called()

    def test_child_result_and_failure_are_bounded_and_sanitized(self):
        def process(args, **kwargs):
            self.assertEqual(kwargs["timeout"], 18)
            self.assertIn("--disable-autoexec", args)
            Path(args[-1]).write_text(json.dumps({"valid": False, "external_count": 1}))
            return types.SimpleNamespace(returncode=0)
        with patch.object(check.subprocess, "run", side_effect=process):
            with self.assertRaisesRegex(check.ProjectCheckError, "^PROJECT_EXTERNAL_DEPENDENCIES$"):
                check.check(self.request)
        for error in (OSError("private path"), subprocess.TimeoutExpired("private path", 18)):
            with patch.object(check.subprocess, "run", side_effect=error):
                with self.assertRaisesRegex(check.ProjectCheckError, "^PROJECT_PREFLIGHT_UNAVAILABLE$"):
                    check.check(self.request)

    def test_unreadable_source_is_sanitized(self):
        with patch.object(Path, "open", side_effect=OSError("private path")):
            with self.assertRaisesRegex(check.ProjectCheckError, "^PROJECT_PREFLIGHT_UNAVAILABLE$"):
                check.check(self.request)

    def test_batch_run_concurrency_timeout_and_quota(self):
        script_path = Path(self.temp.name) / ("inbox/" + "b" * 16 + "/saw_set.py")
        script_path.parent.mkdir(parents=True)
        script_bytes = b"import bpy\nprint('DONE')\n"
        script_path.write_bytes(script_bytes)
        req = {
            "script_asset_id": script_path.relative_to(self.temp.name).as_posix(),
            "script_sha256": hashlib.sha256(script_bytes).hexdigest(),
            "script_size": len(script_bytes),
            "output_subdir": "saw-v1",
            "clean_output_subdir": True,
            "timeout_seconds": 30,
        }

        # 1. Single-instance concurrency lock -> WORKER_BUSY
        batch_run.BATCH_LOCK.acquire()
        try:
            with self.assertRaisesRegex(batch_run.BatchRunError, "^WORKER_BUSY$"):
                batch_run.run(req)
        finally:
            batch_run.BATCH_LOCK.release()

        # 2. Timeout -> BATCH_SCRIPT_TIMEOUT
        class FakeTimeoutProc:
            pid = 12345
            def wait(self, timeout=None):
                if timeout == 30:
                    raise subprocess.TimeoutExpired("blender", 30)
                return -9
            def kill(self):
                pass

        with patch.object(batch_run.subprocess, "Popen", return_value=FakeTimeoutProc()):
            with self.assertRaisesRegex(batch_run.BatchRunError, "^BATCH_SCRIPT_TIMEOUT$"):
                batch_run.run(req)

        # 3. Export quota exceeded -> EXPORT_QUOTA_EXCEEDED
        temp_name = self.temp.name
        class FakeQuotaProc:
            pid = 12346
            def wait(proc_self, timeout=None):
                out_dir = Path(temp_name) / "exports" / "saw-v1"
                for idx in range(5):
                    (out_dir / f"file_{idx}.bin").write_bytes(b"x" * 100)
                return 0

        with patch.dict(os.environ, BLENDER_EXPORT_MAX_FILES_PER_SUBDIR="3"), patch.object(
            batch_run.subprocess, "Popen", return_value=FakeQuotaProc()
        ):
            with self.assertRaisesRegex(batch_run.BatchRunError, "^EXPORT_QUOTA_EXCEEDED$"):
                batch_run.run(req)
        self.assertFalse((Path(self.temp.name) / "exports" / "saw-v1").exists())
