"""Private host worker for frozen stand-template-v1 jobs (standard library only)."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
from ipaddress import ip_address
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import runtime, stand


class LeaseLost(stand.StandError):
    pass


def is_loopback(hostname):
    if hostname == 'localhost':
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


def private_path(value, label, *, directory=False):
    path = Path(stand.string(value, label))
    if not path.is_absolute():
        stand.fail(label + ': absolute path required')
    for part in (path, *path.parents):
        if stand.is_link(part):
            stand.fail(label + ': symlink/junction forbidden')
    if directory and path.exists() and not path.is_dir():
        stand.fail(label + ': directory required')
    return path.resolve()


def validate_config(value):
    stand.obj(value, ('schema_version', 'portal_url', 'token_file', 'host_id', 'work_root', 'catalogs'),
              ('fetch', 'poll_seconds', 'timeout', 'ref_source', 'publish_ip'), label='worker config')
    stand.version(value['schema_version'], 'worker config')
    config = dict(value)
    url = urllib.parse.urlsplit(stand.string(config['portal_url'], 'portal_url'))
    if (url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path not in ('', '/')
            or (url.scheme == 'http' and not is_loopback(url.hostname))):
        stand.fail('portal_url: HTTPS or local loopback HTTP origin required')
    try:
        url.port
    except ValueError:
        stand.fail('portal_url: invalid port')
    config['portal_url'] = config['portal_url'].rstrip('/')
    stand.identifier(config['host_id'], 'host_id')
    config['work_root'] = private_path(config['work_root'], 'work_root', directory=True)
    config['token_file'] = private_path(config['token_file'], 'token_file')
    for private in (config['work_root'], config['token_file']):
        if any((parent / '.git').exists() for parent in (private.parent, *private.parents)):
            stand.fail('worker private files must be outside Git checkouts')
    if not isinstance(config['catalogs'], dict):
        stand.fail('catalogs: project mapping required')
    roots = [stand.SOURCE_ROOT]
    for project, catalog in config['catalogs'].items():
        if (not isinstance(project, str) or not project or len(project) > 32 or not project.isprintable()
                or '/' in project or '\\' in project):
            stand.fail('project: printable code up to 32 characters without slashes required')
        stand.obj(catalog, ('schema_version', 'repositories'), label='catalog')
        if not isinstance(catalog['repositories'], list):
            stand.fail('catalog.repositories: array required')
        for repository in catalog['repositories']:
            stand.obj(repository, ('id', 'path', 'components'), label='repository')
            private_path(repository.get('path'), 'repository.path', directory=True)
        repositories = stand.validate_catalog(catalog)
        roots.extend(repo['path'] for repo in repositories.values())
    for root in roots:
        if (stand.contains(root, config['work_root']) or stand.contains(config['work_root'], root)
                or stand.contains(root, config['token_file'])):
            stand.fail('worker private files must be outside source checkouts')
    config.setdefault('fetch', False)
    if type(config['fetch']) is not bool:
        stand.fail('fetch: boolean required')
    config.setdefault('ref_source', 'origin' if config['fetch'] else 'local')
    if config['ref_source'] not in ('local', 'origin') or (config['fetch'] and config['ref_source'] != 'origin'):
        stand.fail('ref_source: local or origin required; fetch requires origin')
    config['publish_ip'] = runtime.publish_address(config.get('publish_ip', '127.0.0.1'))
    for key, default in (('poll_seconds', 2), ('timeout', 900)):
        config.setdefault(key, default)
        number = config[key]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number <= 0:
            stand.fail(key + ': positive finite number required')
    return config


@contextmanager
def profile_lock(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = runtime.safe_path(root, root / '.worker.lock', exists=False)
    stream = path.open('a+b')
    try:
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stand.fail('worker: profile already has a running worker')
        yield
    finally:
        stream.close()  # OS releases the lifetime lock, including on process death.


class API:
    def __init__(self, config):
        self.origin = config['portal_url']
        path = config['token_file']
        if os.name != 'nt' and path.stat().st_mode & 0o077:
            stand.fail('worker: token file must be private to its owner')
        self.token = path.read_text(encoding='utf-8').strip()
        if not self.token or any(ord(c) < 33 or ord(c) > 126 for c in self.token):
            stand.fail('worker: invalid token file')

    def request(self, method, path, value=None, *, raw=None):
        data = raw if raw is not None else stand.canonical(value or {})
        request = urllib.request.Request(self.origin + '/api/agent/' + path, data=data, method=method,
            headers={'Authorization': 'Bearer ' + self.token,
                     'Content-Type': 'application/octet-stream' if raw is not None else 'application/json'})
        try:
            # Redirects must not forward credentials to another origin.
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs):
                    return None
            with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect).open(request, timeout=10) as response:
                if response.status == 204:
                    return None
                body = response.read(4 * 1024 * 1024 + 1)
                if len(body) > 4 * 1024 * 1024:
                    raise LeaseLost('worker: oversized portal response')
                return runtime.json_output(body.decode('utf-8'), 'portal response')
        except (OSError, ValueError, urllib.error.URLError, UnicodeError) as exc:
            if isinstance(exc, LeaseLost):
                raise
            raise LeaseLost('worker: portal request failed; lease no longer trusted') from None


def fetch_refs(path):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0', GIT_NO_REPLACE_OBJECTS='1')
    try:
        result = subprocess.run(['git', '-C', str(path), '-c', 'core.hooksPath=' + os.devnull,
            '-c', 'maintenance.auto=false', '-c', 'gc.auto=0', 'fetch', '--no-tags', '--prune', 'origin',
            '+refs/heads/*:refs/remotes/origin/*', '+refs/tags/*:refs/remotes/origin-tags/*'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, shell=False, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        stand.fail('worker: origin fetch failed or exceeded 20 seconds; no stale fallback')
    if result.returncode:
        stand.fail('worker: origin fetch failed; no stale fallback')


def fetched_ref(path, requested):
    if stand.SHA.fullmatch(requested):
        return stand.resolve_ref(path, requested)
    if requested.startswith('refs/heads/'):
        choices = ['refs/remotes/origin/' + requested[len('refs/heads/'):]]
    elif requested.startswith('refs/tags/'):
        choices = ['refs/remotes/origin-tags/' + requested[len('refs/tags/'):]]
    else:
        choices = ['refs/remotes/origin/' + requested, 'refs/remotes/origin-tags/' + requested]
    found = stand.git(path, 'for-each-ref', '--format=%(refname)%09%(symref)', *choices).splitlines()
    exact = [ref for ref, _, target in (entry.partition('\t') for entry in found) if ref in choices and not target]
    if len(exact) != 1:
        stand.fail('worker: origin ref missing or ambiguous; no stale fallback')
    sha = stand.git(path, 'rev-parse', '--verify', '--end-of-options', exact[0] + '^{commit}')
    if not stand.SHA.fullmatch(sha):
        stand.fail('worker: origin did not resolve a full SHA')
    return sha.lower()


def terminal_stages(records):
    return [{key: value for key, value in record.items() if key in ('name', 'status', 'started_at', 'finished_at')}
            for record in records if record.get('name') in stand.STAGES]


class Worker:
    def __init__(self, config, api=None, *, run_id=None):
        self.config = config
        self.api = api or API(config)
        self.root = config['work_root']
        if run_id is not None and (not isinstance(run_id, str) or not re.fullmatch(r'[0-9a-f]{32}', run_id)):
            stand.fail('worker: run id must be 32 lowercase hexadecimal characters')
        self.run_id = run_id or uuid.uuid4().hex
        self.done = threading.Event()
        self.current = None
        self.last_lease = 0
        self.cancelled = False
        self.log_offsets = {}
        self.active_workspace = None

    def redact(self, message):
        text = str(message)
        for private in [getattr(self.api, 'token', ''), str(self.root), str(stand.SOURCE_ROOT), str(self.config['token_file']),
                        *(str(entry['path']) for catalog in self.config['catalogs'].values() for entry in catalog['repositories'])]:
            if private:
                text = text.replace(private, '[redacted]')
                text = text.replace(private.replace('\\', '/'), '[redacted]')
        text = re.sub(r'https?://[^\s<>"\']+', '[url]', text)
        return text[-12000:]

    def stopped(self):
        path = runtime.safe_path(self.root, self.root / '.stop.json', exists=False)
        if path.exists():
            value = stand.read_json(path)
            return isinstance(value, dict) and value.get('run_id') == self.run_id
        return False

    def status_loop(self):
        while not self.done.is_set():
            runtime.atomic_json(self.root / 'status.json', {'pid': os.getpid(), 'run_id': self.run_id, 'heartbeat': runtime.now()})
            self.done.wait(2)

    def endpoint(self, suffix):
        return 'jobs/' + urllib.parse.quote(self.current, safe='') + '/stand/' + suffix

    def progress(self, record):
        response = self.api.request('POST', self.endpoint('progress'), {'stage': record['name'], 'status': record['status']})
        if response and response.get('cancel'):
            self.cancelled = True

    def checkpoint(self):
        if self.stopped():
            self.cancelled = True
        if self.current and time.monotonic() - self.last_lease >= 5:
            response = self.api.request('POST', 'jobs/' + self.current + '/log', {'text': ''})
            self.last_lease = time.monotonic()
            if not isinstance(response, dict) or type(response.get('cancel')) is not bool:
                raise LeaseLost('worker: invalid heartbeat response')
            self.cancelled |= response['cancel']
            if self.active_workspace is not None:
                self.logs(self.active_workspace)
        return self.cancelled

    def check(self):
        if self.checkpoint():
            raise runtime.Cancelled('worker: cancelled; containers/data retained')

    def logs(self, workspace):
        execution = workspace / 'execution.json'
        if not execution.exists():
            return
        state = stand.read_json(runtime.safe_path(workspace, execution))
        if not state.get('attempts'):
            return
        logs = runtime.safe_path(workspace, workspace / state['attempts'][-1]['logs'])
        for path in sorted(logs.glob('*.log')):
            if stand.is_link(path) or stand.is_link(path.parent):
                continue
            offset = self.log_offsets.get(str(path), 0)
            with path.open('r', encoding='utf-8', errors='replace') as stream:
                stream.seek(offset)
                text = stream.read(12000)
                self.log_offsets[str(path)] = stream.tell()
            if text:
                self.api.request('POST', 'jobs/' + self.current + '/log', {'text': self.redact(text)})

    def upload_artifacts(self, workspace, state, *, required=False):
        if not state or not state.get('attempts'):
            return
        output = runtime.safe_path(workspace, workspace / state['attempts'][-1]['output_dir'])
        for name in ('results.json', 'junit.xml', 'allure-report.html'):
            path = runtime.safe_path(workspace, output / name, exists=False)
            if path.is_file():
                self.api.request('PUT', self.endpoint('artifacts/' + name), raw=path.read_bytes())
            elif required and name == 'allure-report.html':
                stand.fail('worker: successful smoke must produce allure-report.html')

    def prepare(self, spec, workspace):
        stand.obj(spec, ('schema_version', 'stand', 'project', 'template', 'template_sha256', 'request', 'target_host',
                        'environments', 'ports', 'lock', 'parent_job_id'), ('snapshot', 'publish_ip'), label='stand job spec')
        stand.version(spec['schema_version'], 'stand job spec')
        if type(spec['lock']) is not bool or not isinstance(spec['environments'], list):
            stand.fail('worker: invalid stand metadata')
        stand.strings(spec['environments'], 'stand environments')
        if spec['target_host'] != self.config['host_id']:
            stand.fail('worker: job targets another host')
        publish_ip = self.config.get('publish_ip', '127.0.0.1')
        if spec.get('publish_ip', '127.0.0.1') != publish_ip:
            stand.fail('worker: job publication address differs from the host configuration')
        template = stand.validate_template(spec['template'])
        if stand.template_hash(template) != spec['template_sha256']:
            stand.fail('worker: frozen template hash mismatch')
        runtime.reserved_ports(template, spec['ports'])
        catalog = self.config['catalogs'].get(spec['project'])
        if catalog is None:
            stand.fail('worker: project has no local repository catalog')
        repositories = stand.validate_catalog(catalog)
        selected, parameters = stand.validate_request(template, spec['request'], repositories)
        identity = {'stand': spec['stand'], 'project': spec['project'], 'template_sha256': spec['template_sha256'],
                    'profile': spec['request']['profile'], 'ports': spec['ports']}
        if publish_ip != '127.0.0.1':
            identity['publish_ip'] = publish_ip
        metadata = workspace / '.worker-identity.json'
        retry = 'snapshot' in spec
        if retry:
            snapshot = spec['snapshot']
            stand.validate_snapshot(snapshot, repositories)
            expected = {c['id']: (c['repository'], c['requested_ref']) for c in selected}
            actual = {c['id']: (c['repository'], c['requested_ref']) for c in snapshot['components']}
            if (snapshot['template_sha256'] != spec['template_sha256'] or snapshot['profile'] != spec['request']['profile']
                    or snapshot['parameters'] != parameters or actual != expected):
                stand.fail('worker: retry snapshot differs from frozen request')
            if workspace.exists():
                if not metadata.exists() or stand.read_json(metadata) != identity or stand.read_json(workspace / 'snapshot.json') != snapshot:
                    stand.fail('worker: retry workspace identity/snapshot mismatch')
                runtime.load(workspace)
            else:
                stand.replay(snapshot, catalog, workspace)
                runtime.atomic_json(metadata, identity)
        else:
            if workspace.exists() and any(workspace.iterdir()):
                stand.fail('worker: prior workspace retained; explicit retry/recovery required')
            for rid in dict.fromkeys(c['repository'] for c in selected):
                self.check()
                if self.config['fetch']:
                    fetch_refs(repositories[rid]['path'])
            for component in selected:
                self.check()
                path = repositories[component['repository']]['path']
                ref_source = self.config.get('ref_source', 'origin' if self.config['fetch'] else 'local')
                resolver = fetched_ref if ref_source == 'origin' else stand.resolve_ref
                component['sha'] = resolver(path, component['requested_ref'])
            snapshot = {'schema_version': 1, 'template_sha256': spec['template_sha256'], 'template': template,
                        'components': selected, 'profile': spec['request']['profile'], 'parameters': parameters,
                        'secret_refs': template['secret_refs']}
            stand.publish(snapshot, repositories, workspace)
            runtime.atomic_json(metadata, identity)
        return snapshot, retry

    def job(self, job):
        stand.obj(job, ('id', 'kind', 'spec'), label='claimed job')
        if type(job['id']) is not int or job['id'] <= 0:
            stand.fail('worker: positive integer job id required')
        self.current = str(job['id'])
        self.cancelled = False
        self.last_lease = 0
        workspace, state, accepted, started = None, None, False, False
        stages = []
        try:
            if job['kind'] != 'stand-build':
                stand.fail('worker: unsupported job kind')
            spec = job['spec']
            stand_id = stand.identifier(spec.get('stand'), 'stand id')
            workspace = runtime.safe_path(self.root, self.root / 'stands' / stand_id, exists=False)
            workspace.parent.mkdir(exist_ok=True, mode=0o700)
            self.check()
            prepare = {'name': 'prepare', 'status': 'running', 'started_at': runtime.now()}
            stages.append(prepare)
            self.progress(prepare)
            snapshot, retry = self.prepare(spec, workspace)
            self.check()
            response = self.api.request('POST', self.endpoint('snapshot'), {'snapshot': snapshot})
            if not isinstance(response, dict) or not isinstance(response.get('build_id'), str):
                raise LeaseLost('worker: snapshot was not acknowledged')
            accepted = True
            prepare.update(status='success', finished_at=runtime.now())
            self.progress(prepare)
            self.check()
            started = True
            self.active_workspace = workspace
            def progress(record):
                self.logs(workspace)
                self.progress(record)
            state = runtime.run(workspace, retry=retry, timeout=self.config['timeout'], ports=spec['ports'],
                                progress=progress, cancel=self.checkpoint,
                                publish_ip=self.config.get('publish_ip', '127.0.0.1'))
            self.check()
            self.logs(workspace)
            self.upload_artifacts(workspace, state, required=True)
            self.check()
            self.api.request('POST', self.endpoint('finish'), {'status': 'done', 'stages': terminal_stages(state['attempts'][-1]['stages']),
                'images': state.get('images', []), 'output_bindings': state.get('output_bindings', {})})
            return 0
        except LeaseLost:
            # Uncertain lease ownership forbids terminal updates or automatic replay.
            raise
        except (Exception, KeyboardInterrupt) as exc:
            if started and workspace and (workspace / 'execution.json').exists():
                state = stand.read_json(workspace / 'execution.json')
                stages += state['attempts'][-1]['stages']
                self.logs(workspace)
                if accepted:
                    self.upload_artifacts(workspace, state)
            for stage in stages:
                if stage['status'] == 'running':
                    stage.update(status='failed', finished_at=runtime.now())
            self.api.request('POST', self.endpoint('finish'), {'status': 'cancelled' if isinstance(exc, runtime.Cancelled) else 'failed',
                'stages': terminal_stages(stages), 'images': (state or {}).get('images', []),
                'output_bindings': (state or {}).get('output_bindings', {}), 'message': self.redact(exc)[-2000:]})
            return 2
        finally:
            self.current = None
            self.active_workspace = None


    def run(self, once=False):
        with profile_lock(self.root):
            status = self.root / 'status.json'
            thread = threading.Thread(target=self.status_loop, daemon=True)
            thread.start()
            try:
                while not self.stopped():
                    try:
                        job = self.api.request('POST', 'claim', {'capabilities': ['stand-template-v1'], 'host_id': self.config['host_id']})
                    except LeaseLost:
                        if once:
                            raise
                        self.done.wait(min(self.config['poll_seconds'], 2))
                        continue
                    if job:
                        try:
                            result = self.job(job)
                        except LeaseLost:
                            if once:
                                raise
                            result = 2
                        if once:
                            return result
                    elif once:
                        return 0
                    self.done.wait(min(self.config['poll_seconds'], 2))
                return 0
            finally:
                self.done.set()
                thread.join(timeout=5)
                if status.exists() and stand.read_json(status).get('run_id') == self.run_id:
                    status.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--run-id', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        path = private_path(args.config, 'config')
        config = validate_config(stand.read_json(path))
        return Worker(config, run_id=args.run_id).run(args.once)
    except (stand.StandError, OSError, UnicodeError, json.JSONDecodeError):
        print('worker: operation failed; inspect private local state', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
