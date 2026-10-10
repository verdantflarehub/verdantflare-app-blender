"""GPU-free, idempotent preparation of one Runtime-owned Blender workspace."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import threading
import time
import uuid

from workspace import asset_path, WorkspaceError

PREPARE_FIELDS = {'prepare_id', 'instance_id', 'project_id', 'source_revision_id', 'asset_id', 'sha256', 'size'}


class PreparationError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def valid_id(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


class WorkspaceManager:
    def __init__(self, root, instance_id, max_bytes):
        self.root, self.instance_id, self.max_bytes = Path(root), instance_id, max_bytes
        self.lock = threading.RLock()

    def paths(self):
        # Internal markers are not served by /assets. Reject symlink parents too.
        target = asset_path(self.root, 'project/main.blend', must_exist=False)
        journal = asset_path(self.root, '.verdantflare/prepare.json', must_exist=False)
        return target, journal

    @staticmethod
    def sync_directory(path):
        if os.name == 'posix':
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def write_journal(self, journal, value):
        journal.parent.mkdir(exist_ok=True)
        tmp = journal.with_name('prepare.pending')
        if tmp.is_symlink():
            raise PreparationError('WORKSPACE_PATH_INVALID')
        # Remove a stale temporary name, never truncate a possible hard-link target.
        tmp.unlink(missing_ok=True)
        with tmp.open('x', encoding='utf-8') as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, journal)
        self.sync_directory(journal.parent)
        self.sync_directory(self.root)

    def read_journal(self, journal):
        if not journal.exists():
            return None
        if journal.stat().st_size > 16384:
            raise PreparationError('WORKSPACE_STATE_INVALID')
        try:
            value = json.loads(journal.read_text(encoding='utf-8'))
        except (ValueError, UnicodeError):
            raise PreparationError('WORKSPACE_STATE_INVALID') from None
        if not isinstance(value, dict) or not isinstance(value.get('state'), str) or value['state'] not in {'preparing', 'prepared'}:
            raise PreparationError('WORKSPACE_STATE_INVALID')
        extra = {'state', 'prepared_at'} if value['state'] == 'prepared' else {'state'}
        if set(value) != PREPARE_FIELDS | extra:
            raise PreparationError('WORKSPACE_STATE_INVALID')
        try:
            self.validate_request({k: value[k] for k in PREPARE_FIELDS})
            if value['state'] == 'prepared':
                if not isinstance(value['prepared_at'], str) or datetime.fromisoformat(value['prepared_at']).tzinfo is None:
                    raise ValueError()
        except (ValueError, PreparationError):
            raise PreparationError('WORKSPACE_STATE_INVALID') from None
        return value

    @staticmethod
    def matches(path, size, sha):
        if not path.is_file() or path.is_symlink() or path.stat().st_size != size:
            return False
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest() == sha

    def validate_request(self, request):
        if not isinstance(request, dict) or set(request) != PREPARE_FIELDS or not all(valid_id(request[k]) for k in ('prepare_id', 'instance_id', 'project_id', 'source_revision_id')):
            raise PreparationError('INVALID_ARGUMENT', 400)
        if not valid_id(self.instance_id) or request['instance_id'] != self.instance_id:
            raise PreparationError('INSTANCE_MISMATCH', 403)
        empty = request['asset_id'] is None
        if empty:
            if request['sha256'] is not None or type(request['size']) is not int or request['size'] != 0:
                raise PreparationError('INVALID_ARGUMENT', 400)
        elif (not isinstance(request['asset_id'], str) or not re.fullmatch(r'inbox/[A-Za-z0-9_-]{16}/restore\.blend', request['asset_id'])
              or type(request['size']) is not int or not 0 < request['size'] <= self.max_bytes
              or not isinstance(request['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', request['sha256'])):
            raise PreparationError('INVALID_ARGUMENT', 400)
        return empty

    def prepare(self, request):
        empty = self.validate_request(request)
        with self.lock:
            target, journal = self.paths()
            previous = self.read_journal(journal)
            if previous:
                if any(previous.get(k) != request[k] for k in PREPARE_FIELDS):
                    raise PreparationError('WORKSPACE_PREPARATION_CONFLICT')
                if previous['state'] == 'prepared':
                    return previous  # Never overwrite a subsequently edited scene.
            elif target.exists():
                raise PreparationError('WORKSPACE_ALREADY_INITIALIZED')
            # Publication may have succeeded before the response/journal was saved.
            # Recovery must not depend on the disposable upload still being present.
            published = bool(previous and not empty and target.exists())
            if published and not self.matches(target, request['size'], request['sha256']):
                raise PreparationError('WORKSPACE_CONTENT_MISMATCH')
            source = None if empty or published else asset_path(self.root, request['asset_id'])
            if source and not self.matches(source, request['size'], request['sha256']):
                raise PreparationError('WORKSPACE_CONTENT_MISMATCH')
            # This checks current backend space, not a fictional per-directory hard quota.
            required = 0 if empty or target.exists() else request['size']
            if shutil.disk_usage(self.root).free < required + 64 * 1024 * 1024:
                raise PreparationError('STORAGE_CAPACITY_UNAVAILABLE')
            value = {**request, 'state':'preparing'}
            if previous is None:
                self.write_journal(journal, value)
            if source:
                if target.exists():
                    if not self.matches(target, request['size'], request['sha256']):
                        raise PreparationError('WORKSPACE_CONTENT_MISMATCH')
                else:
                    target.parent.mkdir(exist_ok=True)
                    tmp = asset_path(self.root, 'project/.initial.blend', must_exist=False)
                    tmp.unlink(missing_ok=True)
                    digest, count = hashlib.sha256(), 0
                    with source.open('rb') as src, tmp.open('xb') as dest:
                        while chunk := src.read(1024 * 1024):
                            count += len(chunk)
                            if count > request['size']:
                                raise PreparationError('WORKSPACE_CONTENT_MISMATCH')
                            dest.write(chunk)
                            digest.update(chunk)
                        dest.flush()
                        os.fsync(dest.fileno())
                    if count != request['size'] or digest.hexdigest() != request['sha256']:
                        raise PreparationError('WORKSPACE_CONTENT_MISMATCH')
                    # Exclusive publication: an unexpected existing project is never replaced.
                    os.link(tmp, target)
                    tmp.unlink()
                    self.sync_directory(target.parent)
                    self.sync_directory(self.root)
            elif empty and target.exists():
                raise PreparationError('WORKSPACE_ALREADY_INITIALIZED')
            if published:
                self.sync_directory(target.parent)
            value.update(state='prepared', prepared_at=datetime.now(timezone.utc).isoformat())
            self.write_journal(journal, value)
            return value

    def status(self):
        if not valid_id(self.instance_id):
            raise PreparationError('INSTANCE_BINDING_UNAVAILABLE', 503)
        with self.lock:
            _, journal = self.paths()
            preparation = self.read_journal(journal)
        return {'instance_id':self.instance_id, 'pod_uid':os.environ.get('BLENDER_POD_UID', ''),
                'preparation':preparation, 'storage':self.measure()}

    def measure(self):
        deadline, count, used, seen = time.monotonic()+2, 0, 0, set()
        pending = [self.root]
        try:
            while pending:
                with os.scandir(pending.pop()) as entries:
                    for entry in entries:
                        count += 1
                        if count > 100000 or time.monotonic() > deadline:
                            raise PreparationError('STORAGE_SCAN_LIMIT')
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode):
                            raise PreparationError('STORAGE_SYMLINK_UNMEASURED')
                        if stat.S_ISDIR(info.st_mode):
                            pending.append(entry.path)
                        elif stat.S_ISREG(info.st_mode):
                            key = (info.st_dev, info.st_ino)
                            if key not in seen:
                                seen.add(key)
                                used += info.st_size
                        else:
                            raise PreparationError('STORAGE_SPECIAL_FILE_UNMEASURED')
            return {'value':used, 'unit':'bytes', 'scope':'workspace_file_bytes', 'quality':'fresh',
                    'sampled_at':datetime.now(timezone.utc).isoformat(), 'backend_available_bytes':shutil.disk_usage(self.root).free}
        except (OSError, PreparationError):
            return {'value':None, 'unit':'bytes', 'scope':'workspace_file_bytes', 'quality':'unavailable',
                    'sampled_at':None, 'reason':'STORAGE_MEASUREMENT_UNAVAILABLE', 'backend_available_bytes':None}
