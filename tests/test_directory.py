import json
from pathlib import Path
import tempfile
import threading
import unittest

from test_application import app


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'instances.db'
        self.store = app.Store(self.path)
        self.directory = app.directory.Directory(self.store, app.content.uuid7)
        self.org, self.subject, self.project = [app.content.uuid7() for _ in range(3)]
        self.config = {'id':app.content.uuid7(), 'organization_id':self.org, 'project_id':self.project,
                       'grants':{self.subject:'edit'}, 'managers':[self.subject], 'name':'Blender Test'}
        self.input = {'name':'Blender Test', 'project_id':self.project, 'source_revision_id':app.content.uuid7(), 'profile_id':'blender-standard'}

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def create(self):
        return self.directory.begin_create(self.org, self.subject, 'create-key', self.input, 'blenderTest', self.config)

    def version(self):
        return self.directory.entries()['blenderTest']['_directory']['version']

    def advance(self, operation, final_state=None, **kw):
        return self.directory.advance(operation['id'], self.version(), 'complete' if final_state else 'preparing',
                                      {'verified':True}, final_state, **kw)

    def test_declared_acl_refresh_and_non_reusable_identity(self):
        self.directory.sync_declared({'blenderTest':self.config})
        initial = self.version()
        self.directory.sync_declared({'blenderTest':self.config})
        self.assertEqual(self.version(), initial)
        revoked = dict(self.config, grants={}, managers=[])
        self.directory.sync_declared({'blenderTest':revoked})
        self.assertEqual(self.directory.entries()['blenderTest']['grants'], {})
        self.assertEqual(self.version(), initial+1)
        self.directory.sync_declared({})
        self.assertEqual(self.directory.entries(), {})
        for alias, config in [('blenderOther', self.config), ('blenderTest', dict(self.config, id=app.content.uuid7())),
                              ('blenderTest', dict(self.config, project_id=app.content.uuid7()))]:
            with self.assertRaisesRegex(app.directory.Fault, 'IDENTITY_CONFLICT'):
                self.directory.sync_declared({alias:config})
        self.assertEqual(self.directory.entries(), {})  # Conflicting imports roll back.
        self.directory.sync_declared({'blenderTest':self.config})
        self.assertEqual(self.directory.entries()['blenderTest']['id'], self.config['id'])

    def test_create_replay_and_recovery_preserve_fixed_source_and_operation(self):
        operation = self.create()
        self.advance(operation)
        self.store.db.close()
        self.store = app.Store(self.path)
        self.directory = app.directory.Directory(self.store, app.content.uuid7)
        self.assertEqual([o['id'] for o in self.directory.pending()], [operation['id']])
        replay = self.directory.begin_create(self.org, self.subject, 'create-key', self.input, 'blenderDifferent', dict(self.config, id=app.content.uuid7()))
        self.assertEqual(replay['id'], operation['id'])
        self.assertEqual(json.loads(replay['input'])['source_revision_id'], self.input['source_revision_id'])
        self.assertEqual(len(self.directory.entries()), 1)
        with self.assertRaisesRegex(app.directory.Fault, 'IDEMPOTENCY_CONFLICT'):
            self.directory.begin_create(self.org, self.subject, 'create-key', dict(self.input, source_revision_id='different'), 'blenderTest', self.config)

    def test_commands_are_versioned_and_single_flight(self):
        self.advance(self.create(), 'stopped')
        version = self.version()
        operation = self.directory.begin_command(self.config['id'], self.org, self.subject, 'start-key', 'start', version, {})
        replay = self.directory.begin_command(self.config['id'], self.org, self.subject, 'start-key', 'start', version, {})
        self.assertEqual(operation['id'], replay['id'])
        with self.assertRaisesRegex(app.directory.Fault, 'INSTANCE_BUSY'):
            self.directory.begin_command(self.config['id'], self.org, self.subject, 'another-start-key', 'start', self.version(), {})
        with self.assertRaisesRegex(app.directory.Fault, 'STATE_CONFLICT'):
            self.directory.advance(operation['id'], version, 'late-result', {}, 'running')
        self.advance(operation, 'running')
        with self.assertRaisesRegex(app.directory.Fault, 'STATE_CONFLICT'):
            self.directory.begin_command(self.config['id'], self.org, self.subject, 'stale-stop-key', 'stop', version, {})
        with self.assertRaisesRegex(app.directory.Fault, 'INSTANCE_NOT_FOUND'):
            self.directory.begin_command(self.config['id'], app.content.uuid7(), self.subject, 'other-org-key', 'stop', self.version(), {})

    def test_concurrent_start_accepts_only_one_operation(self):
        self.advance(self.create(), 'stopped')
        version, results = self.version(), []
        def start(key):
            try:
                results.append(self.directory.begin_command(self.config['id'], self.org, self.subject, key, 'start', version, {}))
            except app.directory.Fault as exc:
                results.append(str(exc))
        threads = [threading.Thread(target=start, args=(f'concurrent-{n}',)) for n in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(len(self.directory.pending()), 1)

    def test_destroy_retains_workspace_and_cannot_resurrect_or_rebind(self):
        self.advance(self.create(), 'stopped')
        operation = self.directory.begin_command(self.config['id'], self.org, self.subject, 'destroy-key', 'destroy', self.version(), {})
        version = self.version()
        with self.assertRaisesRegex(app.directory.Fault, 'RETENTION_REQUIRED'):
            self.advance(operation, 'deleted')
        self.assertEqual(self.version(), version)
        retained = {'workspace_id':app.content.uuid7(), 'revision_id':self.input['source_revision_id'], 'capacity_bytes':50*1024**3}
        result = self.advance(operation, 'deleted', retained=retained)
        self.assertEqual(self.directory.entries(), {})
        self.assertEqual(self.directory.advance(operation['id'], version, 'complete', {'verified':True}, 'deleted', retained=retained), result)
        with self.store.transaction() as db:
            rows = db.execute('SELECT * FROM retained_workspaces').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['org'],rows[0]['project']), (self.org,self.project))
        self.assertEqual(json.loads(rows[0]['details']), retained)
        with self.assertRaisesRegex(app.directory.Fault, 'IDENTITY_CONFLICT'):
            self.directory.begin_create(self.org,self.subject,'another-create',self.input,'blenderTest',dict(self.config,id=app.content.uuid7()))
        with self.assertRaisesRegex(app.directory.Fault, 'IDENTITY_CONFLICT'):
            self.directory.sync_declared({'blenderTest':self.config})

    def test_execution_cannot_overwrite_authority_or_cross_operation_owner(self):
        op = self.create()
        with self.assertRaisesRegex(app.directory.Fault, 'INVALID_EXECUTION_BINDING'):
            self.advance(op, 'stopped', binding={'project_id':app.content.uuid7()})
        for org, subject in [(app.content.uuid7(),self.subject),(self.org,app.content.uuid7())]:
            with self.assertRaisesRegex(app.directory.Fault, 'OPERATION_NOT_FOUND'):
                self.directory.operation(op['id'],org,subject)
        self.assertEqual(self.directory.operation(op['id'],self.org,self.subject)['id'],op['id'])

    def test_atomic_rollback_preserves_other_declared_entries(self):
        self.directory.sync_declared({'blenderTest':self.config})
        other = dict(self.config, id=app.content.uuid7())
        with self.assertRaisesRegex(app.directory.Fault, 'IDENTITY_CONFLICT'):
            self.directory.sync_declared({'blenderOther':other, 'blenderTest':dict(self.config, organization_id=app.content.uuid7())})
        self.assertEqual(list(self.directory.entries()), ['blenderTest'])


if __name__ == '__main__':
    unittest.main()
