import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
import uuid

spec=importlib.util.spec_from_file_location('drain_test',Path(__file__).parents[1]/'instance_drain.py')
drain=importlib.util.module_from_spec(spec);spec.loader.exec_module(drain)


class DrainTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.env={key:str(uuid.uuid4()) for key in ('INSTANCE_ID','BLENDER_PROJECT_ID','BLENDER_START_OPERATION_ID','BLENDER_POD_UID')}
        self.journal=drain.Journal(self.temp.name,self.env)
        self.request={'instance_id':self.env['INSTANCE_ID'],'start_operation_id':self.env['BLENDER_START_OPERATION_ID'],
                      'operation_id':str(uuid.uuid4()),'generation':'a'*32,'action':'prepare'}

    def captured(self):
        value=self.journal.accept(self.request)
        root=Path(self.temp.name)
        self.snapshot=root/('checkpoints/checkpoint-3-'+'a'*32+'.blend')
        self.target=root/'project/main.blend'
        for path in (self.target,self.snapshot):
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'saved-project')
        value.update(phase='captured',proof={'asset_id':self.snapshot.relative_to(root).as_posix(),
            **drain.content(self.snapshot),'scene_version':4,'pid':123,'start_ticks':'99'})
        with self.journal.locked():self.journal.write(value)
        return value

    def test_acceptance_and_write_fence_survive_controller_reconstruction(self):
        accepted=self.journal.accept(self.request)
        self.assertEqual(self.journal.accept(self.request),accepted)
        recovered=drain.Journal(self.temp.name,self.env)
        with self.assertRaisesRegex(drain.DrainError,'INSTANCE_DRAINING'):recovered.guard()
        self.assertFalse(recovered.status(self.request)['frozen'])
        for changes in ({'instance_id':str(uuid.uuid4())},{'start_operation_id':str(uuid.uuid4())},
                        {'operation_id':str(uuid.uuid4())},{'generation':'b'*32},{'extra':True}):
            with self.assertRaises(drain.DrainError):recovered.status(dict(self.request,**changes))

    def test_disk_receipt_is_not_frozen_proof_until_same_process_is_stopped(self):
        self.captured()
        running=lambda pid:{'pid':pid,'start_ticks':'99','state':'S'}
        self.assertFalse(self.journal.status(self.request,running)['frozen'])
        frozen=lambda pid:{**running(pid),'state':'T'}
        result=self.journal.status(self.request,frozen)
        self.assertTrue(result['frozen'])
        self.assertNotIn('pid',result['proof'])
        self.assertNotIn('start_ticks',result['proof'])
        with self.assertRaisesRegex(drain.DrainError,'DRAIN_PROCESS_CHANGED'):
            self.journal.status(self.request,lambda pid:{**frozen(pid),'start_ticks':'100'})
        self.target.write_bytes(b'later-edit')
        with self.assertRaisesRegex(drain.DrainError,'DRAIN_CONTENT_CHANGED'):self.journal.status(self.request,frozen)

    def test_abort_only_reopens_after_resumed_main_thread_and_never_refreezes_old_operation(self):
        self.captured();signals=[]
        inspect=lambda pid:{'pid':pid,'start_ticks':'99','state':'T'}
        request=dict(self.request,action='abort')
        result=self.journal.abort(request,inspect,lambda *args:signals.append(args))
        self.assertEqual(result['phase'],'aborting')
        with self.assertRaisesRegex(drain.DrainError,'INSTANCE_DRAINING'):self.journal.guard()
        self.journal.abort(request,inspect,lambda *args:signals.append(args))
        self.assertEqual(len(signals),2)  # Recover a lost/racing CONT, same process only.
        self.journal.resumed(request)
        self.journal.guard()
        self.journal.abort(request,inspect,lambda *args:signals.append(args))
        self.assertEqual(len(signals),2)
        with self.assertRaisesRegex(drain.DrainError,'DRAIN_ABORTED'):self.journal.accept(self.request)
        next_request=dict(self.request,operation_id=str(uuid.uuid4()))
        self.journal.accept(next_request)
        with self.assertRaisesRegex(drain.DrainError,'DRAIN_ABORTED'):self.journal.accept(self.request)

    def test_process_reuse_blocks_signals_and_leaves_abort_fenced(self):
        self.captured();signals=[]
        with self.assertRaisesRegex(drain.DrainError,'DRAIN_PROCESS_CHANGED'):
            self.journal.abort(dict(self.request,action='abort'),lambda pid:{'pid':pid,'start_ticks':'changed','state':'T'},lambda *a:signals.append(a))
        self.assertEqual(signals,[])
        with self.assertRaisesRegex(drain.DrainError,'INSTANCE_DRAINING'):self.journal.guard()

    def test_seal_requires_frozen_evidence_and_permanently_rejects_abort(self):
        self.captured()
        request=dict(self.request,action='seal')
        inspect=lambda pid:{'pid':pid,'start_ticks':'99','state':'S'}
        status=self.journal.status
        with patch.object(self.journal,'status',side_effect=lambda req:status(req,inspect)):
            with self.assertRaisesRegex(drain.DrainError,'DRAIN_NOT_FROZEN'):self.journal.seal(request)
        inspect=lambda pid:{'pid':pid,'start_ticks':'99','state':'T'}
        with patch.object(self.journal,'status',side_effect=lambda req:status(req,inspect)):
            self.assertEqual(self.journal.seal(request)['phase'],'sealed')
            self.assertEqual(self.journal.seal(request)['phase'],'sealed')
        recovered=drain.Journal(self.temp.name,self.env)
        with self.assertRaisesRegex(drain.DrainError,'DRAIN_ALREADY_STOPPING'):
            recovered.abort(dict(request,action='abort'),inspect,lambda *a:self.fail('sealed process resumed'))
        with self.assertRaisesRegex(drain.DrainError,'INSTANCE_DRAINING'):recovered.guard()

    def test_corrupt_journal_and_symlink_never_reopen_or_overwrite_external_files(self):
        self.journal.accept(self.request)
        self.journal.path.write_text('{corrupt')
        with self.assertRaisesRegex(drain.DrainError,'DRAIN_STATE_UNKNOWN'):self.journal.guard()
        self.journal.path.unlink()
        external=Path(self.temp.name)/'external';external.write_text('preserve')
        self.journal.path.symlink_to(external)
        with self.assertRaises(drain.DrainError):self.journal.accept(self.request)
        self.assertEqual(external.read_text(),'preserve')

    def test_cross_process_lock_prevents_overlapping_journal_transitions(self):
        other=drain.Journal(self.temp.name,self.env)
        with self.journal.locked():
            with self.assertRaisesRegex(drain.DrainError,'DRAIN_BUSY'):other.accept(self.request)
        other.accept(self.request)

    def test_activity_observation_requires_known_idle_jobs_and_modal_state(self):
        bpy=types.SimpleNamespace(data=types.SimpleNamespace(is_dirty=True),
            context=types.SimpleNamespace(window_manager=types.SimpleNamespace(is_interface_locked=False,windows=[])),
            app=types.SimpleNamespace(background=False,is_job_running=lambda job:False))
        self.assertTrue(drain.idle(bpy))
        bpy.data.is_dirty=False;self.assertFalse(drain.idle(bpy))
        for job in ('RENDER','RENDER_PREVIEW','OBJECT_BAKE','COMPOSITE','SHADER_COMPILATION'):
            bpy.app.is_job_running=lambda current:current==job
            with self.assertRaisesRegex(drain.DrainError,'WORKER_BUSY'):drain.idle(bpy)
        bpy.app.is_job_running=lambda job:False
        bpy.context.window_manager.windows=[types.SimpleNamespace(modal_operators=[object()])]
        with self.assertRaisesRegex(drain.DrainError,'WORKER_BUSY'):drain.idle(bpy)
        bpy.context.window_manager.windows=[types.SimpleNamespace()]
        with self.assertRaisesRegex(drain.DrainError,'WORKER_ACTIVITY_UNKNOWN'):drain.idle(bpy)
        bpy.app.background=True
        bpy.app.is_job_running=lambda job:self.fail('background shader query must never execute')
        with self.assertRaisesRegex(drain.DrainError,'WORKER_ACTIVITY_UNKNOWN'):drain.idle(bpy)


if __name__=='__main__':unittest.main()
