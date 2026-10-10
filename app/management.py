"""Durable instance management coordinator; no worker credentials are persisted."""
from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import re
import threading
import uuid

_stopping_spec = importlib.util.spec_from_file_location('blender_stopping',Path(__file__).with_name('stopping.py'))
_stopping = importlib.util.module_from_spec(_stopping_spec)
_stopping_spec.loader.exec_module(_stopping)


class ManagementError(Exception):
    def __init__(self, code, status=503):
        self.code, self.status = code, status
        super().__init__(code)


LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z')
KEY = re.compile(r'[A-Za-z0-9_-]{8,128}\Z')
CODES = frozenset({
    'PERMISSION_DENIED', 'NOT_FOUND', 'CONTENT_SERVICE_UNAVAILABLE', 'PROJECT_RESPONSE_INVALID',
    'PROJECT_SOURCE_MISMATCH', 'ARTIFACT_RESPONSE_INVALID', 'ARTIFACT_CONTENT_INVALID',
    'RUNTIME_RESPONSE_INVALID', 'WORKSPACE_PREPARATION_UNAVAILABLE', 'WORKSPACE_PREPARATION_CONFLICT',
    'WORKSPACE_BINDING_CONFLICT', 'STORAGE_CAPACITY_UNAVAILABLE', 'WORKSPACE_IN_USE',
    'OPERATION_OUTCOME_UNKNOWN', 'OPERATION_CONFLICT', 'INSTANCE_BUSY', 'PROFILE_UNAVAILABLE',
    'MANAGEMENT_UNAVAILABLE', 'INSTANCE_CONFIGURATION_UNAVAILABLE',
    'COMPUTE_CAPACITY_UNAVAILABLE', 'COMPUTE_CAPACITY_UNKNOWN', 'WORKER_LOAD_UNVERIFIED', 'WORKER_EXITED',
    'WORKER_DRAIN_UNVERIFIED', 'REVISION_CONFLICT', 'PROJECT_EXTERNAL_DEPENDENCIES',
    'WORKER_BUSY', 'WORKER_ACTIVITY_UNKNOWN', 'DRAIN_BUSY', 'DRAIN_ALREADY_STOPPING',
    'DRAIN_CONTENT_CHANGED', 'DRAIN_STATE_UNKNOWN', 'DRAIN_PROCESS_CHANGED', 'CHECKPOINT_HASH_MISMATCH',
    'INSTANCE_EDIT_LEASE_HELD', 'SAVE_IN_PROGRESS',
})


