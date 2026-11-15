"""Verify and load the exact managed working copy before registering the adapter."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import stat
import uuid

proof = None
wake_state = None


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


def _load_hibernate_wake(bpy, env, root, identity):
    """Restore an isolated hibernate checkpoint without touching project/main.blend."""
    global wake_state
    manifest_path = safe_path(root, '.verdantflare/hibernate.json')
    if not manifest_path.is_file() or manifest_path.stat().st_size > 65536:
        raise ValueError('hibernate manifest missing')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if (
        not isinstance(manifest, dict)
        or manifest.get('schema_version') != 1
        or not isinstance(manifest.get('generation'), str)
        or not re.fullmatch(r'[0-9a-f]{32}', manifest['generation'])
        or type(manifest.get('scene_version')) is not int
        or manifest['scene_version'] < 0
        or not isinstance(manifest.get('checkpoint_asset_id'), str)
        or not re.fullmatch(r'checkpoints/auto_hibernate_gen[0-9a-f]{32}_v[0-9]+\.blend', manifest['checkpoint_asset_id'])
        or not isinstance(manifest.get('sha256'), str)
        or not re.fullmatch(r'[0-9a-f]{64}', manifest['sha256'])
        or type(manifest.get('size')) is not int
        or manifest['size'] <= 0
    ):
        raise ValueError('hibernate manifest invalid')
    expected_instance = identity['instance_id'] if identity else env.get('INSTANCE_ID', '')
    expected_pod_uid = identity['pod_uid'] if identity else env.get('BLENDER_POD_UID', '')
    if expected_instance and manifest.get('instance_id') != expected_instance:
        raise ValueError('hibernate manifest instance mismatch')
    if expected_pod_uid and manifest.get('pod_uid') != expected_pod_uid:
        raise ValueError('hibernate manifest pod mismatch')
    checkpoint = safe_path(root, manifest['checkpoint_asset_id'])
    ckpt_size, ckpt_sha = digest(checkpoint)
    if (ckpt_size, ckpt_sha) != (manifest['size'], manifest['sha256']):
        raise ValueError('hibernate checkpoint integrity mismatch')

    # Stage inside the original base directory (default project/) so Blender's
    # '//' relative texture/library paths resolve identically without modifying
    # /workspace/project/main.blend.
    stage_rel = 'project/.vf_wake_stage.blend'
    original_fp = manifest.get('original_filepath') or ''
    if isinstance(original_fp, str) and original_fp:
        try:
            orig_resolved = Path(original_fp).resolve()
            if root in orig_resolved.parents:
                parent_rel = orig_resolved.parent.relative_to(root).as_posix()
                stage_rel = f'{parent_rel}/.vf_wake_stage.blend' if parent_rel != '.' else '.vf_wake_stage.blend'
        except (OSError, ValueError):
            stage_rel = 'project/.vf_wake_stage.blend'
    stage_path = safe_path(root, stage_rel)
    stage_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with checkpoint.open('rb') as src, stage_path.open('wb') as dst:
            for chunk in iter(lambda: src.read(1024 * 1024), b''):
                dst.write(chunk)
        if digest(stage_path) != (manifest['size'], manifest['sha256']):
            raise ValueError('hibernate stage integrity mismatch')
        result = bpy.ops.wm.open_mainfile(filepath=str(stage_path), load_ui=False, use_scripts=False)
        if result != {'FINISHED'} or Path(bpy.data.filepath).resolve() != stage_path:
            raise ValueError('hibernate checkpoint did not load')
    finally:
        stage_path.unlink(missing_ok=True)

    wake_state = manifest
    return manifest.get('startup_proof')


def load(bpy, env):
    """Return proof only after the requested bytes have loaded successfully.

    Legacy fixed workers retain their existing load path. Managed workers never
    fall back to an empty scene on missing, altered or incompatible content.
    """
    global wake_state
    wake_state = None
    root = Path(env.get('WORKSPACE_ROOT', '/workspace')).resolve(strict=True)
    target = safe_path(root, 'project/main.blend')
    if not env.get('BLENDER_START_OPERATION_ID'):
        if env.get('BLENDER_WAKE_RESTORE') == '1':
            return _load_hibernate_wake(bpy, env, root, None)
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
    if env.get('BLENDER_WAKE_RESTORE') == '1':
        restored_proof = _load_hibernate_wake(bpy, env, root, identity)
        if not isinstance(restored_proof, dict) or any(restored_proof.get(k) != v for k, v in identity.items()):
            raise ValueError('hibernate startup proof mismatch')
        return restored_proof
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
