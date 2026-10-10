"""Fixed-revision content handoff to Runtime's GPU-free preparation phase.

The caller owns durable command admission and passes an upload-receipt writer.
Worker credentials live only in this call, never in receipts or public progress.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import urllib.request
import urllib.error
import uuid


class CreationError(Exception):
    def __init__(self, code, status=503):
        self.code, self.status = code, status
        super().__init__(code)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class CreationClient:
    def __init__(self, content, staging, opener=None):
        self.content, self.staging = content, Path(staging)
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def agent(self, origin, token, path, value=None, stream=None, info=None):
        headers = {'Authorization':'Bearer ' + token, 'Content-Type':'application/json'}
        body = None if value is None else json.dumps(value, allow_nan=False).encode()
        if stream is not None:
            body = stream
            headers.update({'Content-Type':'application/octet-stream', 'Content-Length':str(info['size']),
                            'X-Asset-Name':'restore.blend', 'X-Asset-SHA256':info['sha256']})
        request = urllib.request.Request(origin + path, data=body, headers=headers, method='GET' if body is None else 'POST')
        try:
            with self.opener.open(request, timeout=240 if stream else 10) as response:
                raw = response.read(32769)
                if len(raw) > 32768 or response.headers.get('Content-Encoding'):
                    raise ValueError()
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError()
                return result
        except (OSError, ValueError):
            raise CreationError('WORKSPACE_PREPARATION_UNAVAILABLE') from None

    def advance(self, subject, org, request, file, receipt, remember_upload, validate_progress=None):
        """Return sanitized Runtime progress; never mark an instance stopped here."""
        if request.get('user_id') != subject or request.get('organization_id') != org:
            raise CreationError('PERMISSION_DENIED', 403)
        project = self.content.open(subject, org, request['project_id'],
                                    revision=request['source_revision_id'], require_write=True)
        files = [entry for entry in project.get('manifest', {}).get('files', []) if entry.get('path') == 'blender/main.blend']
        if len(files) > 1 or (request['empty_source'] and (files or file is not None)) or (not request['empty_source'] and (len(files) != 1 or files[0] != file)):
            raise CreationError('PROJECT_SOURCE_MISMATCH', 409)
        result = self.content.request(subject, org, 'POST', '/runtime/instances/create', request)
        if result.get('operation_id') != request['operation_id'] or result.get('instance_id') != request['instance_id']:
            raise CreationError('RUNTIME_RESPONSE_INVALID', 502)
        access = result.get('access')
        # Credential-free internal progress. HTTP views must separately strip the
        # private workload/storage binding before replying to browser clients.
        progress = {key: result[key] for key in ('operation_id', 'instance_id', 'status', 'phase', 'code') if key in result}
        if isinstance(result.get('workspace'), dict):
            progress['workspace'] = {k:result['workspace'][k] for k in (
                'instance_id', 'station_id', 'organization_id', 'project_id', 'create_operation_id', 'profile_id',
                'namespace', 'pool', 'pvc_name', 'pvc_uid', 'pv_name', 'pv_uid', 'node_name', 'reserved_bytes', 'retained') if k in result['workspace']}
        if result.get('status') not in {'running', 'succeeded', 'failed'} or result.get('phase') not in {'accepted', 'reserved', 'claimed', 'preparing', 'awaiting_content', 'removing', 'stopped'}:
            raise CreationError('RUNTIME_RESPONSE_INVALID', 502)
        if validate_progress is not None:
            validate_progress(progress)
        if result['phase'] != 'awaiting_content':
            if access is not None or (result['status'] == 'succeeded' and result['phase'] != 'stopped'):
                raise CreationError('RUNTIME_RESPONSE_INVALID', 502)
            return progress
        if result['status'] != 'running' or not isinstance(access, dict):
            raise CreationError('RUNTIME_RESPONSE_INVALID', 502)
        binding = result.get('workspace', {})
        namespace = binding.get('namespace', '')
        if not isinstance(namespace, str):
            raise CreationError('RUNTIME_RESPONSE_INVALID', 502)
        instance = str(uuid.UUID(request['instance_id']))
        expected = 'http://blender-' + instance.replace('-', '') + '-prepare.' + namespace + '.svc.cluster.local:48085'
        if (not isinstance(namespace, str) or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', namespace)
                or access.get('file_endpoint') != expected or not isinstance(access.get('worker_token'), str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{43}', access['worker_token'])
                or not isinstance(access.get('pod_uid'), str) or not access['pod_uid']):
            raise CreationError('RUNTIME_RESPONSE_INVALID', 502)
        token = access['worker_token']
        remote = self.agent(expected, token, '/internal/workspace/status')
        if remote.get('instance_id') != instance or remote.get('pod_uid') != access['pod_uid']:
            raise CreationError('WORKSPACE_BINDING_CONFLICT', 409)
        prepared = remote.get('preparation')
        arguments = {'prepare_id':request['operation_id'], 'instance_id':instance, 'project_id':request['project_id'],
                     'source_revision_id':request['source_revision_id'], 'sha256':None if request['empty_source'] else request['source_sha256'],
                     'size':request['source_size']}
        asset = None
        if prepared is not None:
            if not isinstance(prepared, dict) or any(prepared.get(k) != v for k,v in arguments.items()):
                raise CreationError('WORKSPACE_PREPARATION_CONFLICT', 409)
            asset = prepared.get('asset_id')
            if prepared.get('state') not in {'preparing', 'prepared'}:
                raise CreationError('WORKSPACE_PREPARATION_CONFLICT', 409)
        elif not request['empty_source']:
            if receipt is not None:
                if receipt.get('pod_uid') != access['pod_uid']:
                    raise CreationError('WORKSPACE_BINDING_CONFLICT', 409)
                asset = receipt.get('asset_id')
            else:
                path = self.staging / (str(uuid.UUID(request['operation_id'])) + '.blend')
                info = self.content.download(subject, org, request['project_id'], request['source_revision_id'], file, path)
                if info != {'sha256':request['source_sha256'], 'size':request['source_size']}:
                    raise CreationError('PROJECT_SOURCE_MISMATCH', 409)
                with path.open('rb') as stream:
                    uploaded = self.agent(expected, token, '/upload', stream=stream, info=info)
                if uploaded.get('sha256') != info['sha256']:
                    raise CreationError('WORKSPACE_CONTENT_MISMATCH', 409)
                asset = uploaded.get('asset_id')
                if not isinstance(asset, str) or not re.fullmatch(r'inbox/[A-Za-z0-9_-]{16}/restore\.blend', asset):
                    raise CreationError('WORKSPACE_CONTENT_MISMATCH', 409)
                # Must finish the durable write before asking the agent to prepare.
                remember_upload({'asset_id':asset, 'pod_uid':access['pod_uid']})
        if (request['empty_source'] and asset is not None) or (not request['empty_source'] and (not isinstance(asset, str) or not re.fullmatch(r'inbox/[A-Za-z0-9_-]{16}/restore\.blend', asset))):
            raise CreationError('WORKSPACE_CONTENT_MISMATCH', 409)
        if prepared is not None and prepared['state'] == 'prepared':
            return progress  # Runtime independently verifies the proof before cleanup.
        arguments['asset_id'] = asset
        response = self.agent(expected, token, '/internal/workspace/prepare', value=arguments)
        if any(response.get(k) != v for k,v in arguments.items()) or response.get('state') != 'prepared':
            raise CreationError('WORKSPACE_PREPARATION_CONFLICT', 409)
        return progress
