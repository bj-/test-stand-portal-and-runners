"""Destructive lifecycle safety with a fake daemon; never deletes live Docker data."""
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from stands import runtime, stand
from stands import test_runtime as fixtures


class DeletionTests(unittest.TestCase):
    setUp = fixtures.RuntimeTests.setUp
    write_snapshot = fixtures.RuntimeTests.write_snapshot
    docker = fixtures.RuntimeTests.docker
    state = fixtures.RuntimeTests.state

    def prepare(self):
        self.execution = runtime.run(self.root, ports={'baseUrl': 49123})
        self.project = self.execution['project_name']
        self.identity = {'stand': 'one', 'project': 'DEMO', 'target_host': 'local',
                         'profile': self.snapshot['profile'], 'template_sha256': self.snapshot['template_sha256'],
                         'ports': {'baseUrl': 49123}}
        runtime.atomic_json(self.root / '.worker-identity.json', self.identity)
        self.volumes = {self.project + '_data': {'Name': self.project + '_data', 'Labels': {
            'com.docker.compose.project': self.project, 'com.docker.compose.volume': 'data'}}}
        self.containers = [{'Id': 'a' * 64, 'Config': {'Labels': {
            'com.docker.compose.project': self.project, 'com.docker.compose.service': 'api',
            'com.docker.compose.project.config_files': str(self.root / 'runtime.effective.json'),
            'com.docker.compose.project.working_dir': str(self.root)}},
            'Mounts': [{'Type': 'volume', 'Name': self.project + '_data'}]}]
        self.resource_calls = []
        self.down_fail = False
        self.foreign_user = False
        self.networks = {}

    def call(self, argv, log, *, capture=False):
        self.resource_calls.append(argv)
        if argv[:2] == ['docker', 'ps']:
            self.assertIn('--no-trunc', argv)
            if any(value.startswith('volume=') for value in argv) and self.foreign_user:
                return 'b' * 64
            return '\n'.join(container['Id'] for container in self.containers)
        if argv[:3] == ['docker', 'container', 'inspect']:
            return json.dumps(self.containers)
        if argv[:3] == ['docker', 'volume', 'ls']:
            return '\n'.join(self.volumes)
        if argv[:3] == ['docker', 'volume', 'inspect']:
            return json.dumps([self.volumes[argv[-1]]])
        if argv[:3] == ['docker', 'network', 'ls']:
            return '\n'.join(self.networks)
        if argv[:3] == ['docker', 'network', 'inspect']:
            return json.dumps([self.networks[argv[-1]]])
        if 'down' in argv:
            if self.down_fail:
                raise stand.StandError('partial down failed')
            self.containers = []
            self.volumes = {}
            return ''
        if argv == ['docker', 'compose', 'version']:
            return 'Docker Compose version v2'
        if argv[:2] == ['docker', 'info']:
            return 'linux'
        raise AssertionError(argv)

    def delete(self, **kwargs):
        with mock.patch.object(runtime.Executor, 'call', side_effect=self.call):
            return runtime.delete(self.root, stand_id='one', project='DEMO', target_host='local', **kwargs)

    def alter_config(self, change):
        path = self.root / 'runtime.effective.json'
        config = stand.read_json(path)
        change(config)
        runtime.atomic_json(path, config)
        state = self.state()
        state['effective_sha256'] = hashlib.sha256(stand.canonical(config)).hexdigest()
        runtime.atomic_json(self.root / 'execution.json', state)

    def assert_no_down(self):
        self.assertFalse(any('down' in argv for argv in self.resource_calls))

    def test_scoped_down_removes_bind_data_preserves_reports_and_retries_idempotently(self):
        self.prepare()
        data = self.root / 'data'
        data.mkdir()
        (data / 'db.sqlite').write_text('stand data')
        report = self.root / self.execution['attempts'][0]['output_dir'] / 'keep.html'
        report.write_text('keep report')
        self.alter_config(lambda config: config['services']['api']['volumes'].append(
            {'type': 'bind', 'source': str(data), 'target': '/data2'}))
        self.assertEqual(self.delete()['status'], 'deleted')
        self.assertFalse(data.exists())
        self.assertEqual(report.read_text(), 'keep report')
        down = next(argv for argv in self.resource_calls if 'down' in argv)
        self.assertEqual(down[-3:], ['down', '--volumes', '--remove-orphans'])
        self.assertEqual(down[down.index('--project-name') + 1], self.project)
        self.assertEqual(down[down.index('--file') + 1], str(self.root / 'runtime.effective.json'))
        self.resource_calls.clear()
        self.assertEqual(self.delete()['status'], 'deleted')
        self.assertEqual(self.resource_calls, [])
        self.assertTrue((self.root / 'snapshot.json').is_file())
        self.assertTrue((self.root / 'contexts/api/compose.yaml').is_file())

    def test_missing_workspace_and_prepared_workspace_need_no_daemon(self):
        missing = self.root / 'never-built'
        self.assertEqual(runtime.delete(missing, stand_id='one', project='DEMO', target_host='local', workspace_required=False)['status'], 'deleted')
        with self.assertRaisesRegex(stand.StandError, 'workspace is missing'):
            runtime.delete(missing, stand_id='one', project='DEMO', target_host='local', workspace_required=True)
        runtime.atomic_json(self.root / '.worker-identity.json', {
            'stand': 'one', 'project': 'DEMO', 'profile': self.snapshot['profile'],
            'template_sha256': self.snapshot['template_sha256']})
        calls = len(self.commands)
        self.assertEqual(runtime.delete(self.root, stand_id='one', project='DEMO', target_host='local')['status'], 'deleted')
        self.assertEqual(len(self.commands), calls)

    def test_identity_host_snapshot_hash_and_lock_fail_before_daemon(self):
        self.prepare()
        for field, value in (('stand', 'another'), ('project', 'OTHER'), ('target_host', 'other-host'), ('template_sha256', '0' * 64)):
            with self.subTest(field=field):
                runtime.atomic_json(self.root / '.worker-identity.json', dict(self.identity, **{field: value}))
                with self.assertRaises(stand.StandError):
                    self.delete()
                self.assert_no_down()
        runtime.atomic_json(self.root / '.worker-identity.json', self.identity)
        lock = self.root / '.execution.lock'
        lock.write_text('another worker')
        with self.assertRaisesRegex(stand.StandError, 'locked'):
            self.delete()
        self.assertEqual(lock.read_text(), 'another worker')

    def test_changed_effective_hash_and_foreign_fixed_volume_fail_before_down(self):
        self.prepare()
        path = self.root / 'runtime.effective.json'
        original = path.read_text()
        path.write_text('{}')
        with self.assertRaisesRegex(stand.StandError, 'changed effective'):
            self.delete()
        path.write_text(original)
        self.alter_config(lambda config: config['volumes']['data'].update(name='foreign_data'))
        with self.assertRaisesRegex(stand.StandError, 'fixed-name'):
            self.delete()
        self.assert_no_down()

    def test_foreign_volume_labels_and_shared_users_block_all_destructive_commands(self):
        self.prepare()
        volume = self.volumes[self.project + '_data']
        volume['Labels']['com.docker.compose.project'] = 'other'
        with self.assertRaisesRegex(stand.StandError, 'foreign named volume'):
            self.delete()
        volume['Labels']['com.docker.compose.project'] = self.project
        self.foreign_user = True
        with self.assertRaisesRegex(stand.StandError, 'shared'):
            self.delete()
        self.assert_no_down()

    def test_container_wrong_configuration_path_and_foreign_orphan_mount_fail(self):
        self.prepare()
        labels = self.containers[0]['Config']['Labels']
        labels['com.docker.compose.project.config_files'] = str(self.root / 'other.json')
        with self.assertRaisesRegex(stand.StandError, 'path mismatch'):
            self.delete()
        labels['com.docker.compose.project.config_files'] = str(self.root / 'runtime.effective.json')
        self.containers[0]['Mounts'][0]['Name'] = 'foreign_database'
        with self.assertRaisesRegex(stand.StandError, 'foreign named volume'):
            self.delete()
        self.assert_no_down()

    def test_network_requires_matching_ownership_and_only_project_containers(self):
        self.prepare()
        name = self.project + '_default'
        network = {'Name': name, 'Labels': {'com.docker.compose.project': self.project,
                   'com.docker.compose.network': 'default'}, 'Containers': {'a' * 64: {}}}
        self.networks[name] = network
        network['Labels']['com.docker.compose.project'] = 'foreign'
        with self.assertRaisesRegex(stand.StandError, 'foreign network'):
            self.delete()
        network['Labels']['com.docker.compose.project'] = self.project
        network['Containers']['b' * 64] = {}
        with self.assertRaisesRegex(stand.StandError, 'network is shared'):
            self.delete()
        self.assert_no_down()
        del network['Containers']['b' * 64]
        self.assertEqual(self.delete()['status'], 'deleted')

    def test_failed_down_leaves_bind_data_and_retry_removes_it(self):
        self.prepare()
        data = self.root / 'data.sqlite'
        data.write_text('keep until complete down')
        self.alter_config(lambda config: config['services']['api']['volumes'].append(
            {'type': 'bind', 'source': str(data), 'target': '/data2'}))
        self.down_fail = True
        with self.assertRaisesRegex(stand.StandError, 'partial down'):
            self.delete()
        self.assertEqual(data.read_text(), 'keep until complete down')
        self.assertEqual(self.state()['deletions'][-1]['status'], 'failed')
        self.assertNotEqual(self.state()['status'], 'deleted')
        self.down_fail = False
        self.assertEqual(self.delete()['status'], 'deleted')
        self.assertFalse(data.exists())

    def test_external_bind_path_and_metadata_bind_are_rejected(self):
        self.prepare()
        for path in (self.root.parent, self.root / 'snapshot.json', self.root / 'contexts/api'):
            original = (self.root / 'runtime.effective.json').read_text()
            original_state = self.state()
            self.alter_config(lambda config: config['services']['api']['volumes'].append(
                {'type': 'bind', 'source': str(path), 'target': '/data2'}))
            with self.subTest(path=path), self.assertRaises(stand.StandError):
                self.delete()
            self.assert_no_down()
            (self.root / 'runtime.effective.json').write_text(original)
            runtime.atomic_json(self.root / 'execution.json', original_state)

    @unittest.skipIf(os.name == 'nt', 'POSIX process groups')
    def test_interrupted_compose_kills_owned_process_group_including_plugins(self):
        process = mock.Mock(pid=4321)
        with mock.patch.object(runtime.os, 'killpg', create=True) as killpg:
            runtime.terminate_command(process, process_group=True)
        self.assertEqual(killpg.call_args_list, [mock.call(4321, runtime.signal.SIGTERM), mock.call(4321, runtime.signal.SIGKILL)])
        self.assertEqual(process.wait.call_count, 2)


if __name__ == '__main__':
    unittest.main()
