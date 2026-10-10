import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime/file-agent'))

def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

creation = module('creation_test_client', ROOT / 'app/creation.py')
content = module('creation_test_content', ROOT / 'app/content.py')
agent = module('creation_test_agent', ROOT / 'runtime/file-agent/server.py')


class CreationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace, self.staging = self.root / 'workspace', self.root / 'staging'
        self.workspace.mkdir()
        self.staging.mkdir()
        self.request = {k: content.uuid7() for k in ('operation_id','station_id','organization_id','user_id','project_id','instance_id','source_revision_id')}
        self.payload = b'BLENDER-fixed-project-revision'
        self.info = {'sha256':hashlib.sha256(self.payload).hexdigest(),'size':len(self.payload)}
        self.request.update(profile_id='blender-standard',source_sha256=self.info['sha256'],source_size=self.info['size'],empty_source=False)
        self.file = {'file_id':content.uuid7(),'path':'blender/main.blend','content_ref':{k:content.uuid7() for k in ('store_id','artifact_id','version_id')}}
        self.pod = content.uuid7()
        environment = patch.dict(agent.os.environ, INSTANCE_ID=self.request['instance_id'], BLENDER_POD_UID=self.pod)
        environment.start()
        self.addCleanup(environment.stop)
        self.token = 'a' * 43
        self.server = agent.FileServer(('127.0.0.1',0),agent.FileState(self.workspace,self.token,1024))
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        origin = 'http://blender-' + self.request['instance_id'].replace('-','') + '-prepare.blender-test.svc.cluster.local:48085'
        self.progress = dict(operation_id=self.request['operation_id'],instance_id=self.request['instance_id'],status='running',phase='awaiting_content',
                             workspace={'namespace':'blender-test'}, access={'file_endpoint':origin,'worker_token':self.token,'pod_uid':self.pod})
        test = self
        class Contents:
            downloads = 0
            calls = 0
            denied = False
            def open(self, subject, org, project, revision=None, require_write=False):
                test.assertEqual(revision,test.request['source_revision_id'])
                test.assertTrue(require_write)
                if self.denied:
                    raise creation.CreationError('PERMISSION_DENIED',403)
                return {'manifest':{'files':[] if test.request['empty_source'] else [test.file]}}
            def request(self, subject, org, method, path, value):
                self.calls += 1
                test.assertEqual(path,'/runtime/instances/create')
                test.assertEqual(value,test.request)
                return copy.deepcopy(test.progress)
            def download(self, subject, org, project, revision, file, path):
                self.downloads += 1
                test.assertEqual(revision,test.request['source_revision_id'])
                test.assertEqual(file,test.file)
                Path(path).write_bytes(test.payload)
                return test.info
        class Opener:
            lose_prepare = False
            calls = 0
            def open(self, request, timeout):
                self.calls += 1
                test.assertTrue(request.full_url.startswith(origin))
                test.assertEqual(request.get_header('Authorization'),'Bearer '+test.token)
                path = request.full_url[len(origin):]
                actual = urllib.request.Request('http://127.0.0.1:'+str(test.server.server_port)+path, data=request.data, headers=dict(request.headers), method=request.method)
                response = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(actual,timeout=timeout)
                if path == '/internal/workspace/prepare' and self.lose_prepare:
                    self.lose_prepare = False
                    response.read()
                    response.close()
                    raise OSError('response lost after durable preparation')
                return response
        self.contents, self.opener = Contents(), Opener()
        self.client = creation.CreationClient(self.contents,self.staging,self.opener)
        self.receipt = None

    def remember(self, value):
        self.assertEqual(set(value),{'asset_id','pod_uid'})
        self.receipt = json.loads(json.dumps(value))

    def advance(self, remember=None):
        return self.client.advance(self.request['user_id'],self.request['organization_id'],self.request,
                                   None if self.request['empty_source'] else self.file,self.receipt,remember or self.remember)

    def test_real_agent_restore_response_loss_and_no_edit_overwrite(self):
        self.opener.lose_prepare = True
        with self.assertRaisesRegex(creation.CreationError,'WORKSPACE_PREPARATION_UNAVAILABLE'):
            self.advance()
        target = self.workspace / 'project/main.blend'
        self.assertEqual(target.read_bytes(),self.payload)
        target.write_bytes(b'USER EDIT')
        self.client = creation.CreationClient(self.contents,self.staging,self.opener)
        result = self.advance()
        self.assertEqual(target.read_bytes(),b'USER EDIT')
        self.assertEqual(self.contents.downloads,1)
        self.assertNotIn(self.token,json.dumps(result))
        self.assertNotIn('access',result)
        self.assertEqual(result['phase'],'awaiting_content')

    def test_upload_receipt_commits_before_prepare_and_restart_reuses_it(self):
        def interrupted(value):
            self.remember(value)
            raise OSError('application exits after receipt commit')
        with self.assertRaises(OSError):
            self.advance(interrupted)
        self.assertFalse((self.workspace / 'project/main.blend').exists())
        self.client = creation.CreationClient(self.contents,self.staging,self.opener)
        self.advance()
        self.assertEqual(self.contents.downloads,1)
        self.assertEqual((self.workspace / 'project/main.blend').read_bytes(),self.payload)

    def test_binding_validation_precedes_any_file_agent_effect(self):
        def reject(progress):
            raise creation.CreationError('WORKSPACE_BINDING_CONFLICT',409)
        with self.assertRaisesRegex(creation.CreationError,'WORKSPACE_BINDING_CONFLICT'):
            self.client.advance(self.request['user_id'],self.request['organization_id'],self.request,
                                self.file,None,self.remember,validate_progress=reject)
        self.assertEqual(self.opener.calls,0)
        self.assertEqual(self.contents.downloads,0)
        self.progress['workspace']['namespace'] = 42
        with self.assertRaisesRegex(creation.CreationError,'RUNTIME_RESPONSE_INVALID'):
            self.advance()
        self.assertEqual(self.opener.calls,0)

    def test_application_coordinator_handoff_with_real_file_agent(self):
        application_module = module('creation_coordinator_app', ROOT / 'app/server.py')
        original = self.contents
        test = self
        class Contents:
            def request(self, subject, org, method, path, value):
                if path == '/runtime/options':
                    return {'station_id':test.request['station_id'], 'organization_id':org,'user_id':subject,'can_manage':True,
                            'profiles':[{'profile_id':'blender-standard','storage_reserved_bytes':4096,'max_file_bytes':1024}]}
                return original.request(subject,org,method,path,value)
            def open(self, subject, org, project, revision=None, require_write=False):
                return original.open(subject,org,project,revision or test.request['source_revision_id'],require_write)
            def describe(self, *args):
                return test.info
            def download(self, *args):
                return original.download(*args)
        config = self.root/'instances.json'; config.write_text('{"instances":{}}')
        application = application_module.Application(config,self.root/'application.sqlite','test-app-token',Contents())
        self.addCleanup(application.store.db.close)
        application.management.enabled = True
        application.management.new_id = lambda: self.request['instance_id']
        application.directory.new_id = lambda: self.request['operation_id']
        application.management.client.opener = self.opener
        self.progress['workspace'].update({k:self.request[k] for k in ('instance_id','station_id','organization_id','project_id','profile_id')})
        self.progress['workspace'].update(create_operation_id=self.request['operation_id'],pool='standard',pvc_name='pvc',pvc_uid=content.uuid7(),
                                          pv_name='pv',pv_uid=content.uuid7(),node_name='test-node',reserved_bytes=4096,retained=False)
        admitted = application.management.create(self.request['user_id'],self.request['organization_id'],
            {'name':'Integration','project_id':self.request['project_id'],'source_revision_id':self.request['source_revision_id'],
             'profile_id':'blender-standard','idempotency_key':'integration-create'})
        self.opener.lose_prepare = True
        application.management.recover_once()
        self.assertEqual((self.workspace/'project/main.blend').read_bytes(),self.payload)
        receipt = application.directory.upload_receipt(admitted['operation_id'])
        self.assertEqual(receipt['pod_uid'],self.pod)
        # Recover the actual application ledger after a lost prepare response.
        application.store.db.close()
        application = application_module.Application(config,self.root/'application.sqlite','test-app-token',Contents())
        self.addCleanup(application.store.db.close)
        application.management.enabled = True
        application.management.client.opener = self.opener
        application.management.recover_once()
        self.assertEqual(self.contents.downloads,1)
        self.progress.pop('access')
        self.progress.update(phase='stopped',status='succeeded')
        application.management.recover_once()
        result = application.management.operation(self.request['user_id'],self.request['organization_id'],admitted['operation_id'])
        self.assertEqual((result['state'],result['instance_state']),('completed','stopped'))
        self.assertNotIn(self.token,json.dumps(result))
        with application.store.transaction() as db:
            baseline = dict(db.execute('SELECT * FROM working_copies').fetchone())
        self.assertEqual(baseline['revision'],self.request['source_revision_id'])
        self.assertEqual(baseline['file_id'],self.file['file_id'])

    def test_empty_source_and_terminal_runtime_progress(self):
        self.request.update(empty_source=True,source_sha256='',source_size=0)
        result = self.advance()
        self.assertFalse((self.workspace / 'project/main.blend').exists())
        self.assertEqual(self.server.state.workspace.status()['preparation']['state'],'prepared')
        self.progress.update(status='succeeded',phase='stopped')
        del self.progress['access']
        before = self.opener.calls
        self.assertEqual(self.advance()['phase'],'stopped')
        self.assertEqual(self.opener.calls,before)

    def test_revocation_and_endpoint_or_pod_mismatch_stop_handoff(self):
        self.contents.denied = True
        with self.assertRaisesRegex(creation.CreationError,'PERMISSION_DENIED'):
            self.advance()
        self.assertEqual(self.contents.calls,0)
        self.contents.denied = False
        original = self.progress['access']['file_endpoint']
        self.progress['access']['file_endpoint'] = 'http://untrusted.example'
        with self.assertRaisesRegex(creation.CreationError,'RUNTIME_RESPONSE_INVALID'):
            self.advance()
        self.assertEqual(self.opener.calls,0)
        self.progress['access']['file_endpoint'] = original
        self.progress['access']['pod_uid'] = content.uuid7()
        with self.assertRaisesRegex(creation.CreationError,'WORKSPACE_BINDING_CONFLICT'):
            self.advance()
        self.assertEqual(self.contents.downloads,0)


if __name__ == '__main__':
    unittest.main()
