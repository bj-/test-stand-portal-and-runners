"""Lifecycle order, durable failure, isolation and retry with a fake Docker CLI."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import sys
import time
import unittest
from unittest import mock

from stands import runtime, stand


class RuntimeTests(unittest.TestCase):
    def test_environment_preserves_windows_docker_plugin_discovery_only(self):
        discovery = {'ProgramData': r'C:\ProgramData', 'ProgramFiles': r'C:\Program Files',
                     'ProgramFiles(x86)': r'C:\Program Files (x86)',
                     'CommonProgramFiles': r'C:\Program Files\Common Files'}
        with mock.patch.dict(os.environ, dict(discovery, PRIVATE_TOKEN='never-forward')):
            env = runtime.environment(self.root, self.snapshot)
        for name, value in discovery.items():
            self.assertEqual(env[name.upper()], value)
        self.assertNotIn('PRIVATE_TOKEN', env)

    def test_binding_names_cannot_override_windows_environment_by_case(self):
        for bindings in ({"Path": {}}, {"docker_host": {}}, {"compose_file": {}},
                         {"url": {}, "URL": {}}, {"baseUrl": {}, "base_url": {}}):
            with self.subTest(bindings=bindings):
                with self.assertRaises(stand.StandError):
                    runtime.check_binding_names({"output_bindings": bindings}, {"PATH": "system"})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="stand-runtime-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "workspace"
        self.root.mkdir()
        (self.root / stand.MARKER).write_text("stand-build-v1\n", encoding="utf-8")
        self.template = stand.read_json(Path(__file__).parent / "examples/http-smoke.template.json")
        self.template["profiles"]["laptop"]["services"].append("runner")
        self.template["lifecycle"]["migrate"] = [{"component": "api", "argv": ["docker", "compose", "run", "--rm", "--no-deps", "api", "python", "tasks.py", "migrate"]}]
        self.template["lifecycle"]["seed"] = [{"component": "api", "argv": ["docker", "compose", "run", "--rm", "--no-deps", "api", "python", "tasks.py", "seed"]}]
        self.template["lifecycle"]["healthcheck"] = [{"component": "api", "argv": ["docker", "compose", "up", "--wait", "--wait-timeout", "60", "api"]}]
        self.template["lifecycle"]["smoke"] = [{"component": "smoke", "argv": ["docker", "compose", "run", "--rm", "--no-deps", "runner"]}]
        for name in ("api", "smoke"):
            context = self.root / "contexts" / name
            context.mkdir(parents=True)
            (context / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
            (context / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        self.snapshot = {"schema_version": 1, "template": self.template, "template_sha256": "", "components": [
            {"id": "api", "repository": "service-src", "requested_ref": "test", "sha": "a" * 40},
            {"id": "smoke", "repository": "runner-src", "requested_ref": "feature/test", "sha": "b" * 40}],
            "profile": "laptop", "parameters": {"timeout_seconds": 30, "verbose": False}, "secret_refs": []}
        self.write_snapshot()
        self.commands = []
        self.events = []
        self.envs = []
        self.failure = None
        self.config_mutation = None
        patch = mock.patch.object(runtime.subprocess, "run", side_effect=self.docker)
        patch.start()
        self.addCleanup(patch.stop)
        env_patch = mock.patch.dict(os.environ, {"DOCKER_HOST": "unix:///var/run/docker.sock", "DOCKER_CONTEXT": "",
                                                "SECRET_VALUE": "never-forward-this", "COMPOSE_FILE": "escape.yaml", "BASE_URL": "http://wrong.invalid"})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def write_snapshot(self):
        self.snapshot["template_sha256"] = stand.template_hash(self.template)
        (self.root / "snapshot.json").write_text(json.dumps(self.snapshot), encoding="utf-8")

    def docker(self, argv, **kwargs):
        self.commands.append(argv)
        self.envs.append(dict(kwargs["env"]))
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["cwd"], self.root)
        self.assertGreater(kwargs["timeout"], 0)
        if argv[:3] == ["docker", "context", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, "unix:///var/run/docker.sock\n")
        if argv[:2] == ["docker", "info"]:
            return subprocess.CompletedProcess(argv, 0, "linux\n")
        if argv == ["docker", "compose", "version"]:
            return subprocess.CompletedProcess(argv, 0, "Docker Compose version v2\n")
        if argv[:3] == ["docker", "image", "inspect"]:
            self.events.append("images")
            return subprocess.CompletedProcess(argv, 0, json.dumps([{"Id": "sha256:" + str(i + 1) * 64,
                "RepoDigests": [] if i else ["example@sha256:" + "c" * 64], "RepoTags": [name]}
                for i, name in enumerate(argv[3:])]))
        project = argv[argv.index("--project-name") + 1]
        operation = next(arg for arg in argv[2:] if arg in {"config", "build", "up", "run", "port", "stop"})
        if operation == "config":
            self.assertIn("--no-env-resolution", argv)
            if any(Path(arg).name == 'runtime.effective.json' for arg in argv):
                return subprocess.CompletedProcess(argv, 0, (self.root / 'runtime.effective.json').read_text())
            config = {"name": project, "services": {
                "api": {"build": {"context": str(self.root / "contexts/api"), "dockerfile": "Dockerfile"},
                        "ports": [{"target": 8080, "published": "0", "host_ip": "127.0.0.1"}],
                        "volumes": [{"type": "volume", "source": "data", "target": "/data"}], "networks": {"default": None}},
                "runner": {"build": {"context": str(self.root / "contexts/smoke")},
                           "volumes": [{"type": "bind", "source": kwargs["env"]["OUTPUT_DIR"], "target": "/results"}]}},
                "volumes": {"data": {"name": project + "_data"}}, "networks": {"default": {"name": project + "_default"}}}
            if self.config_mutation:
                self.config_mutation(config)
            return subprocess.CompletedProcess(argv, 0, json.dumps(config))
        if operation == "port":
            self.events.append("port")
            return subprocess.CompletedProcess(argv, 0, "127.0.0.1:49123\n")
        if operation == "run":
            event = "migrate" if "migrate" in argv else "seed" if "seed" in argv else "smoke"
        elif operation == "up":
            event = "healthcheck" if "--wait" in argv else "deploy"
        else:
            event = operation
        self.events.append(event)
        if hasattr(kwargs["stdout"], "write"):
            kwargs["stdout"].write("fake Docker output\n")
        if self.failure == "timeout-" + event:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 19 if self.failure == event else 0, "")

    def state(self):
        return stand.read_json(self.root / "execution.json")

    def test_success_order_isolation_bindings_limits_images_and_no_host_secret(self):
        state = runtime.run(self.root)
        self.assertEqual(self.events, ["build", "deploy", "port", "migrate", "seed", "healthcheck", "smoke", "images"])
        self.assertEqual(state["status"], "success")
        self.assertEqual(state["output_bindings"], {"baseUrl": "http://127.0.0.1:49123/"})
        attempt = state["attempts"][0]
        self.assertEqual([s["status"] for s in attempt["stages"]], ["success"] * 6)
        self.assertEqual(len(state["images"]), 2)
        self.assertTrue(state["images"][0]["repo_digests"])
        self.assertEqual(state["sources"][0]["sha"], "a" * 40)
        self.assertTrue((self.root / attempt["output_dir"]).is_dir())
        self.assertEqual(stand.read_json(self.root / attempt["output_dir"] / "output-bindings.json"),
                         state["output_bindings"])
        self.assertIn("fake Docker output", (self.root / attempt["stages"][0]["log"]).read_text())
        for command, env in zip(self.commands, self.envs):
            self.assertNotIn("SECRET_VALUE", env)
            self.assertNotIn("COMPOSE_FILE", env)
            self.assertEqual(env["CONTEXT_SMOKE"], str(self.root / "contexts/smoke"))
            self.assertEqual(env["STAND_PARAM_TIMEOUT_SECONDS"], "30")
            self.assertEqual(env["STAND_PARAM_VERBOSE"], "false")
            if "--project-name" in command:
                self.assertEqual(command[command.index("--project-name") + 1], state["project_name"])
                self.assertEqual(command[command.index("--project-directory") + 1], str(self.root / "contexts/api"))
        smoke_command = next(c for c in self.commands if "run" in c and "runner" in c)
        self.assertNotIn("--env", smoke_command)
        self.assertNotIn("never-forward-this", (self.root / "execution.json").read_text())
        override = stand.read_json(self.root / "runtime.override.json")
        self.assertEqual(set(override["services"]), {"api", "runner"})
        self.assertEqual(override["services"]["runner"]["mem_limit"], "512m")
        self.assertFalse((self.root / ".execution.lock").exists())

    def test_each_stage_failure_stops_and_persists_without_cleanup(self):
        for stage in stand.STAGES:
            with self.subTest(stage=stage):
                self.failure = stage
                self.events.clear()
                with self.assertRaisesRegex(stand.StandError, "exit 19"):
                    runtime.run(self.root, retry=bool((self.root / "execution.json").exists()))
                state = self.state()
                self.assertEqual(state["status"], "failed")
                self.assertEqual(state["attempts"][-1]["stages"][-1]["name"], stage)
                self.assertEqual(state["attempts"][-1]["stages"][-1]["status"], "failed")
                self.assertNotIn("images", self.events)
                self.assertNotIn("stop", self.events)
                self.assertNotIn("down", self.events)
                self.assertTrue((self.root / state["attempts"][-1]["output_dir"]).is_dir())

    def test_timeout_is_failure_logs_it_and_does_not_continue(self):
        self.failure = "timeout-migrate"
        with self.assertRaisesRegex(stand.StandError, "timed out"):
            runtime.run(self.root, timeout=12)
        state = self.state()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(self.events, ["build", "deploy", "port", "migrate"])
        self.assertIn("timed out", (self.root / state["attempts"][0]["stages"][-1]["log"]).read_text())

    def test_retry_requires_flag_preserves_project_and_has_separate_attempt_outputs(self):
        first = runtime.run(self.root)
        calls = len(self.commands)
        with self.assertRaisesRegex(stand.StandError, "--retry"):
            runtime.run(self.root)
        self.assertEqual(len(self.commands), calls)
        second = runtime.run(self.root, retry=True)
        self.assertEqual(first["project_name"], second["project_name"])
        self.assertEqual(len(second["attempts"]), 2)
        self.assertNotEqual(second["attempts"][0]["id"], second["attempts"][1]["id"])
        self.assertNotEqual(second["attempts"][0]["output_dir"], second["attempts"][1]["output_dir"])
        self.assertEqual(self.events.count("migrate"), 2)

    def test_stop_same_project_keeps_data_and_records_result(self):
        first = runtime.run(self.root)
        output = self.root / first["attempts"][0]["output_dir"] / "keep.txt"
        output.write_text("retained")
        state = runtime.stop(self.root)
        self.assertEqual(state["project_name"], first["project_name"])
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(state["attempts"][0]["status"], "success")
        self.assertEqual(state["stops"][0]["status"], "success")
        self.assertEqual(output.read_text(), "retained")
        self.assertEqual(self.events[-1], "stop")
        self.assertFalse(any("down" in c or "--volumes" in c for c in self.commands))

    def test_failed_stop_retains_prior_status(self):
        runtime.run(self.root)
        self.failure = "stop"
        with self.assertRaises(stand.StandError):
            runtime.stop(self.root)
        state = self.state()
        self.assertEqual(state["status"], "success")
        self.assertEqual(state["stops"][-1]["status"], "failed")

    def test_remote_docker_rejected_before_any_daemon_command(self):
        for endpoint in ("ssh://remote", "tcp://127.0.0.1:2375", "npipe:////remote/pipe/docker_engine"):
            with self.subTest(endpoint=endpoint), mock.patch.dict(os.environ, {"DOCKER_HOST": endpoint, "DOCKER_CONTEXT": ""}):
                before = len(self.commands)
                with self.assertRaisesRegex(stand.StandError, "remote daemon"):
                    runtime.run(self.root, retry=bool((self.root / "execution.json").exists()))
                self.assertEqual(len(self.commands), before)

    def test_selected_context_takes_priority_over_docker_host(self):
        with mock.patch.dict(os.environ, {"DOCKER_HOST": "ssh://remote", "DOCKER_CONTEXT": "desktop-linux"}):
            self.assertEqual(runtime.run(self.root)["status"], "success")
        self.assertEqual(self.commands[0][:4], ["docker", "context", "inspect", "desktop-linux"])

    def test_lock_blocks_run_and_stop_without_deleting_lock(self):
        runtime.run(self.root)
        lock = self.root / ".execution.lock"
        lock.write_text("owner")
        calls = len(self.commands)
        for action in (runtime.run, runtime.stop):
            with self.subTest(action=action.__name__), self.assertRaisesRegex(stand.StandError, "locked"):
                action(self.root)
        self.assertEqual(len(self.commands), calls)
        self.assertEqual(lock.read_text(), "owner")

    def test_reject_host_commands_escape_flags_wrong_services_and_fake_healthcheck(self):
        cases = [
            ("smoke", ["python", "runner.py"]),
            ("build", ["docker", "--context", "other", "compose", "build"]),
            ("build", ["docker", "compose", "--file", "elsewhere", "build"]),
            ("build", ["docker", "compose", "build", "--file", "elsewhere"]),
            ("deploy", ["docker", "compose", "up", "-d", "--project-name", "other"]),
            ("deploy", ["docker", "compose", "up", "-d", "unselected"]),
            ("smoke", ["docker", "compose", "run", "--volume", "/:/host", "runner"]),
            ("healthcheck", ["docker", "compose", "ps", "--format", "json"]),
            ("healthcheck", ["docker", "compose", "up", "-d"]),
        ]
        for stage, argv in cases:
            with self.subTest(stage=stage, argv=argv), self.assertRaises(stand.StandError):
                runtime.normalize({"argv": argv}, stage, ["api", "runner"])
        # Tokens after run SERVICE are container argv and retain literal spelling.
        result = runtime.normalize({"argv": ["docker", "compose", "run", "runner", "echo", "$(touch x)", "--file"]}, "smoke", ["runner"])
        self.assertEqual(result, ["run", "--rm", "runner", "echo", "$(touch x)", "--file"])

    def test_invalid_compose_is_rejected_before_build(self):
        cases = [
            ("missing service", lambda c: c["services"].pop("runner")),
            ("external volume", lambda c: c["volumes"]["data"].update(external=True)),
            ("fixed volume", lambda c: c["volumes"]["data"].update(name="other_data")),
            ("external network", lambda c: c["networks"]["default"].update(external=True)),
            ("host network", lambda c: c["services"]["api"].update(network_mode="host")),
            ("container name", lambda c: c["services"]["api"].update(container_name="other_api")),
            ("privileged", lambda c: c["services"]["api"].update(privileged=True)),
            ("outside bind", lambda c: c["services"]["runner"]["volumes"][0].update(source=str(self.root.parent))),
            ("outside build", lambda c: c["services"]["api"]["build"].update(context=str(self.root.parent))),
            ("outside Dockerfile", lambda c: c["services"]["api"]["build"].update(dockerfile="../../../../Dockerfile")),
            ("all interfaces", lambda c: c["services"]["api"]["ports"][0].update(host_ip="0.0.0.0")),
            ("fixed port", lambda c: c["services"]["api"]["ports"][0].update(published="8080")),
            ("dependency", lambda c: c["services"]["api"].update(depends_on={"other": {}})),
            ("replicas", lambda c: c["services"]["api"].update(deploy={"replicas": 2})),
        ]
        for label, mutate in cases:
            with self.subTest(label=label):
                self.config_mutation = mutate
                self.events.clear()
                with self.assertRaises(stand.StandError):
                    runtime.run(self.root, retry=bool((self.root / "execution.json").exists()))
                self.assertEqual(self.events, [])
                self.assertEqual(self.state()["status"], "failed")

    def test_snapshot_tampering_secret_refs_and_environment_binding_collisions(self):
        self.template["version"] = "tampered"
        (self.root / "snapshot.json").write_text(json.dumps(self.snapshot))
        with self.assertRaisesRegex(stand.StandError, "hash mismatch"):
            runtime.run(self.root)
        self.template["secret_refs"] = self.snapshot["secret_refs"] = ["password"]
        self.write_snapshot()
        with self.assertRaisesRegex(stand.StandError, "secret_refs"):
            runtime.run(self.root)
        self.template["secret_refs"] = self.snapshot["secret_refs"] = []
        binding = self.template["output_bindings"].pop("baseUrl")
        self.template["output_bindings"]["COMPOSE_FILE"] = binding
        self.write_snapshot()
        with self.assertRaisesRegex(stand.StandError, "collides"):
            runtime.run(self.root)
        self.assertEqual(self.commands, [])

    def test_timeout_budget_is_shared_and_invalid_timeout_rejected(self):
        env = runtime.environment(self.root, self.snapshot)
        with mock.patch.object(runtime.time, "monotonic", side_effect=[100, 104, 111]):
            executor = runtime.Executor(self.root, env, 10)
            executor.call(["docker", "compose", "version"], self.root / "budget.log", capture=True)
            self.assertEqual(self.commands[-1], ["docker", "compose", "version"])
            with self.assertRaisesRegex(stand.StandError, "timeout exceeded"):
                executor.call(["docker", "info"], self.root / "budget.log")
        for timeout in (0, -1, True, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(stand.StandError):
                runtime.Executor(self.root, env, timeout)

    def test_cli_run_stop_and_nonzero_failure(self):
        with mock.patch("sys.stdout"):
            self.assertEqual(stand.main(["run", "--workspace", str(self.root)]), 0)
            self.assertEqual(stand.main(["stop", "--workspace", str(self.root)]), 0)
        with mock.patch("sys.stderr"):
            self.assertEqual(stand.main(["run", "--workspace", str(self.root)]), 2)

    def test_fixed_ports_replace_all_publishing_and_apply_resources_without_merge(self):
        config = {"services": {"api": {"ports": [
            {"target": 8080, "host_ip": "127.0.0.1", "published": "0"},
            {"target": 9090, "host_ip": "127.0.0.1", "published": "0"}]}, "runner": {}}}
        self.template["output_bindings"]["alias"] = copy.deepcopy(self.template["output_bindings"]["baseUrl"])
        ports = {"baseUrl": 24567, "alias": 24567}
        effective = runtime.effective_config(config, self.root, ["api", "runner"], "project", self.template,
                                             ports, {"cpus": 2, "memory_mb": 256})
        self.assertEqual(len(effective["services"]["api"]["ports"]), 1)
        self.assertEqual(effective["services"]["api"]["ports"][0]["published"], "24567")
        self.assertEqual(effective["services"]["runner"]["ports"], [])
        self.assertEqual(effective["services"]["api"]["mem_limit"], "256m")
        self.assertEqual(config["services"]["api"]["ports"][0]["published"], "0")
        # The untrusted source must remain dynamic even in fixed-port mode.
        config["services"]["api"]["ports"][0]["published"] = "24567"
        with self.assertRaisesRegex(stand.StandError, "dynamic"):
            runtime.effective_config(config, self.root, ["api", "runner"], "project", self.template,
                                     ports, {"cpus": 2, "memory_mb": 256})

    def test_reserved_port_aliases_groups_and_actual_port_mismatch(self):
        self.template["output_bindings"]["alias"] = copy.deepcopy(self.template["output_bindings"]["baseUrl"])
        for ports in ({"baseUrl": 24567}, {"baseUrl": True, "alias": True},
                      {"baseUrl": 24567, "alias": 24568}):
            with self.subTest(ports=ports), self.assertRaises(stand.StandError):
                runtime.reserved_ports(self.template, ports)
        self.template["output_bindings"]["alias"]["port"] = 9090
        with self.assertRaisesRegex(stand.StandError, "distinct"):
            runtime.reserved_ports(self.template, {"baseUrl": 24567, "alias": 24567})
        executor = runtime.Executor(self.root, runtime.environment(self.root, self.snapshot), 10)
        with self.assertRaisesRegex(stand.StandError, "differs"):
            runtime.binding_urls(executor, ["docker", "compose", "--project-name", "project"],
                                 self.template, self.root / 'ports.log', {"baseUrl": 24001, "alias": 24002})

    def test_opt_in_private_publication_preserves_dynamic_source_restriction(self):
        config = {"services": {"api": {"ports": [
            {"target": 8080, "host_ip": "127.0.0.1", "published": "0"}]}, "runner": {}}}
        args = (config, self.root, ["api", "runner"], "project", self.template, {"baseUrl": 24567},
                {"cpus": 2, "memory_mb": 256})
        address = runtime.publish_address("10.20.30.40")
        effective = runtime.effective_config(*args, publish_ip=address)
        self.assertEqual(effective["services"]["api"]["ports"][0]["host_ip"], address)
        config["services"]["api"]["ports"][0]["host_ip"] = address
        with self.assertRaisesRegex(stand.StandError, "dynamic"):
            runtime.effective_config(*args, publish_ip=address)
        for invalid in ("0.0.0.0", "8.8.8.8", "::1", "169.254.0.1"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(stand.StandError, "private or loopback"):
                runtime.publish_address(invalid)

    @unittest.skipUnless(os.name == "nt", "Windows Compose uses child plugins")
    def test_managed_cancel_terminates_the_windows_cli_and_plugins(self):
        process = mock.Mock(pid=4321)
        process.poll.side_effect = [None, 1]
        with mock.patch.object(runtime.subprocess, "run") as taskkill:
            runtime.terminate_command(process)
        args = taskkill.call_args.args[0]
        self.assertEqual(Path(args[0]).name, "taskkill.exe")
        self.assertEqual(args[1:], ["/PID", "4321", "/T", "/F"])
        process.terminate.assert_not_called()
        process.wait.assert_called_once_with(timeout=3)

    def test_fixed_run_retry_and_stop_share_persisted_effective_identity(self):
        ports = {'baseUrl': 49123}
        state = runtime.run(self.root, ports=ports)
        self.assertEqual(state['reserved_ports'], ports)
        lifecycle = [argv for argv in self.commands if 'build' in argv or 'up' in argv or 'run' in argv]
        self.assertTrue(all(str(self.root / 'runtime.effective.json') in argv for argv in lifecycle))
        self.assertTrue(all(str(self.root / 'runtime.override.json') not in argv for argv in lifecycle))
        with self.assertRaisesRegex(stand.StandError, 'persisted'):
            runtime.run(self.root, retry=True, ports={'baseUrl': 49124})
        self.assertEqual(runtime.stop(self.root)['status'], 'stopped')
        effective = stand.read_json(self.root / 'runtime.effective.json')
        effective['services']['api']['ports'].append({'target': 9090, 'published': '49200', 'host_ip': '127.0.0.1'})
        runtime.atomic_json(self.root / 'runtime.effective.json', effective)
        calls = len(self.commands)
        with self.assertRaisesRegex(stand.StandError, 'changed effective'):
            runtime.stop(self.root)
        self.assertNotIn('stop', [argv[-1] for argv in self.commands[calls:]])

    def test_cancel_running_owned_command_is_bounded_and_preserves_logs(self):
        started = time.monotonic()
        executor = runtime.Executor(self.root, runtime.environment(self.root, self.snapshot), 10,
                                    cancel=lambda: time.monotonic() - started > 0.3)
        with self.assertRaises(runtime.Cancelled):
            executor.call([sys.executable, '-c', 'import time; print("started", flush=True); time.sleep(30)'],
                          self.root / 'cancel.log')
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn('started', (self.root / 'cancel.log').read_text())

    def test_cancel_between_stages_persists_failed_attempt_and_progress(self):
        events = []
        def progress(record):
            events.append((record['name'], record['status']))
        # Test stage cancellation with the fake Docker adapter while the owned
        # Popen cancellation path is exercised separately above.
        original = runtime.Executor.call
        def fake_call(executor, *args, **kwargs):
            callback, executor.cancel = executor.cancel, None
            try:
                return original(executor, *args, **kwargs)
            finally:
                executor.cancel = callback
        with mock.patch.object(runtime.Executor, 'call', fake_call), self.assertRaises(runtime.Cancelled):
            runtime.run(self.root, progress=progress, cancel=lambda: ('deploy', 'success') in events)
        self.assertEqual(self.state()['status'], 'failed')
        self.assertEqual(self.events, ['build', 'deploy', 'port'])
        self.assertTrue((self.root / 'contexts/api').is_dir())


if __name__ == "__main__":
    unittest.main()