def identity(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value and uuid.UUID(value).int != 0
    except ValueError:
        return False


class Coordinator:
    def __init__(self, app, client, new_id, errors, enabled=False):
        self.app, self.client, self.new_id = app, client, new_id
        self.errors, self.enabled = errors + (ManagementError,), enabled
        self.stop_event = threading.Event()
        self.thread = None
        self.stopping = _stopping.Stopping(self,ManagementError,identity,KEY)

    def stop_instance(self, subject, org, alias, request):
        return self.stopping.admit(subject,org,alias,request)

    def stop_error(self, command, version, code):
        self.app.directory.advance(command['id'],version,command['phase'],{'code':code if code in CODES else 'MANAGEMENT_UNAVAILABLE'})

    def destroy_instance(self, subject, org, alias, request):
        if (not isinstance(request,dict) or set(request) != {'expected_version','idempotency_key','confirm_name'}
                or type(request['expected_version']) is not int or request['expected_version'] < 1
                or not isinstance(request['idempotency_key'],str) or not KEY.fullmatch(request['idempotency_key'])
                or not isinstance(request['confirm_name'],str)):
            raise ManagementError('INVALID_ARGUMENT',400)
        options = self.options(subject,org)
        prior = self.app.directory.keyed_operation(org,subject,request['idempotency_key'])
        if prior:
            old_alias,item = self.authorize(prior)
            if prior['action'] != 'destroy' or old_alias != alias or json.loads(prior['input']).get('request') != request:
                raise ManagementError('IDEMPOTENCY_CONFLICT',409)
            self.app.content.open(subject,org,item['project_id'],require_write=True)
            return self.view(prior)
        item = self.app.instances().get(alias)
        if item is None:
            raise ManagementError('NOT_FOUND',404)
        with self.app.lock(item['id']):
            _,item = self.authorize({'instance':item['id'],'subject':subject,'org':org})
            self.app.content.open(subject,org,item['project_id'],require_write=True)
            if request['confirm_name'] != item.get('name',alias.replace('blender','Blender ',1)):
                raise ManagementError('INSTANCE_NAME_MISMATCH',409)
            record = item['_directory']
            if record['source'] != 'dynamic' or record['state'] != 'stopped' or record['operation_id'] or record['version'] != request['expected_version']:
                raise ManagementError('INSTANCE_STATE_CONFLICT',409)
            workspace = item.get('runtime_binding')
            if not isinstance(workspace,dict) or workspace.get('station_id') != options['station_id'] or workspace.get('retained') is not False:
                raise ManagementError('WORKSPACE_BINDING_CONFLICT',409)
            previous = self.app.directory.last_command(item['id'],'stop') or self.app.directory.last_command(item['id'],'create')
            if not previous or previous['state'] != 'completed' or previous['phase'] != 'stopped':
                raise ManagementError('OPERATION_CONFLICT',409)
            payload = {'request':request,'station_id':options['station_id'],'workspace':workspace,'previous_operation_id':previous['id']}
            return self.view(self.app.directory.begin_command(item['id'],org,subject,request['idempotency_key'],'destroy',request['expected_version'],payload))

    def advance_destroy(self, command):
        _,item = self.authorize(command)
        version = item['_directory']['version']
        try:
            subject,org = command['subject'],command['org']
            options = self.options(subject,org)
            payload = json.loads(command['input'])
            if options['station_id'] != payload['station_id'] or item.get('runtime_binding') != payload['workspace']:
                raise ManagementError('WORKSPACE_BINDING_CONFLICT',409)
            self.app.content.open(subject,org,item['project_id'],require_write=True)
            request = {'operation_id':command['id'],'previous_operation_id':payload['previous_operation_id'],
                       'station_id':payload['station_id'],'organization_id':org,'user_id':subject,'project_id':item['project_id'],'instance_id':item['id']}
            result = self.app.content.request(subject,org,'POST','/runtime/instances/destroy',request)
            final = result.get('status') == 'succeeded'
            if (result.get('operation_id') != command['id'] or result.get('instance_id') != item['id']
                    or result.get('status') not in {'running','succeeded'} or result.get('phase') not in {'accepted','revoking','deleted'}
                    or final != (result['phase'] == 'deleted') or result.get('workspace') != dict(payload['workspace'],retained=final)):
                raise ManagementError('RUNTIME_RESPONSE_INVALID',502)
            _,current = self.authorize(command)
            if current['_directory']['version'] != version:
                raise ManagementError('OPERATION_CONFLICT',409)
            self.app.content.open(subject,org,item['project_id'],require_write=True)
            retained = dict(result['workspace'],workspace_id=item['id']) if final else None
            self.app.directory.advance(command['id'],version,result['phase'],{},'deleted' if final else None,
                retained=retained,binding={'runtime_binding':result['workspace']} if final else None)
        except self.errors as exc:
            code = getattr(exc,'code','MANAGEMENT_UNAVAILABLE')
            self.app.directory.advance(command['id'],version,command['phase'],{'code':code if code in CODES else 'MANAGEMENT_UNAVAILABLE'})

    def options(self, subject, org):
        if not self.enabled:
            raise ManagementError('MANAGEMENT_UNAVAILABLE')
        value = self.app.content.request(subject, org, 'POST', '/runtime/options', {})
        if (not identity(value.get('station_id')) or value.get('user_id') != subject
                or value.get('organization_id') != org or value.get('can_manage') is not True
                or not isinstance(value.get('profiles'), list)):
            raise ManagementError('RUNTIME_RESPONSE_INVALID', 502)
        profiles, seen = [], set()
        for profile in value['profiles']:
            if (not isinstance(profile, dict) or not isinstance(profile.get('profile_id'), str)
                    or not LABEL.fullmatch(profile['profile_id']) or profile['profile_id'] in seen
                    or type(profile.get('storage_reserved_bytes')) is not int or profile['storage_reserved_bytes'] < 1
                    or type(profile.get('max_file_bytes')) is not int
                    or not 0 < profile['max_file_bytes'] <= profile['storage_reserved_bytes']):
                raise ManagementError('RUNTIME_RESPONSE_INVALID', 502)
            seen.add(profile['profile_id'])
            profiles.append({k: profile[k] for k in ('profile_id', 'storage_reserved_bytes', 'max_file_bytes')})
        return {'station_id': value['station_id'], 'user_id': subject, 'organization_id': org, 'can_manage': True, 'profiles': profiles}

    def create(self, subject, org, request, *, adoption=None):
        # Only the offline operator entrypoint supplies adoption. HTTP retains its
        # exact request whitelist and cannot select or replace an existing identity.
        if adoption is not None:
            fields = {'instance_id','alias','expected_version','config_sha256','old_pvc_uid','old_pv_uid','old_workload_uid','manifest_sha256'}
            if (not isinstance(adoption,dict) or set(adoption)!=fields
                    or not all(identity(adoption[k]) for k in ('instance_id','old_pvc_uid','old_pv_uid','old_workload_uid'))
                    or not isinstance(adoption['alias'],str) or not re.fullmatch(r'blender[A-Za-z0-9_-]{1,57}',adoption['alias'])
                    or type(adoption['expected_version']) is not int or adoption['expected_version']<1
                    or not all(isinstance(adoption[k],str) and re.fullmatch(r'[a-f0-9]{64}',adoption[k]) for k in ('config_sha256','manifest_sha256'))):
                raise ManagementError('INVALID_ARGUMENT',400)
        if (not isinstance(request, dict) or set(request) != {'name', 'project_id', 'source_revision_id', 'profile_id', 'idempotency_key'}
                or not isinstance(request['name'], str) or not 1 <= len(request['name'].strip()) <= 40
                or any(ord(c) < 32 or ord(c) == 127 for c in request['name'])
                or not identity(request['project_id']) or not identity(request['source_revision_id'])
                or not isinstance(request['profile_id'], str) or not LABEL.fullmatch(request['profile_id'])
                or not isinstance(request['idempotency_key'], str) or not KEY.fullmatch(request['idempotency_key'])):
            raise ManagementError('INVALID_ARGUMENT', 400)
        request = dict(request, name=request['name'].strip())
        options = self.options(subject, org)  # Current manager check before directory admission.
        project = self.app.content.open(subject, org, request['project_id'], revision=request['source_revision_id'], require_write=True)
        prior = self.app.directory.keyed_operation(org, subject, request['idempotency_key'])
        if prior:
            if (prior['action'] != 'create' or json.loads(prior['input']).get('request') != request
                    or json.loads(prior['input']).get('adoption') != adoption):
                raise ManagementError('IDEMPOTENCY_CONFLICT', 409)
            self.authorize(prior)
            return self.view(prior)
        profiles = [p for p in options['profiles'] if p['profile_id'] == request['profile_id']]
        if not profiles:
            raise ManagementError('PROFILE_UNAVAILABLE', 409)
        profile = profiles[0]
        files = project.get('manifest', {}).get('files')
        if not isinstance(files, list) or any(not isinstance(f, dict) for f in files):
            raise ManagementError('PROJECT_RESPONSE_INVALID', 502)
        sources = [f for f in files if f.get('path') == 'blender/main.blend']
        if len(sources) > 1:
            raise ManagementError('PROJECT_SOURCE_MISMATCH', 409)
        source = sources[0] if sources else None
        info = self.app.content.describe(subject, org, request['project_id'], request['source_revision_id'], source) if source else {'sha256': '', 'size': 0}
        if info['size'] > profile['max_file_bytes']:
            raise ManagementError('CONTENT_TOO_LARGE', 413)
        payload = {'request': request, 'station_id': options['station_id'], 'file': source,
                   'source_sha256': info['sha256'], 'source_size': info['size'], 'storage_reserved_bytes': profile['storage_reserved_bytes']}
        if adoption is not None:
            payload['adoption'] = adoption
            command = self.app.directory.begin_adoption(org,subject,request['idempotency_key'],payload,adoption)
            return self.view(command)
        instance = self.new_id()
        config = {'id': instance, 'organization_id': org, 'project_id': request['project_id'], 'name': request['name'],
                  'grants': {subject: 'edit'}, 'managers': [subject], 'profile_id': request['profile_id']}
        command = self.app.directory.begin_create(org, subject, request['idempotency_key'], payload, 'blender' + instance.replace('-', ''), config)
        return self.view(command)

    def projects(self, subject, org, request):
        if not isinstance(request, dict) or set(request) - {'cursor'} or ('cursor' in request and not identity(request['cursor'])):
            raise ManagementError('INVALID_ARGUMENT', 400)
        self.options(subject, org)
        result = self.app.content.request(subject, org, 'POST', '/project/list', dict(request, require_write=True, limit=50))
        if not isinstance(result.get('items'), list) or len(result['items']) > 50 or (result.get('next_cursor') != '' and not identity(result.get('next_cursor'))):
            raise ManagementError('PROJECT_RESPONSE_INVALID', 502)
        items = []
        for item in result['items']:
            if (not isinstance(item, dict) or not identity(item.get('project_id')) or not identity(item.get('head_revision_id'))
                    or not isinstance(item.get('name'), str) or not item['name'].strip()):
                raise ManagementError('PROJECT_RESPONSE_INVALID', 502)
            items.append({k:item[k] for k in ('project_id', 'head_revision_id', 'name')})
        return {'items':items, 'next_cursor':result['next_cursor']}

    def start_instance(self, subject, org, alias, request):
        if (not isinstance(request, dict) or set(request) != {'expected_version', 'idempotency_key'}
                or type(request['expected_version']) is not int or request['expected_version'] < 1
                or not isinstance(request['idempotency_key'], str) or not KEY.fullmatch(request['idempotency_key'])):
            raise ManagementError('INVALID_ARGUMENT', 400)
        options = self.options(subject, org)
        item = self.app.instances().get(alias)
        if item is None:
            raise ManagementError('NOT_FOUND', 404)
        with self.app.lock(item['id']):
            _, item = self.authorize({'instance': item['id'], 'subject': subject, 'org': org})
            self.app.content.open(subject, org, item['project_id'], require_write=True)
            prior = self.app.directory.keyed_operation(org, subject, request['idempotency_key'])
            if prior:
                if prior['action'] != 'start' or prior['instance'] != item['id'] or json.loads(prior['input']).get('request') != request:
                    raise ManagementError('IDEMPOTENCY_CONFLICT', 409)
                return self.view(prior)
            created = self.app.directory.last_command(item['id'], 'create')
            if item['_directory']['source'] != 'dynamic' or not created or created['state'] != 'completed' or created['phase'] != 'stopped':
                raise ManagementError('WORKSPACE_BINDING_CONFLICT', 409)
            if not isinstance(item.get('runtime_binding'), dict):
                raise ManagementError('WORKSPACE_BINDING_CONFLICT', 409)
            original = json.loads(created['input'])
            if options['station_id'] != original['station_id']:
                raise ManagementError('WORKSPACE_BINDING_CONFLICT', 409)
            source = {'source_revision_id': original['request']['source_revision_id'], 'sha256': original['source_sha256'],
                      'size': original['source_size'], 'empty': original['file'] is None}
            stopped = self.app.directory.last_command(item['id'], 'stop')
            if stopped:
                if stopped['state'] != 'completed' or stopped['phase'] != 'stopped':
                    raise ManagementError('OPERATION_CONFLICT', 409)
                source = json.loads(stopped['result']).get('evidence', {}).get('source')
                if not isinstance(source, dict) or set(source) != {'source_revision_id','sha256','size','empty'}:
                    raise ManagementError('WORKSPACE_BINDING_CONFLICT', 409)
            payload = {'request': request, 'station_id': original['station_id'], 'create_operation_id': created['id'],
                       'profile_id': original['request']['profile_id'], 'source': source, 'workspace': item.get('runtime_binding')}
            command = self.app.directory.begin_command(item['id'], org, subject, request['idempotency_key'], 'start', request['expected_version'], payload)
            return self.view(command)

    def source(self, subject, org, request):
        if not isinstance(request, dict) or set(request) != {'project_id','source_revision_id'} or not all(identity(v) for v in request.values()):
            raise ManagementError('INVALID_ARGUMENT', 400)
        self.options(subject, org)
        project = self.app.content.open(subject, org, request['project_id'], revision=request['source_revision_id'], require_write=True)
        files = project.get('manifest', {}).get('files')
        if not isinstance(files, list) or any(not isinstance(f, dict) for f in files):
            raise ManagementError('PROJECT_RESPONSE_INVALID', 502)
        sources = [f for f in files if f.get('path') == 'blender/main.blend']
        if len(sources) > 1:
            raise ManagementError('PROJECT_SOURCE_MISMATCH', 409)
        info = self.app.content.describe(subject, org, request['project_id'], request['source_revision_id'], sources[0]) if sources else {'size':0, 'sha256':None}
        return dict(request, kind='restore' if sources else 'empty', size=info['size'], sha256=info['sha256'])

    def new_project(self, subject, org, request):
        if (not isinstance(request, dict) or set(request) != {'name','commit_id'} or not isinstance(request['name'], str)
                or not 1 <= len(request['name'].strip()) <= 256 or any(ord(c) < 32 for c in request['name'])
                or not identity(request['commit_id']) or uuid.UUID(request['commit_id']).version != 7):
            raise ManagementError('INVALID_ARGUMENT', 400)
        self.options(subject, org)
        result = self.app.content.request(subject, org, 'POST', '/project/create',
            {'name':request['name'].strip(), 'commit_id':request['commit_id'], 'category':'blender', 'entry_path':'README.md',
             'entry_text':'# Blender 项目\n\nBlender 工程保存于 blender/main.blend。\n'})
        if not identity(result.get('project_id')) or not identity(result.get('revision_id')):
            raise ManagementError('PROJECT_RESPONSE_INVALID', 502)
        return self.source(subject, org, {'project_id':result['project_id'],'source_revision_id':result['revision_id']})

    def authorize(self, command):
        entries = self.app.instances()
        if command.get('action') == 'destroy' and command.get('state') == 'completed':
            tombstone = self.app.directory.tombstone(command['instance'])
            if tombstone:
                entries[tombstone[0]] = tombstone[1]
        for alias, item in entries.items():
            if item['id'] == command['instance']:
                if (item['organization_id'] != command['org'] or command['subject'] not in item.get('managers', [])
                        or item['grants'].get(command['subject']) != 'edit'):
                    raise ManagementError('PERMISSION_DENIED', 403)
                return alias, item
        raise ManagementError('NOT_FOUND', 404)

    def operation(self, subject, org, operation):
        if not self.enabled:
            raise ManagementError('MANAGEMENT_UNAVAILABLE')
        if not identity(operation):
            raise ManagementError('NOT_FOUND', 404)
        command = self.app.directory.operation(operation, org, subject)
        self.options(subject, org)
        _, item = self.authorize(command)
        self.app.content.open(subject, org, item['project_id'], require_write=True)
        return self.view(command)

    def view(self, command):
        alias, item = self.authorize(command)
        evidence = json.loads(command['result']).get('evidence', {})
        return {'operation_id': command['id'], 'instance_id': command['instance'], 'alias': alias,
                'action': command['action'], 'state': command['state'], 'phase': command['phase'],
                'instance_state': item['_directory']['state'], 'version': item['_directory']['version'],
                'code': evidence.get('code') if evidence.get('code') in CODES else None}

    def workspace(self, result, request, payload, previous=None):
        binding = result.get('workspace')
        if binding is None and result['phase'] == 'accepted' and previous is None:
            return None
        expected = {k: request[k] for k in ('instance_id', 'station_id', 'organization_id', 'project_id', 'profile_id')}
        expected.update(create_operation_id=request['operation_id'], retained=False, reserved_bytes=payload['storage_reserved_bytes'])
        if (not isinstance(binding, dict) or any(type(binding.get(k)) is not type(v) or binding[k] != v for k, v in expected.items())
                or any(not isinstance(binding.get(k), str) or not binding[k] or len(binding[k]) > 253 for k in
                       ('namespace', 'pool', 'pvc_name', 'pvc_uid', 'pv_name', 'pv_uid', 'node_name'))):
            raise ManagementError('WORKSPACE_BINDING_CONFLICT', 409)
        if previous is not None and previous != binding:
            raise ManagementError('WORKSPACE_BINDING_CONFLICT', 409)
        return binding

    def advance(self, operation, org, subject):
        if not self.enabled:
            return
        command = self.app.directory.operation(operation, org, subject)
        with self.app.lock(command['instance']):
            command = self.app.directory.operation(operation, org, subject)
            if command['state'] != 'running':
                return
            if command['action'] == 'start':
                return self.advance_start(command)
            if command['action'] == 'stop':
                return self.stopping.advance(command)
            if command['action'] == 'destroy':
                return self.advance_destroy(command)
            if command['action'] != 'create':
                return
            # Reload and authorize each time; no cached session or prior approval.
            item = next((item for item in self.app.instances().values() if item['id'] == command['instance']), None)
            if item is None:
                raise ManagementError('NOT_FOUND', 404)
            version = item['_directory']['version']
            try:
                self.authorize(command)
                payload = json.loads(command['input'])
                fixed = payload['request']
                options = self.options(subject, org)
                if options['station_id'] != payload['station_id']:
                    raise ManagementError('WORKSPACE_BINDING_CONFLICT', 409)
                request = {k: fixed[k] for k in ('project_id', 'source_revision_id', 'profile_id')}
                request.update(operation_id=operation, instance_id=command['instance'], station_id=payload['station_id'],
                               organization_id=org, user_id=subject, source_sha256=payload['source_sha256'],
                               source_size=payload['source_size'], empty_source=payload['file'] is None)
                result = self.client.advance(subject, org, request, payload['file'], self.app.directory.upload_receipt(operation),
                                             lambda receipt: self.app.directory.upload_receipt(operation, receipt),
                                             validate_progress=lambda result: self.workspace(result, request, payload, item.get('runtime_binding')))
                binding = self.workspace(result, request, payload, item.get('runtime_binding'))
                if result['status'] == 'failed' or (result['phase'] == 'stopped') != (result['status'] == 'succeeded'):
                    raise ManagementError('RUNTIME_RESPONSE_INVALID', 502)
                _, current = self.authorize(command)
                if current['_directory']['version'] != version:
                    raise ManagementError('OPERATION_CONFLICT', 409)
                evidence = {'code': result.get('code') if result.get('code') in CODES else None}
                copy = None
                final = None
                if result['status'] == 'succeeded':
                    # Preparation may have taken time; recheck before publishing the usable baseline.
                    self.app.content.open(subject, org, fixed['project_id'], revision=fixed['source_revision_id'], require_write=True)
                    final = 'stopped'
                    copy = {'project': fixed['project_id'], 'revision': fixed['source_revision_id'],
                            'file_id': payload['file']['file_id'] if payload['file'] else ''}
                self.app.directory.advance(operation, version, result['phase'], evidence, final,
                                           binding={'runtime_binding': binding} if binding else None, working_copy=copy)
            except self.errors as exc:
                # Unknown outcomes remain pending. Only bounded codes may reach storage/UI.
                code = getattr(exc, 'code', '')
                code = code if code in CODES else 'MANAGEMENT_UNAVAILABLE'
                self.app.directory.advance(operation, version, command['phase'], {'code': code})

    def advance_start(self, command):
        # Called under the same per-instance lock used by edit and save admission.
        _, item = self.authorize(command)
        version = item['_directory']['version']
        try:
            subject, org = command['subject'], command['org']
            options = self.options(subject, org)
            payload = json.loads(command['input'])
            if options['station_id'] != payload['station_id'] or item.get('runtime_binding') != payload['workspace']:
                raise ManagementError('WORKSPACE_BINDING_CONFLICT', 409)
            self.app.content.open(subject, org, item['project_id'], require_write=True)
            request = dict(payload['source'], operation_id=command['id'], create_operation_id=payload['create_operation_id'],
                           station_id=payload['station_id'], organization_id=org, user_id=subject,
                           project_id=item['project_id'], instance_id=item['id'], profile_id=payload['profile_id'])
            result = self.app.content.request(subject, org, 'POST', '/runtime/instances/start', request)
            if (result.get('operation_id') != command['id'] or result.get('instance_id') != item['id']
                    or result.get('status') not in {'running','succeeded'} or result.get('phase') not in {'accepted','launching','loading','running'}
                    or (result['status'] == 'succeeded') != (result['phase'] == 'running')):
                raise ManagementError('RUNTIME_RESPONSE_INVALID', 502)
            if not isinstance(payload['workspace'], dict) or result.get('workspace') != payload['workspace']:
                raise ManagementError('WORKSPACE_BINDING_CONFLICT', 409)
            binding = self.start_binding(request, result, item.get('execution_binding'))
            _, current = self.authorize(command)
            if current['_directory']['version'] != version:
                raise ManagementError('OPERATION_CONFLICT', 409)
            if result['status'] == 'succeeded':
                self.app.content.open(subject, org, item['project_id'], require_write=True)
            self.app.directory.advance(command['id'], version, result['phase'], {},
                'running' if result['status'] == 'succeeded' else None, binding=binding)
        except self.errors as exc:
            code = getattr(exc, 'code', '')
            self.app.directory.advance(command['id'], version, command['phase'], {'code': code if code in CODES else 'MANAGEMENT_UNAVAILABLE'})

    def start_binding(self, request, result, previous):
        worker, proof = result.get('worker'), result.get('proof')
        if worker is None:
            if result['phase'] != 'accepted' or proof is not None or (previous and previous.get('start_operation_id') == request['operation_id']):
                raise ManagementError('RUNTIME_RESPONSE_INVALID', 502)
            return None
        namespace = result['workspace']['namespace']
        name = 'blender-run-' + request['operation_id'].replace('-', '')
        base = 'http://' + name + '.' + namespace + '.svc.cluster.local:'
        expected = {'pod_name':name, 'namespace':namespace, 'control_endpoint':base+'48084', 'file_endpoint':base+'48085', 'gui_endpoint':base+'48083'}
        if (not isinstance(worker, dict) or any(worker.get(k) != v for k,v in expected.items())
                or not all(identity(worker.get(k)) for k in ('pod_uid','service_uid')) or type(worker.get('pod_ready')) is not bool):
            raise ManagementError('WORKSPACE_BINDING_CONFLICT', 409)
        observed = {**expected, 'pod_uid':worker['pod_uid'], 'service_uid':worker['service_uid'], 'pod_ready':worker['pod_ready']}
        if previous and previous.get('start_operation_id') == request['operation_id']:
            if any(previous.get('worker',{}).get(k) != observed[k] for k in ('pod_uid','service_uid')):
                raise ManagementError('WORKSPACE_BINDING_CONFLICT', 409)
        execution = {'start_operation_id':request['operation_id'], 'worker':observed}
        if result['status'] == 'succeeded':
            startup = {k:request[k] for k in ('source_revision_id','sha256','size','empty','create_operation_id','instance_id','project_id')}
            startup.update(start_operation_id=request['operation_id'], pod_uid=observed['pod_uid'])
            if (not observed['pod_ready'] or not isinstance(proof, dict) or not isinstance(proof.get('startup'), dict)
                    or set(proof['startup']) != set(startup) or any(type(proof['startup'][k]) is not type(v) or proof['startup'][k] != v for k,v in startup.items())
                    or not isinstance(proof.get('generation'), str) or not re.fullmatch(r'[0-9a-f]{32}', proof['generation'])
                    or not isinstance(proof.get('gpu_uuid'), str) or not re.fullmatch(r'GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', proof['gpu_uuid'])):
                raise ManagementError('RUNTIME_RESPONSE_INVALID', 502)
            execution['proof'] = {'startup':startup, 'generation':proof['generation'], 'gpu_uuid':proof['gpu_uuid']}
        elif proof is not None:
            raise ManagementError('RUNTIME_RESPONSE_INVALID', 502)
        binding = {'execution_binding':execution}
        if result['status'] == 'succeeded':
            binding.update(endpoint=observed['control_endpoint'], file_endpoint=observed['file_endpoint'], gui_endpoint=observed['gui_endpoint'])
        return binding

    def recover_once(self):
        for command in self.app.directory.pending():
            if self.stop_event.is_set():
                break
            try:
                self.advance(command['id'], command['org'], command['subject'])
            except self.errors:
                # A conflict/revocation cannot stop recovery of other instances.
                continue

    def start(self):
        if not self.enabled or self.thread is not None:
            return
        def run():
            while not self.stop_event.is_set():
                try:
                    self.recover_once()
                except self.errors:
                    # A transient directory/config read failure must not kill recovery.
                    pass
                self.stop_event.wait(2)
        self.thread = threading.Thread(target=run, name='blender-management', daemon=True)
        self.thread.start()
