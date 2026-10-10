import hashlib
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'file-agent'))
from workspace_management import WorkspaceManager, PreparationError
from workspace import WorkspaceError
import importlib.util

spec = importlib.util.spec_from_file_location('workspace_http', Path(__file__).resolve().parents[1] / 'file-agent/server.py')
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


class WorkspaceManagementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.identity = str(uuid.uuid4())
        self.manager = WorkspaceManager(self.root, self.identity, 1024)
        self.request = dict(prepare_id=str(uuid.uuid4()), instance_id=self.identity,
                            project_id=str(uuid.uuid4()), source_revision_id=str(uuid.uuid4()),
                            asset_id=None, sha256=None, size=0)

    def source(self):
        self.request.update(asset_id='inbox/abcdefghijklmnop/restore.blend', size=5,
                            sha256=hashlib.sha256(b'BLEND').hexdigest())
        path = self.root / self.request['asset_id']
        path.parent.mkdir(parents=True)
        path.write_bytes(b'BLEND')
        return path

    def assert_code(self, code, request=None):
        with self.assertRaises(PreparationError) as caught:
            self.manager.prepare(request or self.request)
        self.assertEqual(caught.exception.code, code)

    def test_empty_preparation_survives_restart_and_records_fixed_revision(self):
        first = self.manager.prepare(self.request)
        self.assertEqual(first['state'], 'prepared')
        self.assertFalse((self.root / 'project/main.blend').exists())
        restarted = WorkspaceManager(self.root, self.identity, 1024)
        self.assertEqual(restarted.prepare(self.request), first)
        self.assertEqual(restarted.status()['preparation']['source_revision_id'], self.request['source_revision_id'])

    def test_restore_replay_never_overwrites_later_edits(self):
        source = self.source()
        first = self.manager.prepare(self.request)
        target = self.root / 'project/main.blend'
        self.assertEqual(target.read_bytes(), b'BLEND')
        target.write_bytes(b'EDITED')
        source.unlink()
        self.assertEqual(WorkspaceManager(self.root, self.identity, 1024).prepare(self.request), first)
        self.assertEqual(target.read_bytes(), b'EDITED')
        self.assert_code('WORKSPACE_PREPARATION_CONFLICT', {**self.request, 'prepare_id':str(uuid.uuid4())})

    def test_recover_after_publication_without_upload(self):
        source = self.source()
        original = self.manager.write_journal
        def crash(journal, value):
            if value['state'] == 'prepared':
                raise OSError('simulated power loss')
            original(journal, value)
        with patch.object(self.manager, 'write_journal', crash), self.assertRaises(OSError):
            self.manager.prepare(self.request)
        source.unlink()
        result = WorkspaceManager(self.root, self.identity, 1024).prepare(self.request)
        self.assertEqual(result['state'], 'prepared')
        self.assertEqual((self.root / 'project/main.blend').read_bytes(), b'BLEND')

    def test_unowned_or_conflicting_target_is_never_overwritten(self):
        self.source()
        target = self.root / 'project/main.blend'
        target.parent.mkdir()
        target.write_bytes(b'USER DATA')
        self.assert_code('WORKSPACE_ALREADY_INITIALIZED')
        _, journal = self.manager.paths()
        self.manager.write_journal(journal, {**self.request, 'state':'preparing'})
        self.assert_code('WORKSPACE_CONTENT_MISMATCH')
        self.assertEqual(target.read_bytes(), b'USER DATA')

    def test_invalid_hash_capacity_identity_and_input(self):
        source = self.source()
        source.write_bytes(b'OTHER')
        self.assert_code('WORKSPACE_CONTENT_MISMATCH')
        source.write_bytes(b'BLEND')
        with patch('workspace_management.shutil.disk_usage') as disk:
            disk.return_value.free = 10
            self.assert_code('STORAGE_CAPACITY_UNAVAILABLE')
        self.assertFalse((self.root / '.verdantflare/prepare.json').exists())
        self.assert_code('INSTANCE_MISMATCH', {**self.request, 'instance_id':str(uuid.uuid4())})
        for change in ({'size':True}, {'extra':1}, {'asset_id':'../restore.blend'}, {'source_revision_id':'latest'}):
            self.assert_code('INVALID_ARGUMENT', {**self.request, **change})

    def test_concurrent_replays_have_one_result(self):
        from concurrent.futures import ThreadPoolExecutor
        self.source()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.manager.prepare(self.request), range(8)))
        self.assertTrue(all(result == results[0] for result in results))

    def test_symlink_parents_and_corrupt_journal_fail_closed(self):
        outside = self.root / 'other'
        outside.mkdir()
        (self.root / '.verdantflare').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(WorkspaceError):
            self.manager.prepare(self.request)
        (self.root / '.verdantflare').unlink()
        (self.root / '.verdantflare').mkdir()
        (self.root / '.verdantflare/prepare.json').write_text('broken')
        self.assert_code('WORKSPACE_STATE_INVALID')
        for bad in ({'state':[]}, {**self.request, 'state':'prepared', 'prepared_at':5},
                    {**self.request, 'state':'prepared', 'prepared_at':'2026-10-10'},
                    {**self.request, 'state':'preparing', 'size':False}):
            (self.root / '.verdantflare/prepare.json').write_text(json.dumps(bad))
            self.assert_code('WORKSPACE_STATE_INVALID')

    def test_stale_temporary_hardlink_is_not_truncated(self):
        self.source()
        other = self.root / 'valuable'
        other.write_bytes(b'KEEP')
        (self.root / 'project').mkdir()
        (self.root / 'project/.initial.blend').hardlink_to(other)
        self.manager.prepare(self.request)
        self.assertEqual(other.read_bytes(), b'KEEP')

    def test_measurement_zero_hardlinks_and_unavailable(self):
        self.assertEqual(self.manager.measure()['value'], 0)
        source = self.root / 'one'
        source.write_bytes(b'hello')
        (self.root / 'two').hardlink_to(source)
        self.assertEqual(self.manager.measure()['value'], 5)
        (self.root / 'link').symlink_to(source)
        metric = self.manager.measure()
        self.assertIsNone(metric['value'])
        self.assertEqual(metric['quality'], 'unavailable')

    def test_authenticated_http_prepare_status_and_private_paths(self):
        state = agent.FileState(self.root, 'test-token', 1024)
        state.workspace = self.manager
        server = agent.FileServer(('127.0.0.1', 0), state)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(method, path, body=None, headers=None):
            client = http.client.HTTPConnection(*server.server_address, timeout=3)
            try:
                client.request(method, path, body=body, headers=headers or {})
                response = client.getresponse()
                return response.status, json.loads(response.read())
            finally:
                client.close()
        auth = {'Authorization':'Bearer test-token', 'Content-Type':'application/json'}
        self.assertEqual(request('GET', '/internal/workspace/status')[0], 401)
        self.assertEqual(request('POST', '/internal/workspace/prepare', json.dumps(self.request), auth)[0], 200)
        status, result = request('GET', '/internal/workspace/status', headers=auth)
        self.assertEqual(status, 200)
        self.assertEqual(result['preparation']['state'], 'prepared')
        for path in ('.verdantflare/prepare.json', './.verdantflare/prepare.json', '%2e/.verdantflare/prepare.json', '%2e%76erdantflare/prepare.json'):
            self.assertEqual(request('GET', '/assets/' + path, headers=auth)[0], 404)
        self.assertEqual(request('POST', '/internal/workspace/prepare', '{"size":0,"size":1}', auth)[0], 400)
        self.assertEqual(request('POST', '/internal/workspace/prepare', json.dumps(self.request), {**auth,'Content-Encoding':'gzip'})[0], 400)
        client = http.client.HTTPConnection(*server.server_address, timeout=3)
        client.putrequest('GET', '/internal/workspace/status')
        client.putheader('Authorization', 'Bearer test-token')
        client.putheader('Authorization', 'Bearer test-token')
        client.endheaders()
        self.assertEqual(client.getresponse().status, 401)
        client.close()


if __name__ == '__main__':
    unittest.main()
