import copy
import hashlib
import io
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import types
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
                if path == '/runtime/instances/access':
                    test.assertEqual((subject, org, method), (test.subject, test.org, 'POST'))
                    test.access_requests.append(copy.deepcopy(value))
                    if getattr(test, 'access_effect', None):
                        test.access_effect()
                    return copy.deepcopy(test.access_response)
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


    def test_start_capability_requires_current_manager_and_completed_dynamic_creation(self):
        created = self.create()
        alias = created['alias']
        self.assertNotIn('start', self.application.instance_view(alias, self.subject, self.org)['allowed_actions'])
        for _ in range(5):
            self.tick()
        view = self.application.instance_view(alias, self.subject, self.org)
        self.assertIn('start', view['allowed_actions'])
        self.assertTrue(view['lifecycle_available'])
        self.manager = False
        self.assertNotIn('start', self.application.instance_view(alias, self.subject, self.org)['allowed_actions'])
        self.manager = True
        self.application.management.enabled = False
        self.assertNotIn('start', self.application.instance_view(alias, self.subject, self.org)['allowed_actions'])
        self.application.management.enabled = True
        self.application.management.start_instance(self.subject, self.org, alias,
            {'expected_version':view['state_version'], 'idempotency_key':'capability-start-key'})
        self.assertNotIn('start', self.application.instance_view(alias, self.subject, self.org)['allowed_actions'])

    def access_ready(self):
        alias, _, _ = self.start_ready()
        for _ in range(4):
            self.tick()
        item = self.application.authorize(alias, self.subject, self.org)
        self.assertEqual(item['_directory']['state'], 'running')
        self.access_requests = []
        self.access_response = {'instance_id': item['id'], 'start_operation_id': item['execution_binding']['start_operation_id'],
                                'kind': 'worker', 'worker': copy.deepcopy(item['execution_binding']['worker']), 'credential': 'w'*43}
        return alias, item

    def test_dynamic_access_is_scoped_ephemeral_and_survives_app_restart(self):
        alias, item = self.access_ready()
        self.manager = False  # Existing editors do not need the manager-only options endpoint.
        self.assertEqual(self.application.credential(item), 'w'*43)
        request = self.access_requests[-1]
        self.assertEqual(request, {'station_id': self.station, 'organization_id': self.org, 'user_id': self.subject,
                                  'project_id': self.project, 'instance_id': item['id'],
                                  'start_operation_id': item['execution_binding']['start_operation_id'], 'kind': 'worker'})
        self.access_response.update(kind='gui', credential='g'*43)
        self.assertEqual(self.application.credential(item, 'gui'), 'g'*43)
        self.restart()
        self.assertEqual(self.application.credential(self.application.authorize(alias, self.subject, self.org), 'gui'), 'g'*43)
        with self.application.store.transaction() as db:
            stored = '\n'.join(db.iterdump())
        self.assertNotIn('w'*43, stored)
        self.assertNotIn('g'*43, stored)

    def test_dynamic_access_rejects_rebinding_revocation_and_stale_state(self):
        alias, item = self.access_ready()
        good = copy.deepcopy(self.access_response)
        for change in [lambda r:r.update(instance_id=app.content.uuid7()),
                       lambda r:r.update(start_operation_id=app.content.uuid7()),
                       lambda r:r.update(kind='gui'), lambda r:r.update(credential='bad'),
                       lambda r:r['worker'].update(pod_uid=app.content.uuid7()),
                       lambda r:r['worker'].update(service_uid=app.content.uuid7()),
                       lambda r:r['worker'].update(pod_ready=False),
                       lambda r:r['worker'].update(file_endpoint='http://other.invalid:48085')]:
            self.access_response = copy.deepcopy(good);change(self.access_response)
            with self.assertRaisesRegex(app.Error, 'WORKSPACE_BINDING_CONFLICT'):
                self.application.credential(item)
        self.access_response = good
        self.access_effect = lambda:setattr(self, 'writable', False)
        with self.assertRaisesRegex(app.Error, 'PERMISSION_DENIED'):
            self.application.credential(item)
        self.writable = True
        self.access_effect = lambda:self.application.store.db.execute("UPDATE instance_directory SET version=version+1 WHERE id=?", (item['id'],))
        with self.assertRaisesRegex(app.Error, 'INSTANCE_STATE_CONFLICT'):
            self.application.credential(item)
        self.access_effect = None
        item = self.application.authorize(alias, self.subject, self.org)
        with self.application.store.transaction() as db:
            db.execute("UPDATE instance_directory SET state='stopping',operation='pending-stop' WHERE id=?", (item['id'],))
        item = self.application.authorize(alias, self.subject, self.org)
        before = len(self.access_requests)
        with self.assertRaisesRegex(app.Error, 'INSTANCE_ADMISSION_CLOSED'):
            self.application.credential(item)
        self.assertEqual(len(self.access_requests), before)

    def test_dynamic_generation_requires_the_authenticated_pod_binding(self):
        import io
        from unittest.mock import Mock
        _, item = self.access_ready()
        worker = item['execution_binding']['worker']
        value = {'status': 'ready', 'instance_id': item['id'], 'generation': 'b'*32,
                 'resource_binding': {k:worker[k] for k in ('pod_uid', 'pod_name', 'namespace')}}
        value['resource_binding']['container'] = 'blender'
        self.application.opener = Mock()
        self.application.opener.open.return_value = io.BytesIO(json.dumps(value).encode())
        self.assertEqual(self.application.generation(item), 'b'*32)
        request = self.application.opener.open.call_args.args[0]
        self.assertTrue(request.full_url.endswith('/internal/status'))
        self.assertEqual(request.get_header('Authorization'), 'Bearer '+'w'*43)
        value['resource_binding']['pod_uid'] = app.content.uuid7()
        self.application.opener.open.return_value = io.BytesIO(json.dumps(value).encode())
        with self.assertRaisesRegex(app.Error, 'WORKER_IDENTITY_MISMATCH'):
            self.application.generation(item)

    def stop_ready(self):
        alias,item = self.access_ready()
        self.stop_item,self.stop_alias = item,alias
        self.stop_bytes = b'real-checkpoint-fixture'
        self.stop_proof = {'asset_id':'checkpoints/checkpoint-4-'+'c'*32+'.blend',
                           'sha256':hashlib.sha256(self.stop_bytes).hexdigest(),'size':len(self.stop_bytes),'scene_version':4}
        self.stop_ref = {k:app.content.uuid7() for k in ('store_id','artifact_id','version_id')}
        self.saved_revision = app.content.uuid7()
        self.drain_result = None
        self.drain_calls,self.commit_calls,self.stop_calls,self.upload_calls = [],[],[],[]
        original = self.contents.request
        def request(subject,org,method,path,value):
            if path == '/project/commit':
                self.commit_calls.append(copy.deepcopy(value))
                self.assertEqual(value['expected_revision_id'],self.revision)
                self.assertEqual(value['changes']['upsert_files'][0]['content_ref'],self.stop_ref)
                if getattr(self,'save_conflict',False):
                    raise app.content.ContentError('REVISION_CONFLICT',409)
                if getattr(self,'lose_commit',False):
                    self.lose_commit=False
                    raise app.content.ContentError('CONTENT_SERVICE_UNAVAILABLE',503)
                return {'project_id':self.project,'revision_id':self.saved_revision,
                        'manifest':{'files':[dict(self.file,content_ref=self.stop_ref)]}}
            if path == '/runtime/instances/stop':
                self.stop_calls.append(copy.deepcopy(value))
                self.assertEqual(value['saved_revision_id'],self.saved_revision)
                self.assertEqual(value['commit_id'],self.commit_calls[0]['commit_id'])
                self.assertEqual({k:value[k] for k in self.stop_proof},self.stop_proof)
                self.assertEqual({k:value[k] for k in self.stop_ref},self.stop_ref)
                self.drain_result.update(phase='sealed')
                phase = ['accepted','removing','stopped'][min(len(self.stop_calls)-1,2)]
                if getattr(self,'lose_stop',False):
                    self.lose_stop=False
                    raise app.content.ContentError('CONTENT_SERVICE_UNAVAILABLE',503)
                return {'operation_id':value['operation_id'],'instance_id':item['id'],'status':'succeeded' if phase=='stopped' else 'running',
                        'phase':phase,'workspace':item['runtime_binding'],'source':{'source_revision_id':self.revision,
                        'sha256':self.stop_proof['sha256'],'size':self.stop_proof['size'],'empty':False},'saved_revision_id':self.saved_revision}
            return original(subject,org,method,path,value)
        self.contents.request = request
        def upload(subject,org,project,write_id,path,sha,size):
            self.assertEqual(path.read_bytes(),self.stop_bytes)
            self.assertEqual((sha,size),(self.stop_proof['sha256'],len(self.stop_bytes)))
            self.upload_calls.append(write_id)
            return self.stop_ref
        self.contents.upload = upload
        self.install_stop_transport()
        return alias,{'expected_version':item['_directory']['version'],'idempotency_key':'stop-key-0001'}

    def install_stop_transport(self):
        def opened(request,timeout):
            self.assertEqual(request.get_header('Authorization'),'Bearer '+'w'*43)
            path=app.urllib.parse.urlsplit(request.full_url).path
            if path == '/internal/status':
                binding={k:self.stop_item['execution_binding']['worker'][k] for k in ('pod_uid','pod_name','namespace')}
                value={'instance_id':self.stop_item['id'],'status':'ready','generation':'b'*32,'gui_held':False,'resource_binding':dict(binding,container='blender')}
            elif path.startswith('/assets/'):
                self.assertEqual(path,'/assets/'+self.stop_proof['asset_id'])
                response=io.BytesIO(self.stop_bytes)
                response.headers={'Content-Length':str(len(self.stop_bytes)),'X-Asset-SHA256':self.stop_proof['sha256']}
                return response
            elif path == '/internal/drain':
                body=json.loads(request.data);self.drain_calls.append(body)
                command=self.application.directory.operation(body['operation_id'],self.org,self.subject)
                self.assertEqual(command['action'],'stop')
                self.assertEqual(self.application.instances()[self.stop_alias]['_directory']['state'],'stopping')
                if body['action']=='prepare':
                    self.drain_result={k:v for k,v in body.items() if k!='action'}
                    self.drain_result.update(project_id=self.project,pod_uid=self.start_pod,phase='captured',frozen=True,proof=self.stop_proof)
                    if getattr(self,'lose_drain',False):
                        self.lose_drain=False;raise OSError('response lost')
                elif body['action']=='abort':
                    self.assertNotEqual(self.drain_result['phase'],'sealed')
                    if getattr(self,'lose_abort',False):
                        self.lose_abort=False;raise OSError('abort response lost')
                    self.drain_result.update(phase='aborted',frozen=False)
                value=self.drain_result
            else:
                self.fail('unexpected worker route')
            return io.BytesIO(json.dumps(value).encode())
        self.application.opener=types.SimpleNamespace(open=opened)

    def test_stop_recovers_lost_drain_commit_and_runtime_responses(self):
        alias,request=self.stop_ready()
        command=self.application.management.stop_instance(self.subject,self.org,alias,request)
        self.assertEqual((self.drain_calls,self.stop_calls),([],[]))
        self.lose_drain=self.lose_commit=self.lose_stop=True
        for _ in range(12):
            self.tick();self.restart();self.install_stop_transport()
        item=self.application.instances()[alias]
        self.assertEqual(item['_directory']['state'],'stopped')
        self.assertEqual(self.application.store.db.execute('SELECT revision FROM working_copies WHERE instance=?',(item['id'],)).fetchone()[0],self.saved_revision)
        replay=self.application.management.stop_instance(self.subject,self.org,alias,request)
        self.assertEqual(replay['operation_id'],command['operation_id'])
        self.assertEqual(replay['state'],'completed')
        self.assertEqual(len(set(self.upload_calls)),1)
        self.assertEqual(len({json.dumps(v,sort_keys=True) for v in self.commit_calls}),1)
        self.assertEqual(len({json.dumps(v,sort_keys=True) for v in self.stop_calls}),1)
        self.assertFalse(any(v['action']=='abort' for v in self.drain_calls))
        stored='\n'.join(self.application.store.db.iterdump())
        self.assertNotIn('w'*43,stored)
        self.assertNotIn('pod_uid',json.dumps(replay))
        restarted=self.application.management.start_instance(self.subject,self.org,alias,{'expected_version':item['_directory']['version'],'idempotency_key':'restart-after-stop'})
        payload=json.loads(self.application.directory.operation(restarted['operation_id'],self.org,self.subject)['input'])
        self.assertEqual(payload['source']['sha256'],self.stop_proof['sha256'])

    def test_stop_revision_conflict_reopens_only_after_confirmed_abort(self):
        alias,request=self.stop_ready();self.save_conflict=True;self.lose_abort=True
        command=self.application.management.stop_instance(self.subject,self.org,alias,request)
        for _ in range(4):self.tick()
        item=self.application.authorize(alias,self.subject,self.org)
        self.assertEqual(item['_directory']['state'],'stopping')
        with self.assertRaisesRegex(app.Error,'INSTANCE_ADMISSION_CLOSED'):self.application.credential(item)
        self.restart();self.install_stop_transport();self.tick()
        item=self.application.authorize(alias,self.subject,self.org)
        self.assertEqual(item['_directory']['state'],'running')
        self.assertEqual(self.application.credential(item),'w'*43)
        result=self.application.management.operation(self.subject,self.org,command['operation_id'])
        self.assertEqual((result['state'],result['phase'],result['code']),('failed','aborted','REVISION_CONFLICT'))
        self.assertEqual(self.stop_calls,[])
        self.assertEqual(self.application.store.db.execute('SELECT revision FROM working_copies WHERE instance=?',(item['id'],)).fetchone()[0],self.revision)

    def test_stop_rejects_leases_saves_and_invalid_version_before_effects(self):
        alias,request=self.stop_ready();item=self.stop_item;db=self.application.store.db
        with self.assertRaisesRegex(app.management.ManagementError,'INSTANCE_STATE_CONFLICT'):
            self.application.management.stop_instance(self.subject,self.org,alias,dict(request,expected_version=1))
        for mode in ('edit','gui'):
            db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)',('held',self.subject,self.org,item['id'],self.project,'b'*32,mode,time.time()+60))
            with self.assertRaisesRegex(app.management.ManagementError,'INSTANCE_EDIT_LEASE_HELD'):
                self.application.management.stop_instance(self.subject,self.org,alias,request)
            db.execute('DELETE FROM sessions')
        db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?,?)',('saving',self.subject,item['id'],'save-key','digest','unknown',None,time.time()))
        db.execute('INSERT INTO saves VALUES (?,?)',('saving','{}'))
        with self.assertRaisesRegex(app.management.ManagementError,'SAVE_IN_PROGRESS'):
            self.application.management.stop_instance(self.subject,self.org,alias,request)
        self.assertEqual(self.drain_calls,[])

    def test_stop_private_access_is_operation_scoped_and_closes_before_runtime(self):
        alias,request=self.stop_ready()
        command=self.application.management.stop_instance(self.subject,self.org,alias,request)
        item=self.application.authorize(alias,self.subject,self.org)
        with self.assertRaisesRegex(app.Error,'INSTANCE_ADMISSION_CLOSED'):self.application.credential(item)
        with self.assertRaisesRegex(app.Error,'INSTANCE_ADMISSION_CLOSED'):self.application.credential(item,'gui',stop_operation=command['operation_id'])
        with self.assertRaises(app.directory.Fault):self.application.credential(item,stop_operation=app.content.uuid7())
        self.assertEqual(self.application.credential(item,stop_operation=command['operation_id']),'w'*43)
        self.manager=False;self.tick();self.assertEqual(self.drain_calls,[])
        self.manager=True;self.writable=False;self.tick();self.assertEqual(self.drain_calls,[])
        self.writable=True
        for _ in range(3):self.tick()
        item=self.application.authorize(alias,self.subject,self.org)
        self.assertEqual(self.application.directory.operation(command['operation_id'],self.org,self.subject)['phase'],'stopping')
        with self.assertRaisesRegex(app.Error,'INSTANCE_ADMISSION_CLOSED'):self.application.credential(item,stop_operation=command['operation_id'])

    def destroy_ready(self):
        alias,item=self.created_instance();self.destroy_calls=[];original=self.contents.request
        def request(subject,org,method,path,value):
            if path!='/runtime/instances/destroy':return original(subject,org,method,path,value)
            self.destroy_calls.append(copy.deepcopy(value))
            command=self.application.directory.operation(value['operation_id'],org,subject)
            self.assertEqual(command['action'],'destroy')
            phase=['accepted','revoking','deleted'][min(len(self.destroy_calls)-1,2)]
            if getattr(self,'lose_destroy',False):
                self.lose_destroy=False;raise app.content.ContentError('CONTENT_SERVICE_UNAVAILABLE',503)
            workspace=dict(item['runtime_binding'],retained=phase=='deleted')
            if getattr(self,'bad_retention',False):workspace['retained']=False
            return {'operation_id':command['id'],'instance_id':item['id'],'status':'succeeded' if phase=='deleted' else 'running','phase':phase,'workspace':workspace}
        self.contents.request=request
        return alias,item,{'expected_version':item['_directory']['version'],'idempotency_key':'destroy-key-001','confirm_name':item['name']}

    def test_destroy_preserves_retention_and_tombstone_operation_after_lost_response(self):
        alias,item,request=self.destroy_ready()
        command=self.application.management.destroy_instance(self.subject,self.org,alias,request)
        self.assertEqual(self.destroy_calls,[]);self.lose_destroy=True
        self.tick();self.restart();self.tick();self.bad_retention=True;self.tick()
        self.assertIn(alias,self.application.instances())
        self.bad_retention=False;self.tick();self.restart()
        self.assertNotIn(alias,self.application.instances())
        result=self.application.management.operation(self.subject,self.org,command['operation_id'])
        self.assertEqual((result['state'],result['phase']),('completed','deleted'))
        replay=self.application.management.destroy_instance(self.subject,self.org,alias,request)
        self.assertEqual(replay['operation_id'],command['operation_id'])
        retained=json.loads(self.application.store.db.execute('SELECT details FROM retained_workspaces WHERE instance=?',(item['id'],)).fetchone()[0])
        self.assertEqual(retained,dict(item['runtime_binding'],retained=True,workspace_id=item['id']))
        self.assertEqual(len({json.dumps(v,sort_keys=True) for v in self.destroy_calls}),1)
        with self.assertRaisesRegex(app.Error,'INSTANCE_NOT_FOUND'):self.application.authorize(alias,self.subject,self.org)
        self.writable=False
        with self.assertRaisesRegex(app.content.ContentError,'PERMISSION_DENIED'):
            self.application.management.operation(self.subject,self.org,command['operation_id'])

    def test_destroy_confirmation_scope_and_revocation(self):
        alias,item,request=self.destroy_ready()
        with self.assertRaisesRegex(app.management.ManagementError,'INSTANCE_NAME_MISMATCH'):
            self.application.management.destroy_instance(self.subject,self.org,alias,dict(request,confirm_name='wrong'))
        with self.assertRaisesRegex(app.management.ManagementError,'INSTANCE_STATE_CONFLICT'):
            self.application.management.destroy_instance(self.subject,self.org,alias,dict(request,expected_version=1))
        command=self.application.management.destroy_instance(self.subject,self.org,alias,request)
        self.manager=False;self.tick();self.assertEqual(self.destroy_calls,[])
        self.manager=True;self.writable=False;self.tick();self.assertEqual(self.destroy_calls,[])
        self.writable=True;self.tick()
        with self.assertRaisesRegex(app.management.ManagementError,'IDEMPOTENCY_CONFLICT'):
            self.application.management.destroy_instance(self.subject,self.org,alias,dict(request,confirm_name='changed'))


    def adoption_fixture(self):
        instance=app.content.uuid7(); reader=app.content.uuid7()
        config={'id':instance,'organization_id':self.org,'project_id':self.project,'name':self.request['name'],
                'grants':{self.subject:'edit',reader:'read'},'managers':[self.subject],
                'endpoint':'http://legacy-worker:48084','token_env':'BLENDER_WORKER_OLD'}
        self.application.directory.sync_declared({'blenderA':config})
        self.application.directory.sync_declared({})
        with self.application.store.transaction() as db:
            db.execute('INSERT INTO working_copies VALUES (?,?,?,?)',(instance,self.project,self.revision,self.file['file_id']))
        adoption={'instance_id':instance,'alias':'blenderA','expected_version':1,
                  'config_sha256':hashlib.sha256(app.directory.encode(config).encode()).hexdigest(),
                  'manifest_sha256':'c'*64,**{k:app.content.uuid7() for k in ('old_pvc_uid','old_pv_uid','old_workload_uid')}}
        return config,adoption

    def test_operator_adoption_preserves_identity_acl_and_uses_real_creation_ledger(self):
        original,adoption=self.adoption_fixture()
        command=self.application.management.create(self.subject,self.org,self.request,adoption=adoption)
        self.assertEqual((command['alias'],command['instance_id'],command['phase']),('blenderA',original['id'],'accepted'))
        self.assertEqual(self.calls,[]) # Admission is not Runtime success.
        self.restart()
        again=self.application.management.create(self.subject,self.org,self.request,adoption=adoption)
        self.assertEqual(command['operation_id'],again['operation_id'])
        with self.assertRaisesRegex(app.management.ManagementError,'IDEMPOTENCY_CONFLICT'):
            self.application.management.create(self.subject,self.org,self.request)
        for _ in range(5):self.tick()
        item=self.application.instances()['blenderA']
        self.assertEqual(item['_directory']['state'],'stopped')
        self.assertEqual(item['grants'],original['grants']);self.assertEqual(item['managers'],original['managers'])
        self.assertNotIn('token_env',item);self.assertNotIn('endpoint',item)
        with self.application.store.transaction() as db:
            record=db.execute('SELECT * FROM instance_adoptions WHERE instance=?',(original['id'],)).fetchone()
            self.assertEqual(json.loads(record['original_config']),original)
            self.assertEqual(json.loads(record['evidence']),adoption)
        self.assertTrue(self.calls)
        # A stale ConfigMap must not reactivate the old execution path.
        with self.assertRaisesRegex(app.directory.Fault,'INSTANCE_IDENTITY_CONFLICT'):
            self.application.directory.sync_declared({'blenderA':original})

    def test_operator_adoption_rejects_stale_identity_unsaved_source_and_active_leases(self):
        original,adoption=self.adoption_fixture();db=self.application.store.db
        for field,value in [('expected_version',2),('config_sha256','f'*64),('instance_id',app.content.uuid7())]:
            with self.subTest(field=field),self.assertRaisesRegex(app.directory.Fault,'INSTANCE_STATE_CONFLICT'):
                self.application.management.create(self.subject,self.org,self.request,adoption=dict(adoption,**{field:value}))
        db.execute('UPDATE instance_directory SET enabled=1')
        # begin_adoption independently rejects an active declared entry.
        payload={'request':self.request,'file':self.file}
        with self.assertRaisesRegex(app.directory.Fault,'INSTANCE_STATE_CONFLICT'):
            self.application.directory.begin_adoption(self.org,self.subject,'direct-adoption',payload,adoption)
        db.execute('UPDATE instance_directory SET enabled=0')
        db.execute('UPDATE working_copies SET revision=?',(app.content.uuid7(),))
        with self.assertRaisesRegex(app.directory.Fault,'WORKING_COPY_CONFLICT'):
            self.application.management.create(self.subject,self.org,self.request,adoption=adoption)
        db.execute('UPDATE working_copies SET revision=?',(self.revision,))
        db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)',('active-lease',self.subject,self.org,original['id'],self.project,'generation','gui',time.time()+60))
        with self.assertRaisesRegex(app.directory.Fault,'INSTANCE_EDIT_LEASE_HELD'):
            self.application.management.create(self.subject,self.org,self.request,adoption=adoption)
        db.execute('DELETE FROM sessions WHERE id=?',('active-lease',))
        self.manager=False
        with self.assertRaisesRegex(app.content.ContentError,'PERMISSION_DENIED'):
            self.application.management.create(self.subject,self.org,self.request,adoption=adoption)
        self.manager=True;self.writable=False
        with self.assertRaisesRegex(app.content.ContentError,'PERMISSION_DENIED'):
            self.application.management.create(self.subject,self.org,self.request,adoption=adoption)
        self.assertEqual(self.application.directory.pending(),[]);self.assertEqual(self.calls,[])

if __name__ == '__main__':
    unittest.main()
