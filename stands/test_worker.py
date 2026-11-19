"""Worker protocol, immutable sources, retries and profile ownership."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from stands import stand, runtime, worker
from stands.test_stand import git, commit


class FakeAPI:
    token = 'secret-bearer-token'

    def __init__(self):
        self.calls = []
        self.cancel = False
        self.lose_snapshot = False
        self.jobs = []

    def request(self, method, path, value=None, *, raw=None):
        self.calls.append((method, path, value, raw))
        if path == 'claim':
            return self.jobs.pop(0) if self.jobs else None
        if path.endswith('/snapshot'):
            if self.lose_snapshot:
                raise worker.LeaseLost('lost')
            return {'build_id': 'build-one'}
        if path.endswith('/log') or path.endswith('/progress'):
            return {'cancel': self.cancel}
        return {'ok': True}


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='host-worker-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / 'source'
        self.repo.mkdir()
        git(self.repo, 'init', '-q', '-b', 'main')
        (self.repo / 'compose.yaml').write_text('services: {}\n')
        self.sha = commit(self.repo, 'first')
        self.catalog = {'schema_version': 1, 'repositories': [
            {'id': 'service-src', 'path': str(self.repo), 'components': ['api']},
            {'id': 'runner-src', 'path': str(self.repo), 'components': ['smoke']}]}
        self.template = stand.read_json(Path(__file__).parent / 'examples/http-smoke.template.json')
        self.spec = {'schema_version': 1, 'stand': 'stand-one', 'project': 'DEMO', 'template': self.template,
            'template_sha256': stand.template_hash(self.template), 'request': {'schema_version': 1,
            'profile': 'laptop', 'components': {}, 'parameters': {}}, 'target_host': 'local',
            'environments': ['feature'], 'ports': {'baseUrl': 24001}, 'lock': True, 'parent_job_id': None}
        self.token = self.base / 'token'
        self.token.write_text(FakeAPI.token)
        self.token.chmod(0o600)
        self.raw_config = {'schema_version': 1, 'portal_url': 'http://127.0.0.1:8110', 'host_id': 'local',
            'token_file': str(self.token), 'work_root': str(self.base / 'private'), 'catalogs': {'DEMO': self.catalog}}
        self.config = worker.validate_config(self.raw_config)
        self.config['work_root'].mkdir()
        self.api = FakeAPI()
        self.worker = worker.Worker(self.config, self.api)
        self.job = {'id': 1, 'kind': 'stand-build', 'spec': self.spec}
        self.workspace = self.config['work_root'] / 'stands' / self.spec['stand']

    def fake_runtime(self, workspace, **kwargs):
        self.assertTrue(any(path.endswith('/snapshot') for _, path, _, _ in self.api.calls))
        self.assertEqual(kwargs['ports'], self.spec['ports'])
        stages = []
        for stage in stand.STAGES:
            record = {'name': stage, 'status': 'running'}
            kwargs['progress'](dict(record))
            record['status'] = 'success'
            stages.append(record)
            kwargs['progress'](dict(record))
        output = workspace / 'results' / 'attempt'
        output.mkdir(parents=True, exist_ok=True)
        for name in ('allure-report.html', 'junit.xml', 'results.json'):
            (output / name).write_text('artifact')
        (workspace / 'logs/attempt').mkdir(parents=True, exist_ok=True)
        return {'attempts': [{'stages': stages, 'output_dir': 'results/attempt', 'logs': 'logs/attempt'}], 'images': [
            {'id': 'sha256:' + 'a' * 64, 'repo_tags': [], 'repo_digests': []}],
            'output_bindings': {'baseUrl': 'http://127.0.0.1:24001/'}}

    def finishes(self):
        return [value for _, path, value, _ in self.api.calls if path.endswith('/finish')]

    def test_full_protocol_snapshot_before_docker_artifacts_before_finish_and_sources_unchanged(self):
        head = git(self.repo, 'rev-parse', 'HEAD')
        (self.repo / 'content.txt').write_text('dirty checkout')
        with mock.patch.object(worker.runtime, 'run', side_effect=self.fake_runtime):
            self.assertEqual(self.worker.job(self.job), 0)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), head)
        self.assertEqual((self.repo / 'content.txt').read_text(), 'dirty checkout')
        self.assertEqual((self.workspace / 'contexts/api/content.txt').read_text(), 'first')
        finish = self.finishes()[-1]
        self.assertEqual(finish['status'], 'done')
        self.assertEqual(len(finish['stages']), 6)
        self.assertTrue(self.api.calls[-2][1].endswith('/log') or self.api.calls[-2][1].endswith('/allure-report.html'))
        self.assertEqual(self.api.calls[-1][1], 'jobs/1/stand/finish')

    def test_prepare_failure_reported_without_snapshot_and_private_messages_redacted(self):
        self.spec['template_sha256'] = 'a' * 64
        self.assertEqual(self.worker.job(self.job), 2)
        self.assertEqual(self.finishes()[-1]['status'], 'failed')
        self.assertEqual(self.finishes()[-1]['stages'], [])
        self.assertFalse(any(path.endswith('/snapshot') for _, path, _, _ in self.api.calls))
        text = self.worker.redact(self.api.token + ' ' + str(self.repo) + ' https://user:pass@private/path')
        self.assertNotIn(self.api.token, text)
        self.assertNotIn(str(self.repo), text)
        self.assertNotIn('user:pass', text)

    def test_lost_snapshot_ack_prevents_runtime_and_false_finish(self):
        self.api.lose_snapshot = True
        with mock.patch.object(worker.runtime, 'run') as run, self.assertRaises(worker.LeaseLost):
            self.worker.job(self.job)
        run.assert_not_called()
        self.assertEqual(self.finishes(), [])

    def test_cancel_before_prepare_retains_sources_and_reports_cancelled(self):
        self.api.cancel = True
        with mock.patch.object(worker.runtime, 'run') as run:
            self.assertEqual(self.worker.job(self.job), 2)
        run.assert_not_called()
        self.assertEqual(self.finishes()[-1]['status'], 'cancelled')

    def test_existing_initial_workspace_fails_and_explicit_retry_uses_original_snapshot(self):
        with mock.patch.object(worker.runtime, 'run', side_effect=self.fake_runtime):
            self.assertEqual(self.worker.job(self.job), 0)
            self.assertEqual(self.worker.job(self.job), 2)
            self.spec['snapshot'] = stand.read_json(self.workspace / 'snapshot.json')
            commit(self.repo, 'second')
            self.assertEqual(self.worker.job(self.job), 0)
        self.assertEqual((self.workspace / 'contexts/api/content.txt').read_text(), 'first')
        self.assertEqual(stand.read_json(self.workspace / 'snapshot.json')['components'][0]['sha'], self.sha)

    def test_retry_rejects_cross_stand_identity(self):
        with mock.patch.object(worker.runtime, 'run', side_effect=self.fake_runtime):
            self.worker.job(self.job)
        self.spec['snapshot'] = stand.read_json(self.workspace / 'snapshot.json')
        identity = stand.read_json(self.workspace / '.worker-identity.json')
        identity['stand'] = 'another-stand'
        runtime.atomic_json(self.workspace / '.worker-identity.json', identity)
        self.assertEqual(self.worker.job(self.job), 2)
        self.assertIn('identity', self.finishes()[-1]['message'])

    def test_snapshot_explicit_sha_is_case_insensitive_and_cannot_be_reassigned(self):
        with mock.patch.object(worker.runtime, 'run', side_effect=self.fake_runtime):
            self.assertEqual(self.worker.job(self.job), 0)
        snapshot = stand.read_json(self.workspace / 'snapshot.json')
        repositories = stand.validate_catalog(self.catalog)
        for length in (40, 64):
            component = snapshot['components'][0]
            component.update(requested_ref='A' * length, sha='a' * length)
            stand.validate_snapshot(snapshot, repositories)
            component['sha'] = 'b' * length
            with self.assertRaisesRegex(stand.StandError, 'explicit requested SHA'):
                stand.validate_snapshot(snapshot, repositories)
            component['requested_ref'] = 'main'
            stand.validate_snapshot(snapshot, repositories)

    def delete_spec(self):
        return dict({key: self.spec[key] for key in ('schema_version', 'stand', 'project', 'target_host', 'lock', 'parent_job_id')},
                    workspace_required=True)

    def test_delete_job_uses_scoped_runtime_and_dedicated_finish(self):
        spec = self.delete_spec()
        with mock.patch.object(runtime, 'delete') as delete:
            self.assertEqual(self.worker.job({'id': 9, 'kind': 'stand-delete', 'spec': spec}), 0)
        delete.assert_called_once_with(self.workspace, stand_id='stand-one', project='DEMO', target_host='local',
                                      workspace_required=True, timeout=900, cancel=self.worker.checkpoint)
        self.assertEqual(self.api.calls[-1][1:3], ('jobs/9/stand/delete-finish', {'status': 'done'}))
        self.assertTrue(any(path == 'jobs/9/log' for _, path, _, _ in self.api.calls))
        self.assertIsNone(self.worker.current)

    def test_delete_wrong_host_or_recovery_metadata_fails_before_runtime(self):
        for changes in ({'target_host': 'another'}, {'lock': False}, {'parent_job_id': 1}, {'extra': True}, {'workspace_required': 1}):
            with self.subTest(changes=changes), mock.patch.object(runtime, 'delete') as delete:
                self.assertEqual(self.worker.job({'id': 9, 'kind': 'stand-delete', 'spec': dict(self.delete_spec(), **changes)}), 2)
                delete.assert_not_called()
                self.assertEqual(self.api.calls[-1][2]['status'], 'failed')

    def test_delete_failure_or_cancellation_never_reports_done(self):
        for error in (stand.StandError('unsafe data'), runtime.Cancelled('cancelled')):
            with self.subTest(error=error), mock.patch.object(runtime, 'delete', side_effect=error):
                self.assertEqual(self.worker.job({'id': 9, 'kind': 'stand-delete', 'spec': self.delete_spec()}), 2)
                self.assertEqual(self.api.calls[-1][2]['status'], 'failed')

    def test_delete_lost_lease_has_no_terminal_callback(self):
        with mock.patch.object(runtime, 'delete', side_effect=worker.LeaseLost('lost')):
            with self.assertRaises(worker.LeaseLost):
                self.worker.job({'id': 9, 'kind': 'stand-delete', 'spec': self.delete_spec()})
        self.assertFalse(any(path.endswith('/delete-finish') for _, path, _, _ in self.api.calls))

    def test_missing_success_allure_is_failed(self):
        def no_report(*args, **kwargs):
            result = self.fake_runtime(*args, **kwargs)
            (args[0] / 'results/attempt/allure-report.html').unlink()
            return result
        with mock.patch.object(worker.runtime, 'run', side_effect=no_report):
            self.assertEqual(self.worker.job(self.job), 2)
        self.assertEqual(self.finishes()[-1]['status'], 'failed')

    def test_failed_smoke_uploads_available_report_before_failed_finish(self):
        def failed_report(*args, **kwargs):
            state = self.fake_runtime(*args, **kwargs)
            state['attempts'][-1]['stages'][-1]['status'] = 'failed'
            runtime.atomic_json(args[0] / 'execution.json', state)
            raise stand.StandError('reporter exit 1')
        with mock.patch.object(worker.runtime, 'run', side_effect=failed_report):
            self.assertEqual(self.worker.job(self.job), 2)
        self.assertEqual(self.finishes()[-1]['status'], 'failed')
        self.assertTrue(any(path.endswith('/artifacts/allure-report.html') for _, path, _, _ in self.api.calls))
        self.assertEqual(self.api.calls[-1][1], 'jobs/1/stand/finish')

    def test_once_idle_claims_only_capability_host_and_cleans_status(self):
        self.assertEqual(self.worker.run(once=True), 0)
        self.assertEqual(self.api.calls[0][2], {'capabilities': ['stand-template-v1', 'stand-template-v1-delete'], 'host_id': 'local'})
        self.assertFalse((self.config['work_root'] / 'status.json').exists())
        self.assertTrue((self.config['work_root'] / '.worker.lock').exists())
        with worker.profile_lock(self.config['work_root']):
            with self.assertRaisesRegex(stand.StandError, 'running worker'):
                with worker.profile_lock(self.config['work_root']):
                    self.fail('must not acquire second OS lock')

    def test_persistent_worker_recovers_claim_lease_loss_and_honors_stop(self):
        self.config['poll_seconds'] = 0.01
        requests = []
        def request(method, path, value=None, **kwargs):
            requests.append(path)
            if len(requests) == 1:
                raise worker.LeaseLost('portal restarting')
            if len(requests) == 2:
                return self.job
            runtime.atomic_json(self.config['work_root'] / '.stop.json', {'run_id': self.worker.run_id})
            return None
        with mock.patch.object(self.api, 'request', side_effect=request), mock.patch.object(self.worker, 'job', return_value=0) as process:
            self.assertEqual(self.worker.run(), 0)
        process.assert_called_once_with(self.job)
        self.assertEqual(requests, ['claim', 'claim', 'claim'])
        self.assertFalse((self.config['work_root'] / 'status.json').exists())

    def test_once_worker_keeps_claim_lease_loss_fail_fast(self):
        with mock.patch.object(self.api, 'request', side_effect=worker.LeaseLost('portal restarting')) as request:
            with self.assertRaises(worker.LeaseLost):
                self.worker.run(once=True)
        self.assertEqual(request.call_count, 1)
        self.assertFalse((self.config['work_root'] / 'status.json').exists())

    def test_stop_requests_are_instance_bound(self):
        path = self.config['work_root'] / '.stop.json'
        runtime.atomic_json(path, {'run_id': 'old-instance'})
        self.assertFalse(self.worker.stopped())
        runtime.atomic_json(path, {'run_id': self.worker.run_id})
        self.assertTrue(self.worker.stopped())

    def test_launcher_run_id_is_validated_and_used_as_instance_identity(self):
        nonce = 'a1' * 16
        instance = worker.Worker(self.config, self.api, run_id=nonce)
        self.assertEqual(instance.run_id, nonce)
        runtime.atomic_json(self.config['work_root'] / '.stop.json', {'run_id': nonce})
        self.assertTrue(instance.stopped())
        for invalid in ('A1' * 16, 'a' * 31, 'a' * 33, '../x', '', 1):
            with self.subTest(run_id=invalid), self.assertRaises(stand.StandError):
                worker.Worker(self.config, self.api, run_id=invalid)

    def test_cli_passes_private_launcher_run_id_to_worker(self):
        config_path = self.base / 'worker.json'
        runtime.atomic_json(config_path, self.raw_config)
        with mock.patch.object(worker, 'Worker') as worker_class:
            worker_class.return_value.run.return_value = 0
            self.assertEqual(worker.main(['--config', str(config_path), '--once', '--run-id', 'a1' * 16]), 0)
        self.assertEqual(worker_class.call_args.kwargs['run_id'], 'a1' * 16)

    def test_config_rejects_unknown_nonfinite_unsafe_paths_urls(self):
        for changes in ({'extra': 1}, {'fetch': 1}, {'poll_seconds': float('inf')}, {'timeout': True},
                        {'work_root': str(self.repo / 'work')}, {'token_file': str(self.repo / 'token')},
                        {'portal_url': 'http://remote.invalid'}, {'portal_url': 'https://user:password@host'}):
            with self.subTest(changes=changes), self.assertRaises(stand.StandError):
                worker.validate_config(dict(self.raw_config, **changes))
        self.assertEqual(worker.validate_config(dict(self.raw_config, catalogs={}))['catalogs'], {})
        self.assertIn('Проект: тест', worker.validate_config(dict(self.raw_config, catalogs={'Проект: тест': self.catalog}))['catalogs'])

    def test_fetch_resolves_remote_new_commit_without_checkout_changes_and_no_stale_fallback(self):
        remote = self.base / 'remote'
        git(self.base, 'clone', '-q', str(self.repo), str(remote))
        git(self.repo, 'remote', 'add', 'origin', str(remote))
        new = commit(remote, 'remote-new')
        head = git(self.repo, 'rev-parse', 'HEAD')
        index = (self.repo / '.git/index').read_bytes()
        worker.fetch_refs(self.repo)
        self.assertEqual(worker.fetched_ref(self.repo, 'main'), new)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), head)
        self.assertEqual((self.repo / '.git/index').read_bytes(), index)
        git(remote, 'tag', 'main')
        worker.fetch_refs(self.repo)
        with self.assertRaisesRegex(stand.StandError, 'ambiguous'):
            worker.fetched_ref(self.repo, 'main')
        self.assertEqual(worker.fetched_ref(self.repo, 'refs/heads/main'), new)
        self.assertEqual(worker.fetched_ref(self.repo, 'refs/tags/main'), new)
        git(remote, 'checkout', '-qb', 'different')
        git(remote, 'branch', '-D', 'main')
        worker.fetch_refs(self.repo)
        with self.assertRaisesRegex(stand.StandError, 'missing'):
            worker.fetched_ref(self.repo, 'refs/heads/main')
        git(self.repo, 'remote', 'set-url', 'origin', str(self.base / 'missing'))
        with self.assertRaisesRegex(stand.StandError, 'no stale fallback'):
            worker.fetch_refs(self.repo)


if __name__ == '__main__':
    unittest.main()
