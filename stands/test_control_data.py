"""Portable control and data contracts using synthetic resources only."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import unittest
from unittest import mock

from stands import data_modules, runtime, stand, worker
from stands import test_runtime as runtime_fixtures
from stands import test_deletion as deletion_fixtures
from stands import test_worker as worker_fixtures


class ControlTests(unittest.TestCase):
    setUp = runtime_fixtures.RuntimeTests.setUp
    write_snapshot = runtime_fixtures.RuntimeTests.write_snapshot
    docker = runtime_fixtures.RuntimeTests.docker
    state = runtime_fixtures.RuntimeTests.state
    resource_call = deletion_fixtures.DeletionTests.call

    def prepare(self):
        self.template['lifecycle']['deploy'][0]['argv'] = ['docker', 'compose', 'up', '-d', 'api']
        self.write_snapshot()
        deletion_fixtures.DeletionTests.prepare(self)

    def call(self, argv, log, *, capture=False, capture_stderr=False):
        if argv[:3] == ['docker', 'container', 'restart']:
            self.resource_calls.append(argv)
            for container in self.containers:
                container['State'] = {'Status': 'running', 'Health': {'Status': 'starting' if getattr(self, 'unready', False) else 'healthy'}}
            return ''
        if argv[:3] == ['docker', 'container', 'logs']:
            self.resource_calls.append(argv)
            self.assertTrue(capture_stderr)
            return 'safe log\nPASSWORD=do-not-publish\n'
        return self.resource_call(argv, log, capture=capture)

    def control(self, action, **kwargs):
        with mock.patch.object(runtime.Executor, 'call', side_effect=self.call):
            return runtime.control(self.root, stand_id='one', project='DEMO', target_host='local', action=action, **kwargs)

    def test_restart_only_inspected_deployed_ids_keeps_data_and_projects_safe_status(self):
        self.prepare()
        self.containers[0]['State'] = {'Status': 'exited', 'Health': {'Status': 'unhealthy'}, 'Error': 'secret'}
        result = self.control('restart')
        self.assertEqual(result, {'services': [{'service': 'api', 'state': 'running', 'health': 'healthy'}]})
        self.assertIn(['docker', 'container', 'restart', 'a' * 64], self.resource_calls)
        self.assertTrue(self.volumes)
        self.assertFalse(any('down' in argv or 'up' in argv for argv in self.resource_calls))
        self.assertNotIn('secret', json.dumps(result))

    def test_restart_targets_skip_setup_container_and_external_smoke_runner(self):
        services = {'api': {'depends_on': {'init': {'condition': 'service_completed_successfully'},
                                          'db': {'condition': 'service_healthy'}}},
                    'init': {}, 'db': {}, 'smoke': {}}
        template = {'lifecycle': {'deploy': [{'argv': ['docker', 'compose', 'up', '-d', 'api', 'init']}]},
                    'output_bindings': {'baseUrl': {'service': 'api'}}}
        self.assertEqual(runtime.restart_targets(template, {'services': services}, list(services)),
                         ({'api', 'db'}, {'init'}))

    def test_status_and_logs_are_read_only_tail_is_fixed(self):
        self.prepare()
        self.containers[0]['State'] = {'Status': 'exited', 'Health': {'Status': 'unhealthy'}}
        self.assertEqual(self.control('status')['services'][0]['state'], 'exited')
        self.assertEqual(self.control('logs')['logs'][0][0], 'api')
        logs = next(argv for argv in self.resource_calls if 'logs' in argv)
        self.assertEqual(logs[-4:], ['--tail', '100', '--timestamps', 'a' * 64])
        self.assertFalse(any('restart' in argv or 'down' in argv for argv in self.resource_calls))

    def test_foreign_host_config_or_profile_is_refused_before_restart(self):
        self.prepare()
        for field, value in [('target_host', 'foreign'), ('profile', 'foreign'), ('ports', {'baseUrl': 33333})]:
            runtime.atomic_json(self.root / '.worker-identity.json', dict(self.identity, **{field: value}))
            with self.subTest(field=field), self.assertRaises(stand.StandError):
                self.control('restart')
        runtime.atomic_json(self.root / '.worker-identity.json', self.identity)
        labels = self.containers[0]['Config']['Labels']
        labels['com.docker.compose.project.config_files'] = str(self.root / 'foreign.json')
        with self.assertRaisesRegex(stand.StandError, 'path mismatch'):
            self.control('restart')
        self.assertFalse(any('restart' in argv for argv in self.resource_calls))

    def test_status_of_legacy_owned_workspace_without_explicit_host_field(self):
        self.prepare()
        del self.identity['target_host']
        runtime.atomic_json(self.root / '.worker-identity.json', self.identity)
        self.assertEqual(self.control('status')['services'][0]['service'], 'api')

    def test_changed_effective_or_unselected_service_never_restarts(self):
        self.prepare()
        path = self.root / 'runtime.effective.json'
        original = path.read_text()
        path.write_text('{}')
        with self.assertRaisesRegex(stand.StandError, 'changed effective'):
            self.control('restart')
        path.write_text(original)
        self.containers[0]['Config']['Labels']['com.docker.compose.service'] = 'foreign-service'
        with self.assertRaisesRegex(stand.StandError, 'unexpected container'):
            self.control('restart')
        self.assertFalse(any('restart' in argv for argv in self.resource_calls))

    def test_empty_restart_is_refused_and_cancel_preserves_resources(self):
        self.prepare()
        with self.assertRaises(runtime.Cancelled):
            self.control('restart', cancel=lambda: True)
        self.assertTrue(self.volumes)
        self.containers = []
        with self.assertRaisesRegex(stand.StandError, 'missing'):
            self.control('restart')

    def test_remote_daemon_and_unready_restart_fail_safely(self):
        self.prepare()
        with mock.patch.dict(runtime.os.environ, {'DOCKER_HOST': 'tcp://remote:2375'}):
            with self.assertRaisesRegex(stand.StandError, 'local Docker'):
                self.control('restart')
        self.assertEqual(self.resource_calls, [])
        self.unready = True
        with self.assertRaisesRegex(stand.StandError, 'did not become ready'):
            self.control('restart', timeout=0.01)
        self.assertTrue(self.volumes)


class DataTests(unittest.TestCase):
    setUp = runtime_fixtures.RuntimeTests.setUp
    write_snapshot = runtime_fixtures.RuntimeTests.write_snapshot
    base_docker = runtime_fixtures.RuntimeTests.docker
    state = runtime_fixtures.RuntimeTests.state

    def docker(self, argv, **kwargs):
        if 'PORTAL_DATA_INPUT=/portal-data/input.json' in argv:
            self.events.append('adapter')
            self.adapter_argv = argv
            directory = Path(argv[argv.index('--volume') + 1].rsplit(':/portal-data', 1)[0])
            self.envelope = stand.read_json(directory / 'input.json')
            kwargs['stderr'].write('adapter SECRET_VALUE=hidden\n')
            output = getattr(self, 'adapter_output', {'ok': True, 'environment': {'USER_ID': 123, 'FEATURE': True, 'OPTION': None}})
            if output is not None:
                runtime.atomic_json(directory / 'output.json', output)
            return subprocess.CompletedProcess(argv, getattr(self, 'adapter_exit', 0), 'stdout SECRET_VALUE=hidden')
        return self.base_docker(argv, **kwargs)

    def enable(self, binding=True):
        module = {'title': 'Synthetic users', 'service': 'api', 'format': 'users.v1', 'argv': ['python', 'adapter.py']}
        if binding:
            module['run_binding'] = 'baseUrl'
        self.template['data_modules'] = {'users': module}
        self.write_snapshot()
        self.entries = [{'module': 'users', 'dataset_id': 1, 'version': 2, 'format': 'users.v1', 'data': {'sensitive': 'payload-never-log'}}]
        self.managed = self.root.parent / 'managed'
        self.context = {'project': 'DEMO', 'stand': 'one', 'target_host': 'local'}

    def run_data(self, **kwargs):
        return runtime.run(self.root, ports={'baseUrl': 49123}, test_data=self.entries,
                           data_context=self.context, managed_run_overlays=self.managed, **kwargs)

    def test_apply_protocol_seed_order_fixed_command_overlay_and_private_io(self):
        self.enable()
        state = self.run_data()
        self.assertLess(self.events.index('seed'), self.events.index('adapter'))
        self.assertLess(self.events.index('adapter'), self.events.index('healthcheck'))
        self.assertEqual(self.envelope, {'protocol': 1, 'operation': 'apply', 'project': 'DEMO', 'stand': 'one',
            'target': 'users', 'format': 'users.v1', 'data': {'sensitive': 'payload-never-log'}, 'query': None,
            'dataset': {'id': 1, 'version': 2}})
        self.assertEqual(self.adapter_argv[self.adapter_argv.index('run'):][:4], ['run', '--rm', '--no-deps', '-T'])
        self.assertEqual(self.adapter_argv[-4:], ['--entrypoint', 'python', 'api', 'adapter.py'])
        path = self.managed / 'DEMO/.local/one--baseUrl.yaml'
        self.assertEqual(stand.read_json(path), {'USER_ID': 123, 'FEATURE': True, 'OPTION': None})
        self.assertTrue(path.with_name(path.name + '.portal-owner.json').is_file())
        self.assertEqual(list(self.root.glob('.portal-data-*')), [])
        for path in self.root.rglob('*'):
            if path.is_file():
                content = path.read_text(encoding='utf-8')
                self.assertNotIn('payload-never-log', content)
                self.assertNotIn('SECRET_VALUE=hidden', content)
        self.assertNotIn('USER_ID', json.dumps(state))

    def test_bad_output_or_exit_fails_seed_without_health_or_publication(self):
        for output, exit_code in [(None, 0), ({'ok': False}, 0), ({'ok': True, 'environment': {'USER': {}}}, 0),
                                  ({'ok': True, 'environment': {'__portal_ca_pem': 'bad'}}, 0), ({'ok': True}, 19)]:
            with self.subTest(output=output, exit_code=exit_code):
                self.enable()
                self.adapter_output, self.adapter_exit = output, exit_code
                self.events.clear()
                with self.assertRaises(stand.StandError):
                    self.run_data(retry=(self.root / 'execution.json').exists())
                self.assertEqual(self.state()['attempts'][-1]['stages'][-1]['name'], 'seed')
                self.assertEqual(self.state()['attempts'][-1]['stages'][-1]['status'], 'failed')
                self.assertNotIn('healthcheck', self.events)
                self.assertFalse(self.managed.exists())
                self.assertEqual(list(self.root.glob('.portal-data-*')), [])

    def test_invalid_claim_data_fails_before_docker(self):
        self.enable()
        entries = self.entries
        for change in [('format', 'wrong'), ('dataset_id', True), ('version', 0), ('module', 'unknown'), ('data', [])]:
            self.entries = [dict(entries[0], **{change[0]: change[1]})]
            with self.subTest(change=change), self.assertRaises(stand.StandError):
                self.run_data()
        self.entries = entries * 2
        with self.assertRaisesRegex(stand.StandError, 'duplicate'):
            self.run_data()
        self.entries = [dict(entries[0], data={'huge': 'x' * (2 * 1024 * 1024)})]
        with self.assertRaisesRegex(stand.StandError, '2 MiB'):
            self.run_data()
        self.assertEqual(self.commands, [])

    def test_run_binding_requires_opt_in_before_any_docker(self):
        self.enable()
        with self.assertRaisesRegex(stand.StandError, 'managed_run_overlays'):
            runtime.run(self.root, test_data=self.entries, data_context=self.context)
        self.assertEqual(self.commands, [])

    def test_modified_manual_overlay_is_preserved_and_cleanup_checks_provenance(self):
        self.enable()
        self.run_data()
        path = self.managed / 'DEMO/.local/one--baseUrl.yaml'
        path.write_text('{"MANUAL": true}')
        with self.assertRaisesRegex(stand.StandError, 'modified settings'):
            self.run_data(retry=True)
        data_modules.cleanup(self.managed, self.snapshot, project='DEMO', stand_id='one', target_host='local', workspace=self.root)
        self.assertEqual(path.read_text(), '{"MANUAL": true}')

    def test_unchanged_generated_overlay_cleanup_and_idempotent_retry(self):
        self.enable()
        self.run_data()
        self.run_data(retry=True)
        path = self.managed / 'DEMO/.local/one--baseUrl.yaml'
        data_modules.cleanup(self.managed, self.snapshot, project='DEMO', stand_id='one', target_host='other', workspace=self.root)
        self.assertTrue(path.exists())
        data_modules.cleanup(self.managed, self.snapshot, project='DEMO', stand_id='one', target_host='local', workspace=self.root)
        self.assertFalse(path.exists())
        self.assertFalse(path.with_name(path.name + '.portal-owner.json').exists())

    def test_all_outputs_validate_before_any_overlay_is_published(self):
        self.enable()
        self.template['data_modules']['other'] = dict(self.template['data_modules']['users'])
        self.template['data_modules']['other'].pop('run_binding')
        self.entries.append(dict(self.entries[0], module='other'))
        self.write_snapshot()
        original = self.docker
        count = 0
        def second_invalid(argv, **kwargs):
            nonlocal count
            if 'PORTAL_DATA_INPUT=/portal-data/input.json' in argv:
                count += 1
                if count == 2:
                    self.adapter_output = {'ok': False}
            return original(argv, **kwargs)
        with mock.patch.object(runtime.subprocess, 'run', side_effect=second_invalid):
            with self.assertRaises(stand.StandError):
                self.run_data()
        self.assertFalse(self.managed.exists())

    def test_publication_failure_rolls_back_all_generated_targets(self):
        self.enable()
        original = runtime.atomic_json
        def fail_provenance(path, value):
            if str(path).endswith('.portal-owner.json'):
                raise OSError('synthetic write failure')
            return original(path, value)
        with mock.patch.object(runtime, 'atomic_json', side_effect=fail_provenance):
            with self.assertRaises(OSError):
                data_modules.publish(self.managed, [('baseUrl', {'USER': 1})], project='DEMO', stand_id='one',
                                     target_host='local', workspace=self.root)
        self.assertFalse((self.managed / 'DEMO/.local/one--baseUrl.yaml').exists())

    def test_template_rejects_unsafe_modules_and_binding_collisions(self):
        self.enable()
        good = copy.deepcopy(self.template)
        cases = [{}, {'../bad': good['data_modules']['users']}, {'users': dict(good['data_modules']['users'], argv=['-bad'])},
                 {'users': dict(good['data_modules']['users'], argv=['python', ''])},
                 {'users': dict(good['data_modules']['users'], format='a/b')},
                 {'users': dict(good['data_modules']['users'], service='missing')},
                 {'users': dict(good['data_modules']['users'], run_binding='missing')},
                 {'users': good['data_modules']['users'], 'other': good['data_modules']['users']}]
        for modules in cases:
            with self.subTest(modules=modules), self.assertRaises(stand.StandError):
                stand.validate_template(dict(good, data_modules=modules))


class WorkerControlTests(unittest.TestCase):
    setUp = worker_fixtures.WorkerTests.setUp
    finishes = worker_fixtures.WorkerTests.finishes

    def control_job(self, action='logs'):
        self.workspace.mkdir(parents=True)
        return {'id': 5, 'kind': 'stand-control', 'spec': {'schema_version': 1, 'stand': 'stand-one', 'project': 'DEMO',
                'target_host': 'local', 'action': action, 'lock': action == 'restart', 'parent_job_id': None}}

    def test_control_uses_normal_finish_and_sanitizes_sensitive_lines_urls_paths(self):
        job = self.control_job()
        text = 'safe log\npassword=do-not-publish\nsecret_token=value\nURL https://user:pass@example.test/a\nC:\\private\\file /etc/private/file\n'
        with mock.patch.object(runtime, 'control', return_value={'logs': [('api', text)]}):
            self.assertEqual(self.worker.job(job), 0)
        self.assertEqual(self.api.calls[-1][1], 'jobs/5/finish')
        self.assertEqual(self.finishes(), [{'status': 'done'}])
        logs = '\n'.join(value['text'] for _, path, value, _ in self.api.calls if path.endswith('/log'))
        for private in ('do-not-publish', 'secret_token', 'user:pass', 'C:\\private', '/etc/private'):
            self.assertNotIn(private, logs)
        self.assertIn('safe log', logs)

    def test_control_cancel_and_lease_loss_do_not_finish_as_success(self):
        job = self.control_job('status')
        with mock.patch.object(runtime, 'control', side_effect=worker.LeaseLost('lost')):
            with self.assertRaises(worker.LeaseLost):
                self.worker.job(job)
        self.assertEqual(self.finishes(), [])
        self.api.cancel = True
        with mock.patch.object(runtime, 'control') as control:
            self.assertEqual(self.worker.job(job), 2)
        control.assert_not_called()
        self.assertEqual(self.finishes(), [{'status': 'cancelled', 'message': 'worker: cancelled; containers/data retained'}])

    def test_managed_root_must_be_isolated_from_source_and_work_root(self):
        for path in (self.repo, self.config['work_root'], self.config['work_root'] / 'overlays', self.repo / 'overlays'):
            with self.subTest(path=path), self.assertRaises(stand.StandError):
                worker.validate_config(dict(self.raw_config, managed_run_overlays=str(path)))


if __name__ == '__main__':
    unittest.main()
