"""Verify and load the exact managed working copy before registering the adapter."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import stat
import uuid

proof = None


def identifier(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value or uuid.UUID(value).int == 0:
        raise ValueError('invalid managed startup identity')
    return value


def safe_path(root, relative):
    path = root
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink():
            raise ValueError('managed workspace contains a symlink')
    return path


def digest(path):
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        raise ValueError('managed project is not a regular nonempty file')
    sha = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            sha.update(chunk)
    return info.st_size, sha.hexdigest()


def load(bpy, env):
    """Return proof only after the requested bytes have loaded successfully.

    Legacy fixed workers retain their existing load path. Managed workers never
    fall back to an empty scene on missing, altered or incompatible content.
    """
    root = Path(env.get('WORKSPACE_ROOT', '/workspace')).resolve(strict=True)
    target = safe_path(root, 'project/main.blend')
    if not env.get('BLENDER_START_OPERATION_ID'):
        if target.is_file():
            bpy.ops.wm.open_mainfile(filepath=str(target), use_scripts=False)
        return None
    identity = {key: identifier(env.get(var)) for key, var in (
        ('start_operation_id', 'BLENDER_START_OPERATION_ID'),
        ('create_operation_id', 'BLENDER_CREATE_OPERATION_ID'),
        ('instance_id', 'INSTANCE_ID'), ('project_id', 'BLENDER_PROJECT_ID'),
        ('pod_uid', 'BLENDER_POD_UID'), ('source_revision_id', 'BLENDER_SOURCE_REVISION_ID'))}
    journal = safe_path(root, '.verdantflare/prepare.json')
    if not journal.is_file() or journal.stat().st_size > 16384:
        raise ValueError('managed preparation journal missing')
    prepared = json.loads(journal.read_text(encoding='utf-8'))
    if not isinstance(prepared, dict) or any(prepared.get(k) != v for k, v in (
        ('prepare_id', identity['create_operation_id']), ('instance_id', identity['instance_id']),
        ('project_id', identity['project_id']), ('source_revision_id', identity['source_revision_id']),
        ('state', 'prepared'))):
        raise ValueError('managed preparation identity mismatch')
    empty = env.get('BLENDER_START_EMPTY')
    sha, size = env.get('BLENDER_START_SHA256', ''), env.get('BLENDER_START_SIZE', '')
    if empty == 'true':
        if sha != '' or size != '0' or prepared.get('asset_id') is not None or prepared.get('sha256') is not None or prepared.get('size') != 0 or target.exists():
            raise ValueError('managed empty source mismatch')
        result = bpy.ops.wm.read_factory_settings(use_empty=True)
        if result != {'FINISHED'} or bpy.data.filepath:
            raise ValueError('managed empty scene did not load')
        loaded_size, loaded_sha = 0, ''
    elif empty == 'false':
        if not re.fullmatch(r'[0-9a-f]{64}', sha) or not re.fullmatch(r'[1-9][0-9]{0,18}', size):
            raise ValueError('managed content binding invalid')
        loaded_size, loaded_sha = digest(target)
        if (loaded_size, loaded_sha) != (int(size), sha):
            raise ValueError('managed content differs from requested working copy')
        result = bpy.ops.wm.open_mainfile(filepath=str(target), load_ui=False, use_scripts=False)
        if result != {'FINISHED'} or Path(bpy.data.filepath).resolve() != target or digest(target) != (loaded_size, loaded_sha):
            raise ValueError('managed project did not load unchanged')
    else:
        raise ValueError('managed startup source type missing')
    return {**identity, 'empty': empty == 'true', 'sha256': loaded_sha, 'size': loaded_size}
