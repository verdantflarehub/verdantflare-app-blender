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
