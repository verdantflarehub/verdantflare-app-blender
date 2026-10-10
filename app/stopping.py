"""Save-and-stop coordination. Network uncertainty preserves the original operation."""
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import time
import urllib.request


class Stopping:
    def __init__(self, coordinator, error, identity, key):
        self.owner, self.app = coordinator, coordinator.app
        self.error, self.identity, self.key = error, identity, key

    def admit(self, subject, org, alias, request):
        if (not isinstance(request, dict) or set(request) != {'expected_version','idempotency_key'}
                or type(request['expected_version']) is not int or request['expected_version'] < 1
                or not isinstance(request['idempotency_key'], str) or not self.key.fullmatch(request['idempotency_key'])):
            raise self.error('INVALID_ARGUMENT', 400)
        options = self.owner.options(subject, org)
        item = self.app.instances().get(alias)
        if item is None:
            raise self.error('NOT_FOUND', 404)
        with self.app.lock(item['id']):
            self.owner.authorize({'instance':item['id'],'subject':subject,'org':org})
            item = self.app.authorize(alias, subject, org)
            prior = self.app.directory.keyed_operation(org, subject, request['idempotency_key'])
            if prior:
                if prior['action'] != 'stop' or prior['instance'] != item['id'] or json.loads(prior['input']).get('request') != request:
                    raise self.error('IDEMPOTENCY_CONFLICT', 409)
                return self.owner.view(prior)
            record = item['_directory']
            if record['source'] != 'dynamic' or record['state'] != 'running' or record['operation_id'] or record['version'] != request['expected_version']:
                raise self.error('INSTANCE_STATE_CONFLICT', 409)
            binding, workspace = item.get('execution_binding'), item.get('runtime_binding')
            started = self.app.directory.last_command(item['id'], 'start')
            if (not isinstance(binding, dict) or not isinstance(binding.get('proof'), dict) or not isinstance(workspace, dict)
                    or workspace.get('station_id') != options['station_id'] or not started or started['state'] != 'completed'
                    or binding.get('start_operation_id') != started['id']):
                raise self.error('WORKSPACE_BINDING_CONFLICT', 409)
            generation = binding['proof']['generation']
            with self.app.store.transaction() as db:
                if db.execute("SELECT 1 FROM sessions WHERE instance=? AND generation=? AND mode IN ('edit','gui') AND expires>?", (item['id'],generation,time.time())).fetchone():
                    raise self.error('INSTANCE_EDIT_LEASE_HELD', 409)
                if db.execute("SELECT 1 FROM operations o JOIN saves s ON s.operation=o.id WHERE o.instance=? AND o.state IN ('running','unknown','retryable')", (item['id'],)).fetchone():
                    raise self.error('SAVE_IN_PROGRESS', 409)
                copy = db.execute('SELECT * FROM working_copies WHERE instance=? AND project=?', (item['id'],item['project_id'])).fetchone()
                if copy is None:
                    raise self.error('WORKSPACE_BINDING_CONFLICT', 409)
                copy = dict(copy)
            probe = self.app.worker(item, path='/internal/status')
            if probe.get('status') != 'ready' or probe.get('generation') != generation or probe.get('gui_held') is not False:
                raise self.error('WORKER_DRAIN_UNVERIFIED', 409)
            payload = {'request':request, 'station_id':options['station_id'], 'workspace':workspace, 'execution':binding,
                       'generation':generation, 'revision':copy['revision'], 'file_id':copy['file_id'],
                       'write_id':self.owner.new_id(), 'commit_id':self.owner.new_id()}
            command = self.app.directory.begin_command(item['id'],org,subject,request['idempotency_key'],'stop',request['expected_version'],payload)
            return self.owner.view(command)

    def worker(self, item, command, payload, action):
        request = {'instance_id':item['id'], 'start_operation_id':payload['execution']['start_operation_id'],
                   'operation_id':command['id'], 'generation':payload['generation'], 'action':action}
        result = self.app.worker(item, request, path='/internal/drain', stop_operation=command['id'])
        expected = {k:v for k,v in request.items() if k != 'action'}
        expected.update(project_id=item['project_id'],pod_uid=payload['execution']['worker']['pod_uid'])
        if (any(result.get(k) != v for k,v in expected.items()) or type(result.get('frozen')) is not bool
                or result.get('phase') not in {'accepted','captured','sealed','aborting','aborted'}):
            raise self.error('WORKER_DRAIN_UNVERIFIED', 502)
        if result['frozen']:
            proof = result.get('proof')
            if (not isinstance(proof,dict) or set(proof) != {'asset_id','sha256','size','scene_version'}
                    or not isinstance(proof['asset_id'],str) or not re.fullmatch(r'checkpoints/checkpoint-[0-9]+-[0-9a-f]{32}\.blend',proof['asset_id'])
                    or not isinstance(proof['sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',proof['sha256'])
                    or type(proof['size']) is not int or not 0 < proof['size'] <= 512*1024*1024
                    or type(proof['scene_version']) is not int or proof['scene_version'] < 0):
                raise self.error('WORKER_DRAIN_UNVERIFIED', 502)
        return result

    def checkpoint(self, item, command, proof):
        path = self.app.staging / ('stop-'+command['id']+'.blend')
        if not path.exists():
            token = self.app.credential(item, stop_operation=command['id'])
            request = urllib.request.Request(item['file_endpoint']+'/assets/'+proof['asset_id'], headers={'Authorization':'Bearer '+token})
            temporary = path.with_suffix('.partial')
            try:
                with self.app.opener.open(request, timeout=120) as response, temporary.open('wb') as stream:
                    if (response.headers.get('Content-Encoding') or response.headers.get('Content-Length') != str(proof['size'])
                            or response.headers.get('X-Asset-SHA256') != proof['sha256']):
                        raise self.error('CHECKPOINT_HASH_MISMATCH', 502)
                    total, digest = 0, hashlib.sha256()
                    while chunk := response.read(min(1024*1024,proof['size']+1-total)):
                        total += len(chunk)
                        if total > proof['size']:
                            raise self.error('CHECKPOINT_HASH_MISMATCH', 502)
                        stream.write(chunk);digest.update(chunk)
                    if total != proof['size'] or digest.hexdigest() != proof['sha256']:
                        raise self.error('CHECKPOINT_HASH_MISMATCH', 502)
                    stream.flush();os.fsync(stream.fileno())
                os.replace(temporary,path)
            finally:
                temporary.unlink(missing_ok=True)
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream,'sha256').hexdigest()
        if path.stat().st_size != proof['size'] or digest != proof['sha256']:
            raise self.error('CHECKPOINT_HASH_MISMATCH', 502)
        return path

    def save(self, item, command, payload, proof):
        subject,org = command['subject'],command['org']
        path = self.checkpoint(item,command,proof)
        ref = self.app.content.upload(subject,org,item['project_id'],payload['write_id'],path,proof['sha256'],proof['size'])
        if not isinstance(ref,dict) or set(ref) != {'store_id','artifact_id','version_id'} or not all(self.identity(v) for v in ref.values()):
            raise self.error('ARTIFACT_RESPONSE_INVALID', 502)
        update = {'path':'blender/main.blend','role':'source','content_ref':ref}
        if payload['file_id']:
            update['file_id'] = payload['file_id']
        result = self.app.content.request(subject,org,'POST','/project/commit',{'project_id':item['project_id'],
            'expected_revision_id':payload['revision'],'commit_id':payload['commit_id'],'changes':{'upsert_files':[update]}})
        if result.get('project_id') != item['project_id'] or not self.identity(result.get('revision_id')):
            raise self.error('PROJECT_RESPONSE_INVALID', 502)
        files = result.get('manifest',{}).get('files')
        if not isinstance(files,list) or any(not isinstance(f,dict) for f in files):
            raise self.error('PROJECT_RESPONSE_INVALID', 502)
        saved = [f for f in files if f.get('path') == 'blender/main.blend' and f.get('content_ref') == ref]
        if len(saved) != 1 or not self.identity(saved[0].get('file_id')):
            raise self.error('PROJECT_RESPONSE_INVALID', 502)
        return {'revision_id':result['revision_id'],'file_id':saved[0]['file_id'],'content_ref':ref,
                'saved_at':datetime.now(timezone.utc).isoformat()}

    def advance(self, command):
        alias,item = self.owner.authorize(command)
        version = item['_directory']['version']
        try:
            payload = json.loads(command['input'])
            subject,org = command['subject'],command['org']
            options = self.owner.options(subject,org)
            item = self.app.authorize(alias,subject,org)
            if (item['_directory']['operation_id'] != command['id'] or item['_directory']['version'] != version
                    or options['station_id'] != payload['station_id'] or item.get('runtime_binding') != payload['workspace']
                    or item.get('execution_binding') != payload['execution']):
                raise self.error('WORKSPACE_BINDING_CONFLICT',409)
            receipt = self.app.directory.stop_receipt(command['id'])
            phase = command['phase']
            if phase == 'accepted':
                self.app.directory.advance(command['id'],version,'draining',{})
                return
            if phase == 'aborting':
                result = self.worker(item,command,payload,'abort')
                if result['phase'] == 'aborted' and not result['frozen']:
                    self.app.directory.abort_stop(command['id'],version,receipt['abort_code'])
                return
            if phase == 'draining':
                result = self.worker(item,command,payload,'prepare')
                if result['phase'] != 'captured' or not result['frozen']:
                    return
                self.app.directory.stop_receipt(command['id'],{'proof':result['proof']})
                self.app.directory.advance(command['id'],version,'saving',{})
                return
            if phase == 'saving':
                proof = receipt['proof']
                if 'saved' not in receipt:
                    result = self.worker(item,command,payload,'status')
                    if result['phase'] != 'captured' or not result['frozen'] or result['proof'] != proof:
                        raise self.error('WORKER_DRAIN_UNVERIFIED',409)
                    try:
                        saved = self.save(item,command,payload,proof)
                    except self.owner.errors as exc:
                        # Only a definitive revision conflict proves no commit happened.
                        # Timeouts, malformed responses and revocation remain fenced.
                        if getattr(exc,'code',None) != 'REVISION_CONFLICT':
                            raise
                        self.app.directory.stop_receipt(command['id'],{'abort_code':'REVISION_CONFLICT'})
                        self.app.directory.advance(command['id'],version,'aborting',{'code':'REVISION_CONFLICT'})
                        return
                    receipt = self.app.directory.stop_receipt(command['id'],{'saved':saved})
                # Commit intent before contacting Runtime: never abort after this point.
                self.app.directory.advance(command['id'],version,'stopping',{})
                return
            if phase not in {'stopping','removing'}:
                raise self.error('OPERATION_CONFLICT',409)
            proof,saved = receipt['proof'],receipt['saved']
            request = dict(proof, **saved['content_ref'],operation_id=command['id'],start_operation_id=payload['execution']['start_operation_id'],
                station_id=payload['station_id'],organization_id=org,user_id=subject,project_id=item['project_id'],instance_id=item['id'],
                generation=payload['generation'],saved_revision_id=saved['revision_id'],commit_id=payload['commit_id'])
            result = self.app.content.request(subject,org,'POST','/runtime/instances/stop',request)
            source = {'source_revision_id':payload['execution']['proof']['startup']['source_revision_id'],
                      'sha256':proof['sha256'],'size':proof['size'],'empty':False}
            if (result.get('operation_id') != command['id'] or result.get('instance_id') != item['id']
                    or result.get('status') not in {'running','succeeded'} or result.get('phase') not in {'accepted','removing','stopped'}
                    or (result['status'] == 'succeeded') != (result['phase'] == 'stopped')
                    or result.get('workspace') != payload['workspace'] or result.get('source') != source
                    or result.get('saved_revision_id') != saved['revision_id']):
                raise self.error('RUNTIME_RESPONSE_INVALID',502)
            self.owner.authorize(command)
            self.app.content.open(subject,org,item['project_id'],require_write=True)
            if result['status'] == 'succeeded':
                self.app.directory.advance(command['id'],version,'stopped',dict(source=source,saved=saved,proof=proof),'stopped',
                    working_copy={'project':item['project_id'],'revision':saved['revision_id'],'file_id':saved['file_id']})
            else:
                self.app.directory.advance(command['id'],version,'removing' if result['phase']=='removing' else 'stopping',{})
        except self.owner.errors as exc:
            code = getattr(exc,'code','MANAGEMENT_UNAVAILABLE')
            if code == 'PROJECT_EXTERNAL_DEPENDENCIES' and command['phase'] == 'draining':
                self.app.directory.stop_receipt(command['id'],{'abort_code':code})
                self.app.directory.advance(command['id'],version,'aborting',{'code':code})
            else:
                self.owner.stop_error(command,version,code)
