"""Persistent, execution-scoped edit fence and Linux process freeze evidence."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import uuid


class DrainError(Exception):
    pass


def identifier(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value and uuid.UUID(value).int != 0
    except (ValueError, AttributeError):
        return False


def safe(root, relative):
    path = root
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink():
            raise DrainError('DRAIN_STATE_UNKNOWN')
    return path


def content(path):
    if not path.is_file() or path.stat().st_size <= 0:
        raise DrainError('DRAIN_CONTENT_CHANGED')
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'sha256':digest, 'size':path.stat().st_size}


def process(pid):
    if type(pid) is not int or pid <= 1:
        raise DrainError('DRAIN_PROCESS_CHANGED')
    root = Path('/proc') / str(pid)
    try:
        fields = (root/'stat').read_text().rsplit(')',1)[1].split()
        if root.stat().st_uid != os.getuid() or Path(os.readlink(root/'exe')).name != 'blender':
            raise DrainError('DRAIN_PROCESS_CHANGED')
        return {'pid':pid,'start_ticks':fields[19],'state':fields[0]}
    except (OSError, IndexError, ValueError):
        raise DrainError('DRAIN_PROCESS_CHANGED') from None


def continue_process(proof):
    """pidfd prevents PID reuse between identity observation and the signal."""
    descriptor = os.pidfd_open(proof['pid'])
    try:
        if process(proof['pid'])['start_ticks'] != proof['start_ticks']:
            raise DrainError('DRAIN_PROCESS_CHANGED')
        signal.pidfd_send_signal(descriptor, signal.SIGCONT)
    finally:
        os.close(descriptor)


class Journal:
    def __init__(self, root=None, env=None):
        env = os.environ if env is None else env
        self.root = Path(root or env.get('WORKSPACE_ROOT','/workspace')).resolve()
        self.binding = {key:env.get(var) for key,var in (
            ('instance_id','INSTANCE_ID'), ('project_id','BLENDER_PROJECT_ID'),
            ('start_operation_id','BLENDER_START_OPERATION_ID'), ('pod_uid','BLENDER_POD_UID'))}

    @property
    def managed(self):
        return all(identifier(value) for value in self.binding.values())

    @property
    def path(self):
        if not self.managed:
            raise DrainError('DRAIN_UNMANAGED_INSTANCE')
        return safe(self.root,'.verdantflare/drain-'+self.binding['start_operation_id']+'.json')

    def read(self):
        if not self.managed:
            return None
        path = self.path
        if not path.exists():
            return None
        try:
            if not path.is_file() or path.stat().st_size > 65536:
                raise ValueError()
            value=json.loads(path.read_text(encoding='utf-8'))
            if (not isinstance(value,dict) or any(value.get(k)!=v for k,v in self.binding.items())
                    or not identifier(value.get('operation_id')) or not re.fullmatch(r'[0-9a-f]{32}',value.get('generation',''))
                    or value.get('phase') not in {'accepted','captured','sealed','aborting','aborted'}
                    or not isinstance(value.get('aborted'),list) or len(value['aborted'])>1024
                    or not all(identifier(v) for v in value['aborted'])):
                raise ValueError()
            return value
        except (OSError, ValueError, TypeError):
            raise DrainError('DRAIN_STATE_UNKNOWN') from None

    @contextmanager
    def locked(self):
        import fcntl  # The deployed worker is Linux; legacy observers can import elsewhere.
        path=self.path
        path.parent.mkdir(parents=True,exist_ok=True)
        lock=safe(self.root,'.verdantflare/drain-'+self.binding['start_operation_id']+'.lock')
        with lock.open('a+b') as stream:
            try:
                fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                raise DrainError('DRAIN_BUSY') from None
            try:
                yield
            finally:
                fcntl.flock(stream,fcntl.LOCK_UN)

    def write(self,value):
        path=self.path
        tmp=safe(self.root,'.verdantflare/drain-'+self.binding['start_operation_id']+'.partial')
        with tmp.open('w',encoding='utf-8') as stream:
            json.dump(value,stream,sort_keys=True,allow_nan=False)
            stream.flush();os.fsync(stream.fileno())
        os.replace(tmp,path)
        directory=os.open(path.parent,os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def guard(self):
        value=self.read()
        if value and value['phase']!='aborted':
            raise DrainError('INSTANCE_DRAINING')

    def match(self,request,value):
        if (not isinstance(request,dict) or set(request)!={'instance_id','start_operation_id','operation_id','generation','action'}
                or request['instance_id']!=self.binding['instance_id'] or request['start_operation_id']!=self.binding['start_operation_id']
                or not identifier(request['operation_id']) or not isinstance(request['generation'],str)
                or not re.fullmatch(r'[0-9a-f]{32}',request['generation'])
                or request['action'] not in {'prepare','status','abort','seal'}):
            raise DrainError('INVALID_ARGUMENT')
        if value and any(request[k]!=value[k] for k in ('operation_id','generation')):
            raise DrainError('DRAIN_OPERATION_CONFLICT')

    def accept(self,request):
        with self.locked():
            value=self.read()
            self.match(request,None)
            if value and request['operation_id'] in value['aborted']:
                raise DrainError('DRAIN_ABORTED')
            if value and value['operation_id']==request['operation_id']:
                self.match(request,value)
                return value
            if value and value['phase']!='aborted':
                raise DrainError('DRAIN_OPERATION_CONFLICT')
            history=value['aborted'] if value else []
            if len(history)>=1024:
                raise DrainError('DRAIN_STATE_UNKNOWN')
            value={**self.binding,'operation_id':request['operation_id'],'generation':request['generation'],'phase':'accepted','aborted':history}
            self.write(value)
            return value

    def status(self,request,inspect=process):
        value=self.read();self.match(request,value)
        if value is None:
            raise DrainError('DRAIN_NOT_FOUND')
        result={k:value[k] for k in (*self.binding,'operation_id','generation','phase')}
        result['frozen']=False
        if value['phase'] in {'captured','sealed'}:
            proof=value.get('proof')
            if (not isinstance(proof,dict) or type(proof.get('scene_version')) is not int or proof['scene_version']<0
                    or not re.fullmatch(r'checkpoints/checkpoint-[0-9]+-[0-9a-f]{32}\.blend',proof.get('asset_id',''))):
                raise DrainError('DRAIN_STATE_UNKNOWN')
            live=inspect(proof.get('pid'))
            if live['start_ticks']!=proof.get('start_ticks'):
                raise DrainError('DRAIN_PROCESS_CHANGED')
            if live['state']=='T':
                expected={k:proof[k] for k in ('sha256','size')}
                if content(safe(self.root,'project/main.blend'))!=expected or content(safe(self.root,proof['asset_id']))!=expected:
                    raise DrainError('DRAIN_CONTENT_CHANGED')
                result.update(frozen=True,proof={k:proof[k] for k in ('asset_id','sha256','size','scene_version')})
        return result

    def abort(self,request,inspect=process,send=None):
        with self.locked():
            value=self.read();self.match(request,value)
            if value is None:
                raise DrainError('DRAIN_NOT_FOUND')
            if value['phase']=='aborted':
                return value
            if value['phase']=='sealed':
                raise DrainError('DRAIN_ALREADY_STOPPING')
            value['phase']='aborting';self.write(value)
            proof=value.get('proof')
            if proof:
                live=inspect(proof.get('pid'))
                if live['start_ticks']!=proof.get('start_ticks'):
                    raise DrainError('DRAIN_PROCESS_CHANGED')
                # Retry CONT while aborting. The first signal can race the final
                # self-STOP; success is acknowledged by the resumed adapter only.
                if send is None:
                    continue_process(proof)
                else:
                    send(live['pid'],signal.SIGCONT)
            return value

    def seal(self,request):
        with self.locked():
            result=self.status(request)
            if result['phase'] not in {'captured','sealed'} or not result['frozen']:
                raise DrainError('DRAIN_NOT_FROZEN')
            value=self.read()
            value['phase']='sealed';self.write(value)
            return value

    def resumed(self,request):
        with self.locked():
            value=self.read();self.match(request,value)
            if value is None or value['phase'] not in {'aborting','aborted'}:
                raise DrainError('DRAIN_OPERATION_CONFLICT')
            if value['phase']!='aborted':
                value['phase']='aborted'
                value['aborted'].append(value['operation_id'])
                self.write(value)
            return {'phase':'aborted','operation_id':value['operation_id']}


def idle(bpy):
    """Unknown activity is never interpreted as idle."""
    try:
        # Blender 4.5's shader queue query dereferences an absent draw context
        # in background mode. Managed desktop workers must have a real UI loop.
        if getattr(bpy.app, 'background', None) is not False:
            raise DrainError('WORKER_ACTIVITY_UNKNOWN')
        wm=bpy.context.window_manager
        if type(wm.is_interface_locked) is not bool or type(bpy.data.is_dirty) is not bool:
            raise ValueError()
        if wm.is_interface_locked or any(list(window.modal_operators) for window in wm.windows):
            raise DrainError('WORKER_BUSY')
        for job in ('RENDER','RENDER_PREVIEW','OBJECT_BAKE','COMPOSITE','SHADER_COMPILATION'):
            running=bpy.app.is_job_running(job)
            if type(running) is not bool:
                raise ValueError()
            if running:
                raise DrainError('WORKER_BUSY')
        return bpy.data.is_dirty
    except (AttributeError,ValueError,TypeError,RuntimeError):
        raise DrainError('WORKER_ACTIVITY_UNKNOWN') from None
