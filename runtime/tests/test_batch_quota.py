"""Regression coverage for quota rejection without destructive output cleanup."""
import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('batch_quota_target', Path(__file__).resolve().parents[1] / 'instance-control/batch_run.py')
batch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(batch)


class BatchQuotaTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.env = patch.dict(os.environ, {
            'WORKSPACE_ROOT': str(self.root), 'BLENDER_EXPORT_MAX_SUBDIRS': '16',
            'BLENDER_EXPORT_MAX_FILES_PER_SUBDIR': '64',
            'BLENDER_EXPORT_MAX_BYTES_PER_SUBDIR': '2048',
            'BLENDER_EXPORT_MAX_BYTES_TOTAL': '10000',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        script = self.root / 'inbox/abcdefghijkl/input.py'
        script.parent.mkdir(parents=True)
        script.write_bytes(b'pass\n')
        self.request = dict(script_asset_id='inbox/abcdefghijkl/input.py',
                            script_sha256=hashlib.sha256(script.read_bytes()).hexdigest(),
                            script_size=script.stat().st_size, output_subdir='target', timeout_seconds=10)

    def put(self, directory, name='keep.bin', data=b'KEEP'):
        path = self.root / 'exports' / directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def rejected(self, callback):
        with self.assertRaises(batch.BatchRunError) as caught:
            callback()
        self.assertEqual(caught.exception.code, 'EXPORT_QUOTA_EXCEEDED')
        return caught.exception

    def test_83_directories_reject_new_and_reused_without_deletion(self):
        for i in range(82):
            self.put(f'old-{i}')
        old = self.put('target')
        for target in ['new', 'target']:
            with self.subTest(target=target):
                self.rejected(lambda: batch._check_exports_quota(self.root, target, pre_run=True))
                self.assertEqual(old.read_bytes(), b'KEEP')
                self.assertEqual(len(list((self.root / 'exports').iterdir())), 83)

    def test_post_check_preserves_existing_directory_on_count_failure(self):
        for i in range(16):
            self.put(f'old-{i}')
        old = self.put('target')
        self.rejected(lambda: batch._check_exports_quota(self.root, 'target', pre_run=False))
        self.assertEqual(old.read_bytes(), b'KEEP')

    def test_post_file_limit_preserves_outputs_and_reports_counts(self):
        for i in range(65):
            self.put('target', f'{i}.bin')
        error = self.rejected(lambda: batch._check_exports_quota(self.root, 'target', pre_run=False))
        self.assertEqual(error.data['file_count'], 65)
        self.assertEqual(len(list((self.root / 'exports/target').iterdir())), 65)

    def test_existing_bytes_counted_unless_explicitly_cleaning(self):
        old = self.put('target', data=b'x' * 10000)
        self.rejected(lambda: batch._check_exports_quota(self.root, 'target', pre_run=True))
        batch._check_exports_quota(self.root, 'target', pre_run=True, clean_output=True)
        self.assertEqual(old.stat().st_size, 10000)

    def test_exact_directory_limit_allows_reuse_but_not_new_directory(self):
        for i in range(15):
            self.put(f'old-{i}')
        self.put('target')
        batch._check_exports_quota(self.root, 'target', pre_run=True)
        batch._check_exports_quota(self.root, 'target', pre_run=False)
        self.rejected(lambda: batch._check_exports_quota(self.root, 'new', pre_run=True))

    def test_run_rejects_before_explicit_cleanup_or_log_truncation(self):
        for i in range(16):
            self.put(f'old-{i}')
        old = self.put('target')
        log = self.put('target', 'build.log', b'OLD LOG')
        with patch.object(batch.subprocess, 'Popen') as spawn:
            self.rejected(lambda: batch.run({**self.request, 'clean_output_subdir': True}))
            spawn.assert_not_called()
        self.assertEqual(old.read_bytes(), b'KEEP')
        self.assertEqual(log.read_bytes(), b'OLD LOG')
        self.assertFalse(batch.BATCH_LOCK.locked())

    def test_valid_run_retains_old_files_without_cleanup(self):
        old = self.put('target')
        with patch.object(batch.subprocess, 'Popen') as spawn:
            spawn.return_value.wait.return_value = 0
            result = batch.run(self.request)
        self.assertEqual(old.read_bytes(), b'KEEP')
        self.assertIn('keep.bin', [f['name'] for f in result['files']])
        self.assertFalse(batch.BATCH_LOCK.locked())

    def test_explicit_cleanup_still_executes_after_valid_preflight(self):
        old = self.put('target')
        def start(*args, **kwargs):
            self.assertFalse(old.exists())
            class Process:
                def wait(self, **kwargs):
                    return 0
            return Process()
        with patch.object(batch.subprocess, 'Popen', side_effect=start):
            result = batch.run({**self.request, 'clean_output_subdir': True})
        self.assertEqual([f['name'] for f in result['files']], ['build.log'])


if __name__ == '__main__':
    unittest.main()
