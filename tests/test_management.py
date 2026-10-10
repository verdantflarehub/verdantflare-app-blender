import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

spec = importlib.util.spec_from_file_location('management_test_app', Path(__file__).parents[1] / 'app/server.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'instances.json'
        self.config.write_text('{"instances":{}}')
        self.subject, self.org, self.station, self.project, self.revision = (app.content.uuid7() for _ in range(5))
        self.file = {'path': 'blender/main.blend', 'file_id': app.content.uuid7(),
                     'content_ref': {k: app.content.uuid7() for k in ('store_id', 'artifact_id', 'version_id')}}
        self.request = {'name':'Blender Test','project_id':self.project,'source_revision_id':self.revision,
                        'profile_id':'standard','idempotency_key':'creation-key-001'}
        self.manager, self.writable, self.empty = True, True, False
        test = self
        class Contents:
            def request(self, subject, org, method, path, value):
                if path == '/runtime/instances/start':
                    test.assertEqual((subject, org, method), (test.subject, test.org, 'POST'))
                    return test.start_driver(value)
                test.assertEqual((subject,org,method,path,value), (test.subject,test.org,'POST','/runtime/options',{}))
                if not test.manager:
                    raise app.content.ContentError('PERMISSION_DENIED',403)
                return {'station_id':test.station,'user_id':subject,'organization_id':org,'can_manage':True,
                        'profiles':[{'profile_id':'standard','storage_reserved_bytes':4096,'max_file_bytes':1024}]}
            def open(self, subject, org, project, revision=None, require_write=False):
                test.assertTrue(require_write)
                if not test.writable:
                    raise app.content.ContentError('PERMISSION_DENIED',403)
                test.assertEqual(project,test.project)
                test.assertIn(revision,(None,test.revision))
                return {'project_id':project,'revision_id':test.revision,'manifest':{'files':[] if test.empty else [test.file]}}
            def describe(self, subject, org, project, revision, file):
                test.assertEqual(file,test.file)
                return {'sha256':'a'*64,'size':128,'path':'/private-artifact-path'}
        self.contents = Contents()
        self.apps = []
        self.restart()
        self.addCleanup(self.close_apps)
        self.calls, self.stage, self.receipts = [], {}, {}
        self.bad_binding, self.lose_response = False, False
        self.replace_volume = False
        class Driver:
            def advance(self, subject, org, request, file, receipt, remember, validate_progress=None):
                # Prove the application accepted durably before the first Runtime effect.
                op = test.application.directory.operation(request['operation_id'],org,subject)
                test.assertEqual(op['instance'],request['instance_id'])
                test.assertEqual(request['source_revision_id'],test.revision)
                if not test.writable:
                    raise app.content.ContentError('PERMISSION_DENIED',403)
                test.calls.append(copy.deepcopy(request))
                phase = test.stage.get(op['id'],'accepted')
                binding = {k:request[k] for k in ('station_id','organization_id','project_id','instance_id','profile_id')}
                binding.update(create_operation_id=op['id'],namespace='blender',pool='standard',pvc_name='private-claim',pvc_uid='private-pvc-uid',
                               pv_name='private-volume',pv_uid='private-pv-uid',node_name='private-node',reserved_bytes=4096,retained=False)
                if test.bad_binding:
                    binding['project_id'] = app.content.uuid7()
                if test.replace_volume:
                    binding['pvc_uid'] = 'replaced-volume-uid'
                result = {'operation_id':op['id'],'instance_id':op['instance'],'phase':phase,
                          'status':'succeeded' if phase=='stopped' else 'running'}
                if phase != 'accepted':
                    result['workspace'] = binding
                validate_progress(result)
                if phase == 'awaiting_content' and file:
                    if receipt is None:
                        receipt = {'asset_id':'inbox/abcdefghijklmnop/restore.blend','pod_uid':app.content.uuid7()}
                        remember(receipt)
                        test.receipts[op['id']] = receipt
                    else:
                        test.assertEqual(receipt,test.receipts[op['id']])
                test.stage[op['id']] = {'accepted':'reserved','reserved':'awaiting_content','awaiting_content':'removing','removing':'stopped','stopped':'stopped'}[phase]
                if test.lose_response:
                    test.lose_response = False
                    raise app.creation.CreationError('WORKSPACE_PREPARATION_UNAVAILABLE')
                return result
        self.driver = Driver()
        self.application.management.client = self.driver

    def close_apps(self):
        for application in self.apps:
            application.management.stop_event.set()
            application.store.db.close()

    def restart(self):
        if self.apps:
            self.apps.pop().store.db.close()
        self.application = app.Application(self.config,self.root/'state.sqlite','internal-test-token',self.contents)
        self.application.management.enabled = True
        if hasattr(self,'driver'):
            self.application.management.client = self.driver
        self.apps.append(self.application)

    def create(self, request=None):
        return self.application.management.create(self.subject,self.org,request or self.request)

    def tick(self):
        self.application.management.recover_once()

    def created_instance(self):
        created = self.create()
        for _ in range(5):
            self.tick()
        item = self.application.instances()[created['alias']]
        self.assertEqual(item['_directory']['state'], 'stopped')
        return created['alias'], item

    def start_driver(self, request):
        command = self.application.directory.operation(request['operation_id'], self.org, self.subject)
        self.assertEqual(command['action'], 'start')
        self.assertEqual(command['instance'], request['instance_id'])
        self.assertEqual((request['source_revision_id'], request['sha256'], request['size'], request['empty']), (self.revision, 'a'*64, 128, False))
        payload = json.loads(command['input'])
        self.assertEqual(request['create_operation_id'], payload['create_operation_id'])
        self.start_calls.append(copy.deepcopy(request))
        phase = self.start_phase
        self.start_phase = {'accepted':'launching', 'launching':'loading', 'loading':'running', 'running':'running'}[phase]
        result = {'operation_id':command['id'], 'instance_id':command['instance'], 'status':'succeeded' if phase == 'running' else 'running',
                  'phase':phase, 'workspace':payload['workspace']}
        if phase != 'accepted':
            namespace = payload['workspace']['namespace']
            name = 'blender-run-' + command['id'].replace('-', '')
            base = 'http://' + name + '.' + namespace + '.svc.cluster.local:'
            result['worker'] = {'pod_uid':self.start_pod, 'service_uid':self.start_service, 'pod_name':name, 'namespace':namespace,
                                'control_endpoint':base+'48084', 'file_endpoint':base+'48085', 'gui_endpoint':base+'48083', 'pod_ready':phase in {'loading','running'}}
            if phase == 'running':
                startup = {k:request[k] for k in ('source_revision_id','sha256','size','empty','create_operation_id','instance_id','project_id')}
                startup.update(start_operation_id=command['id'], pod_uid=self.start_pod)
                result['proof'] = {'startup':startup, 'generation':'b'*32, 'gpu_uuid':'GPU-'+app.content.uuid7()}
        if getattr(self, 'bad_start_proof', False):
            result.pop('proof', None)
        # Unexpected private fields must never enter the directory or browser response.
        result['worker_token'] = 'must-not-persist-secret'
        if 'worker' in result:
            result['worker']['gui_password'] = 'must-not-persist-gui'
        if getattr(self, 'lose_start_response', False):
            self.lose_start_response = False
            raise app.content.ContentError('CONTENT_SERVICE_UNAVAILABLE', 503)
        return result

    def start_ready(self):
        alias, item = self.created_instance()
        self.start_calls, self.start_phase = [], 'accepted'
        self.start_pod, self.start_service = app.content.uuid7(), app.content.uuid7()
        request = {'expected_version':item['_directory']['version'], 'idempotency_key':'start-key-001'}
        result = self.application.management.start_instance(self.subject, self.org, alias, request)
        self.assertEqual(self.start_calls, [])
        self.assertEqual(result['instance_state'], 'starting')
        return alias, request, result

    def test_start_persistent_recovery_and_loaded_proof_only(self):
        alias, request, started = self.start_ready()
        self.tick(); self.tick()
        self.restart()
        replay = self.application.management.start_instance(self.subject, self.org, alias, request)
        self.assertEqual(replay['operation_id'], started['operation_id'])
        self.tick()
        self.assertEqual(self.application.instances()[alias]['_directory']['state'], 'starting')
        self.bad_start_proof = True
        self.tick()
        self.assertEqual(self.application.instances()[alias]['_directory']['state'], 'starting')
        self.bad_start_proof = False
        self.tick()
        item = self.application.instances()[alias]
        self.assertEqual(item['_directory']['state'], 'running')
        self.assertEqual(item['execution_binding']['worker']['pod_uid'], self.start_pod)
        self.assertEqual(item['execution_binding']['proof']['generation'], 'b'*32)
        with self.application.store.transaction() as db:
            stored = '\n'.join(str(tuple(row)) for table in ('instance_directory','instance_commands','instance_events') for row in db.execute('SELECT * FROM '+table))
        self.assertNotIn('must-not-persist', stored)
        public = self.application.management.operation(self.subject, self.org, started['operation_id'])
        self.assertEqual(public['instance_state'], 'running')
        self.assertNotIn('worker', json.dumps(public))

    def test_start_revocation_unknown_response_and_binding_replacement(self):
        alias, request, started = self.start_ready()
        self.manager = False
        self.tick()
        self.assertEqual(self.start_calls, [])
        self.manager = True
        self.writable = False
        self.tick()
        self.assertEqual(self.start_calls, [])
        self.writable = True
        self.lose_start_response = True
        self.tick()
        self.restart()
        self.tick()
        bound = self.application.instances()[alias]['execution_binding']['worker']['pod_uid']
        self.start_pod = app.content.uuid7()
        self.tick()
        item = self.application.instances()[alias]
        self.assertEqual(item['_directory']['state'], 'starting')
        self.assertEqual(item['execution_binding']['worker']['pod_uid'], bound)
        self.assertEqual(item['_directory']['evidence']['code'], 'WORKSPACE_BINDING_CONFLICT')
        self.assertTrue(all(call['operation_id'] == started['operation_id'] for call in self.start_calls))

    def test_start_strict_input_version_and_separate_management_acl(self):
        alias, item = self.created_instance()
        request = {'expected_version':item['_directory']['version'], 'idempotency_key':'start-key-002'}
        for update in ({'expected_version':True}, {'endpoint':'http://untrusted'}, {'idempotency_key':'short'}):
            with self.assertRaisesRegex(app.management.ManagementError, 'INVALID_ARGUMENT'):
                self.application.management.start_instance(self.subject,self.org,alias,dict(request,**update))
        with self.assertRaisesRegex(app.directory.Fault,'INSTANCE_STATE_CONFLICT'):
            self.application.management.start_instance(self.subject,self.org,alias,dict(request,expected_version=1))
        with self.application.store.transaction() as db:
            config = json.loads(db.execute('SELECT config FROM instance_directory WHERE id=?',(item['id'],)).fetchone()[0])
            config['managers'] = []
            db.execute('UPDATE instance_directory SET config=? WHERE id=?',(json.dumps(config),item['id']))
        with self.assertRaisesRegex(app.management.ManagementError,'PERMISSION_DENIED'):
            self.application.management.start_instance(self.subject,self.org,alias,request)

    def test_admission_permissions_and_strict_fields_before_effects(self):
        for change in ({'name':'\ninvalid'}, {'idempotency_key':'short'}, {'endpoint':'http://arbitrary'}, {'source_revision_id':'latest'}):
            with self.assertRaisesRegex(app.management.ManagementError,'INVALID_ARGUMENT'):
                self.create(dict(self.request,**change))
        self.manager = False
        with self.assertRaisesRegex(app.content.ContentError,'PERMISSION_DENIED'):
            self.create()
        self.manager, self.writable = True, False
        with self.assertRaisesRegex(app.content.ContentError,'PERMISSION_DENIED'):
            self.create()
        self.assertEqual(self.application.directory.entries(),{})
        self.assertEqual(self.calls,[])

    def test_creation_restart_receipt_and_atomic_working_copy(self):
        first = self.create()
        self.assertEqual(first['instance_state'],'creating')
        self.assertEqual(self.calls,[])
        self.tick(); self.tick()
        self.lose_response = True
        self.tick()
        receipt = self.application.directory.upload_receipt(first['operation_id'])
        self.assertEqual(set(receipt),{'asset_id','pod_uid'})
        self.restart()
        # A replayed create returns the original instance/operation after restart.
        replay = self.create()
        self.assertEqual((replay['operation_id'],replay['instance_id']),(first['operation_id'],first['instance_id']))
        self.tick()
        # Force failure of the baseline insert: the final state must roll back too.
        with self.application.store.transaction() as db:
            db.execute("CREATE TRIGGER reject_baseline BEFORE INSERT ON working_copies BEGIN SELECT RAISE(ABORT,'simulated disk failure'); END")
        self.tick()
        operation = self.application.directory.operation(first['operation_id'],self.org,self.subject)
        self.assertEqual(operation['state'],'running')
        with self.application.store.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM working_copies').fetchone()[0],0)
            db.execute('DROP TRIGGER reject_baseline')
        self.tick()
        result = self.application.management.operation(self.subject,self.org,first['operation_id'])
        self.assertEqual((result['state'],result['instance_state']),('completed','stopped'))
        with self.application.store.transaction() as db:
            baseline = db.execute('SELECT * FROM working_copies').fetchone()
        self.assertEqual((baseline['instance'],baseline['project'],baseline['revision'],baseline['file_id']),
                         (first['instance_id'],self.project,self.revision,self.file['file_id']))
        count = len(self.calls)
        self.tick()
        self.assertEqual(len(self.calls),count)
        public = json.dumps(result)
        for forbidden in ('private-', 'workspace', 'binding', 'source_sha256', 'receipt', 'token'):
            self.assertNotIn(forbidden,public)
        self.assertEqual(len({json.dumps(r,sort_keys=True) for r in self.calls}),1)

    def test_revoke_and_binding_conflict_keep_original_pending_operation(self):
        first = self.create()
        self.manager = False
        self.tick()
        self.assertEqual(self.calls,[])
        op = self.application.directory.operation(first['operation_id'],self.org,self.subject)
        self.assertEqual(json.loads(op['result'])['evidence']['code'],'PERMISSION_DENIED')
        self.manager = True
        self.tick()
        self.bad_binding = True
        self.tick()
        self.assertIsNone(self.application.directory.upload_receipt(first['operation_id']))
        op = self.application.directory.operation(first['operation_id'],self.org,self.subject)
        self.assertEqual((op['state'],op['phase']),('running','accepted'))
        self.assertEqual(json.loads(op['result'])['evidence']['code'],'WORKSPACE_BINDING_CONFLICT')
        self.bad_binding = False
        for _ in range(4): self.tick()
        self.assertEqual(self.application.directory.pending(),[])

    def test_concurrent_replays_only_admit_one_instance(self):
        results, failures = [], []
        def create():
            try: results.append(self.create())
            except Exception as exc: failures.append(type(exc).__name__)
        threads = [threading.Thread(target=create) for _ in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(failures,[])
        self.assertEqual(len({r['operation_id'] for r in results}),1)
        self.assertEqual(len(self.application.directory.entries()),1)
        with self.assertRaisesRegex(app.management.ManagementError,'IDEMPOTENCY_CONFLICT'):
            self.create(dict(self.request,name='Changed'))
        with self.assertRaisesRegex(app.directory.Fault,'INSTANCE_NAME_CONFLICT'):
            self.create(dict(self.request,idempotency_key='different-key',name='blender test'))

    def test_project_selection_fixed_source_and_idempotent_project_creation(self):
        original = self.contents.request
        commits, calls = {}, []
        def request(subject,org,method,path,value):
            if path == '/runtime/options': return original(subject,org,method,path,value)
            calls.append((path,copy.deepcopy(value)))
            if path == '/project/list':
                self.assertEqual(value, {'require_write':True,'limit':50})
                return {'items':[{'project_id':self.project,'head_revision_id':self.revision,'name':'Test','private':'secret'}],'next_cursor':''}
            self.assertEqual(path,'/project/create')
            self.assertEqual(set(value),{'name','commit_id','category','entry_path','entry_text'})
            self.assertEqual((value['category'],value['entry_path']),('blender','README.md'))
            return commits.setdefault(value['commit_id'],{'project_id':self.project,'revision_id':self.revision})
        self.contents.request = request
        selected = self.application.management.projects(self.subject,self.org,{})
        self.assertEqual(set(selected['items'][0]),{'project_id','head_revision_id','name'})
        info = self.application.management.source(self.subject,self.org,{'project_id':self.project,'source_revision_id':self.revision})
        self.assertEqual((info['kind'],info['size']),('restore',128))
        self.assertNotIn('path',info)
        self.writable = False
        with self.assertRaisesRegex(app.content.ContentError,'PERMISSION_DENIED'):
            self.application.management.source(self.subject,self.org,{'project_id':self.project,'source_revision_id':self.revision})
        self.writable, self.empty = True, True
        payload = {'name':'New Project','commit_id':app.content.uuid7()}
        first = self.application.management.new_project(self.subject,self.org,payload)
        self.assertEqual(first['kind'],'empty')
        self.assertEqual(self.application.management.new_project(self.subject,self.org,payload),first)
        self.assertEqual(len(commits),1)
        self.manager = False
        before = len(calls)
        with self.assertRaisesRegex(app.content.ContentError,'PERMISSION_DENIED'):
            self.application.management.new_project(self.subject,self.org,payload)
        self.assertEqual(len(calls),before)

    def test_replaced_volume_is_rejected_before_upload(self):
        first = self.create()
        self.tick(); self.tick()
        self.replace_volume = True
        self.tick()
        self.assertEqual(self.receipts,{})
        self.assertIsNone(self.application.directory.upload_receipt(first['operation_id']))
        op = self.application.directory.operation(first['operation_id'],self.org,self.subject)
        self.assertEqual(json.loads(op['result'])['evidence']['code'],'WORKSPACE_BINDING_CONFLICT')

    def test_empty_source_and_acl_revocation(self):
        self.empty = True
        first = self.create()
        with self.application.store.transaction() as db:
            row = db.execute('SELECT config FROM instance_directory WHERE id=?',(first['instance_id'],)).fetchone()
            config = json.loads(row['config']); config['managers'] = []
            db.execute('UPDATE instance_directory SET config=?,version=version+1 WHERE id=?',(json.dumps(config),first['instance_id']))
        self.tick()
        self.assertEqual(self.calls,[])
        op = self.application.directory.operation(first['operation_id'],self.org,self.subject)
        self.assertEqual(json.loads(op['result'])['evidence']['code'],'PERMISSION_DENIED')
        with self.application.store.transaction() as db:
            config['managers'] = [self.subject]
            db.execute('UPDATE instance_directory SET config=?,version=version+1 WHERE id=?',(json.dumps(config),first['instance_id']))
        for _ in range(5): self.tick()
        with self.application.store.transaction() as db:
            self.assertEqual(db.execute('SELECT file_id FROM working_copies').fetchone()[0],'')
        self.assertTrue(self.calls[-1]['empty_source'])
        self.assertEqual(self.receipts,{})

    def test_real_http_auth_body_and_public_progress(self):
        server = app.Server(('127.0.0.1',0),self.application)
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        def call(method,path,body=None,token='internal-test-token',org=None):
            headers = {'Authorization':'Bearer '+token,'X-User-Id':self.subject,'X-Organization-Id':org or self.org,'Content-Type':'application/json'}
            request = urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+path,data=body,headers=headers,method=method)
            try: response = urllib.request.urlopen(request)
            except urllib.error.HTTPError as exc: response = exc
            with response: return response.status,json.loads(response.read())
        self.assertEqual(call('GET','/internal/instance-options',token='browser-token')[0],401)
        self.assertEqual(call('POST','/internal/instances',b'{"name":"one","name":"two"}')[0],400)
        self.assertEqual(call('POST','/internal/instances?target=other',b'{}')[0],404)
        status, result = call('POST','/internal/instances',json.dumps(self.request).encode())
        self.assertEqual(status,202)
        self.assertEqual(call('GET','/internal/instance-operations/'+result['operation_id'],org=app.content.uuid7())[0],404)
        self.assertEqual(call('GET','/internal/instance-operations/'+result['operation_id'])[1]['operation_id'],result['operation_id'])
        for _ in range(5): self.tick()
        item = self.application.instances()[result['alias']]
        start_path = '/internal/instances/'+result['alias']+'/start'
        start_body = json.dumps({'expected_version':item['_directory']['version'], 'idempotency_key':'http-start-key'}).encode()
        self.assertEqual(call('POST',start_path,start_body,token='browser-token')[0],401)
        self.assertEqual(call('POST',start_path+'?other=true',start_body)[0],404)
        self.assertEqual(call('POST',start_path,b'{"expected_version":1,"expected_version":2}')[0],400)
        code, started = call('POST',start_path,start_body)
        self.assertEqual(code,202)
        self.assertEqual(started['action'],'start')
        self.application.management.enabled = False
        self.assertEqual(call('POST','/internal/instances',json.dumps(self.request).encode())[0],503)
        self.assertEqual(call('GET','/internal/instance-options')[0],503)


if __name__ == '__main__':
    unittest.main()
