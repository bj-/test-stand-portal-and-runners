"""Host launcher lifecycle checks use OS locks and process doubles, never Docker."""
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import agent_local as agent
import portal_local as local


class AgentLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent launcher ")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.workspace = self.directory / "workspace with spaces"
        self.source = self.directory / "portal with spaces"
        self.source.mkdir()
        (self.source / "docker-compose.yaml").write_text("services: {}\n", encoding="utf-8")
        (self.source / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        self.data = local.init_workspace(self.workspace, None, 8765, self.source)
        self.root = self.workspace / "agent/work"

    def status(self, *, pid=12345, run_id="run-current", age=0):
        return {"pid": pid, "run_id": run_id,
                "heartbeat": (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()}

    def write_status(self, **kwargs):
        status = self.status(**kwargs)
        (self.root / "status.json").write_text(json.dumps(status), encoding="utf-8")
        return status

    def invoke(self, command, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = local.main(["--workspace", str(self.workspace), command, *args])
        return result, output.getvalue()

    def test_initial_configuration_and_readonly_compose_mounts(self):
        config = agent.configuration(self.workspace)
        self.assertEqual(config, {"schema_version": 1, "portal_url": "http://127.0.0.1:8765",
            "token_file": str(self.workspace / "agent/token"), "host_id": "local",
            "work_root": str(self.root), "catalogs": {}, "fetch": False, "poll_seconds": 2, "timeout": 900})
        self.assertGreaterEqual(len(agent.token(self.workspace)), 32)
        hosts = agent.read_json(self.workspace / "agent/hosts.json")
        self.assertEqual(hosts["hosts"][0]["port_range"], [20000, 20999])
        override = agent.read_json(self.workspace / "agent.compose.json")["services"]["portal"]
        self.assertEqual(override["environment"], {"PORTAL_STAND_HOSTS": "/config/local-agent/hosts.json"})
        self.assertEqual({volume["target"] for volume in override["volumes"]}, {"/config/local-agent", "/stand-mirrors"})
        self.assertTrue(all(volume["read_only"] and Path(volume["source"]).is_absolute() for volume in override["volumes"]))
        if os.name != "nt":
            self.assertEqual((self.workspace / "agent/token").stat().st_mode & 0o777, 0o600)
        args = local.parser().parse_args(["--workspace", str(self.workspace), "up"])
        command = local.compose_command("docker", self.workspace, self.data, args)
        files = [command[index + 1] for index, part in enumerate(command) if part == "--file"]
        self.assertEqual(files, [str(self.source / "docker-compose.yaml"), str(self.workspace / "agent.compose.json")])
        self.assertNotIn(agent.token(self.workspace), " ".join(command))

    def test_repeat_preserves_credentials_user_configuration_and_data_exactly(self):
        config_path = self.workspace / "agent/config.json"
        config = agent.configuration(self.workspace)
        config.update(catalogs={"project": {"repositories": []}}, poll_seconds=7)
        config_path.write_text(json.dumps(config), encoding="utf-8")
        (self.workspace / "agent/mirrors/user-content").write_bytes(b"keep mirror")
        (self.root / "job-data").write_bytes(b"keep job")
        (self.workspace / "projects.yaml").write_text("projects: {custom: true}\n", encoding="utf-8")
        paths = [self.workspace / name for name in (".env", "projects.yaml", "agent/token", "agent/config.json", "agent/hosts.json", "agent.compose.json", "agent/work/job-data", "agent/mirrors/user-content")]
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        self.assertEqual(self.invoke("init")[0], 0)
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths})

    def test_old_workspace_gets_missing_files_before_compose_up(self):
        # Model the installation format which predates host agents.
        shutil.rmtree(self.workspace / "agent")
        (self.workspace / "agent.compose.json").unlink()
        credentials = (self.workspace / ".env").read_bytes()
        def run(command, *_args, **_kwargs):
            self.assertIn(str(self.workspace / "agent.compose.json"), command)
            self.assertTrue((self.workspace / "agent/token").is_file())
            return 0
        with patch.object(local, "check_docker", return_value="docker"), patch.object(local, "run_compose", side_effect=run):
            self.assertEqual(self.invoke("up")[0], 0)
        self.assertEqual(credentials, (self.workspace / ".env").read_bytes())

    def test_provision_uses_configured_host_and_token_path_without_secret_argv(self):
        path = self.workspace / "agent/config.json"
        config = agent.configuration(self.workspace)
        config["host_id"] = "custom-host"
        path.write_text(json.dumps(config), encoding="utf-8")
        (self.workspace / "agent/hosts.json").write_text(json.dumps({"schema_version": 1, "hosts": [{"id": "custom-host"}]}), encoding="utf-8")
        secret = agent.token(self.workspace)
        with patch.object(local, "check_docker", return_value="docker"), patch.object(local, "run_compose", return_value=17) as run:
            result, output = self.invoke("agent-init")
        self.assertEqual(result, 17)
        command = run.call_args.args[0]
        self.assertEqual(command[-9:], ["portal.cli", "stand-agent", "provision", "--name", self.data["project_name"] + "-agent", "--host", "custom-host", "--token-file", "/config/local-agent/token"])
        self.assertIn("-T", command)
        self.assertTrue(run.call_args.kwargs["noninteractive"])
        self.assertIn(secret, run.call_args.kwargs["redactions"])
        self.assertNotIn(secret, " ".join(command) + output)

    def test_config_host_must_exist_without_rewriting_user_files(self):
        path = self.workspace / "agent/config.json"
        config = agent.configuration(self.workspace)
        config["host_id"] = "missing"
        path.write_text(json.dumps(config), encoding="utf-8")
        before = path.read_bytes()
        with patch.object(local, "check_docker") as docker:
            self.assertEqual(self.invoke("agent-init")[0], 1)
        docker.assert_not_called()
        self.assertEqual(before, path.read_bytes())

    def test_os_lock_and_recent_heartbeat_prevent_duplicate_process(self):
        self.write_status()
        with agent.file_lock(self.root / ".worker.lock") as acquired, patch.object(agent.subprocess, "Popen") as popen:
            self.assertTrue(acquired)
            self.assertTrue(agent.worker_locked(self.root))
            self.assertIn("работает", agent.start(self.workspace))
            self.assertIn("работает", agent.describe(self.workspace))
            popen.assert_not_called()
        self.assertFalse(agent.worker_locked(self.root))
        self.assertIn("остановлен", agent.describe(self.workspace))

    def test_locked_but_stale_worker_is_reported_without_spawning(self):
        self.write_status(age=60)
        with agent.file_lock(self.root / ".worker.lock"), patch.object(agent.subprocess, "Popen") as popen:
            self.assertIn("свежий heartbeat отсутствует", agent.describe(self.workspace))
            with self.assertRaisesRegex(agent.AgentError, "второй процесс не запущен"):
                agent.start(self.workspace)
            popen.assert_not_called()

    def test_start_background_process_uses_private_file_not_token_argument(self):
        process = Mock(pid=333)
        process.poll.return_value = None
        run_id = "a" * 32
        # Windows venv redirector PID and worker PID can legitimately differ.
        status = self.status(pid=444, run_id=run_id)
        with patch.object(agent.subprocess, "Popen", return_value=process) as popen, \
                patch.object(agent, "worker_locked", side_effect=[False, True, True]), \
                patch.object(agent, "read_status", side_effect=[status, status]), \
                patch.object(agent.secrets, "token_hex", return_value=run_id):
            self.assertIn("работает", agent.start(self.workspace))
        argv = popen.call_args.args[0]
        options = popen.call_args.kwargs
        self.assertEqual(argv, [sys.executable, "-m", "stands.worker", "--config", str(self.workspace / "agent/config.json"), "--run-id", run_id])
        self.assertNotIn(agent.token(self.workspace), " ".join(argv))
        self.assertEqual(options["cwd"], local.REPO)
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertEqual(options["stderr"], subprocess.STDOUT)
        self.assertNotIn("shell", options)
        if os.name == "nt":
            self.assertEqual(options["creationflags"], subprocess.CREATE_NO_WINDOW)
        else:
            self.assertTrue(options["start_new_session"])

    def test_start_failure_propagates_exit_and_stale_pid_cannot_confirm_start(self):
        self.write_status(pid=333)
        process = Mock(pid=444)
        process.poll.return_value = 19
        with patch.object(agent.subprocess, "Popen", return_value=process), patch.object(agent, "worker_locked", return_value=False):
            with self.assertRaisesRegex(agent.AgentError, "код 19"):
                agent.start(self.workspace)
        process.kill.assert_not_called()
        process.terminate.assert_not_called()

    def test_start_wait_is_bounded_and_never_kills_an_unconfirmed_process(self):
        # A reused PID and fresh heartbeat cannot confirm an unrelated run nonce.
        self.write_status(pid=333)
        process = Mock(pid=333)
        process.poll.return_value = None
        with patch.object(agent.subprocess, "Popen", return_value=process), \
                patch.object(agent, "worker_locked", side_effect=[False, True]), \
                patch.object(agent.secrets, "token_hex", return_value="b" * 32), \
                patch.object(agent.time, "monotonic", side_effect=[0, 0, 1]), patch.object(agent.time, "sleep"):
            with self.assertRaisesRegex(agent.AgentError, "не подтвердил запуск"):
                agent.start(self.workspace, timeout=0.5)
        process.kill.assert_not_called()
        process.terminate.assert_not_called()

    def test_stop_requests_specific_run_atomically_and_waits_for_lock_release(self):
        self.write_status()
        (self.root / ".stop.json").write_text('{"run_id":"old-run"}', encoding="utf-8")
        with patch.object(agent, "worker_locked", side_effect=[True, True, False]), \
                patch.object(agent.time, "sleep"), patch.object(agent.os, "replace", wraps=os.replace) as replace, \
                patch.object(agent.os, "kill") as kill:
            self.assertIn("остановлен", agent.stop(self.workspace))
        self.assertEqual(agent.read_json(self.root / ".stop.json"), {"run_id": "run-current"})
        self.assertEqual(len(replace.call_args_list), 1)
        self.assertEqual(replace.call_args.args[1], self.root / ".stop.json")
        self.assertEqual(list(self.root.glob("*.tmp")), [])
        kill.assert_not_called()

    def test_stale_status_without_lock_never_signals_a_reused_pid(self):
        self.write_status(pid=os.getpid(), age=100)
        with patch.object(agent.os, "kill") as kill, patch.object(agent.subprocess, "Popen") as popen:
            self.assertIn("остановлен", agent.stop(self.workspace))
        self.assertFalse((self.root / ".stop.json").exists())
        kill.assert_not_called()
        popen.assert_not_called()

    def test_stop_timeout_preserves_portal_and_data_without_force_kill(self):
        self.write_status()
        before = (self.workspace / ".env").read_bytes()
        with patch.object(agent, "worker_locked", return_value=True), \
                patch.object(agent.time, "monotonic", side_effect=[0, 31]), \
                patch.object(local, "check_docker") as docker, patch.object(local, "run_compose") as compose, \
                patch.object(agent.os, "kill") as kill:
            result, output = self.invoke("down")
        self.assertEqual(result, 1)
        self.assertIn("портал оставлен работать", output)
        docker.assert_not_called()
        compose.assert_not_called()
        kill.assert_not_called()
        self.assertEqual(before, (self.workspace / ".env").read_bytes())

    def test_missing_run_id_and_changed_run_id_do_not_stop_another_process(self):
        with patch.object(agent, "worker_locked", return_value=True):
            with self.assertRaisesRegex(agent.AgentError, "нет достоверного run_id"):
                agent.stop(self.workspace)
        self.assertFalse((self.root / ".stop.json").exists())
        with patch.object(agent, "worker_locked", return_value=True), \
                patch.object(agent, "read_status", side_effect=[self.status(), self.status(run_id="new-run")]):
            with self.assertRaisesRegex(agent.AgentError, "run_id агента изменился"):
                agent.stop(self.workspace)
        self.assertEqual(agent.read_json(self.root / ".stop.json"), {"run_id": "run-current"})

    def test_agent_commands_and_status_work_without_docker(self):
        with patch.object(local, "check_docker") as docker, patch.object(agent.subprocess, "Popen") as popen:
            result, output = self.invoke("agent-status")
            self.assertEqual(result, 0)
            self.assertIn("остановлен", output)
            self.assertEqual(self.invoke("agent-stop")[0], 0)
        docker.assert_not_called()
        popen.assert_not_called()
        with patch.object(local, "check_docker", return_value="docker"), patch.object(local, "run_compose", return_value=0):
            self.assertIn("Агент: остановлен", self.invoke("status")[1])

    def test_control_lock_prevents_parallel_launcher_commands(self):
        with agent.file_lock(self.workspace / "agent/.control.lock") as acquired, patch.object(agent.subprocess, "Popen") as popen:
            self.assertTrue(acquired)
            for action in (agent.start, agent.stop):
                with self.assertRaisesRegex(agent.AgentError, "другая команда"):
                    action(self.workspace)
            popen.assert_not_called()

    def test_invalid_timeout_never_launches_or_stops_anything(self):
        with patch.object(agent, "start") as start, patch.object(agent, "stop") as stop:
            for action in ("agent-start", "agent-stop"):
                for value in ("0", "-1", "nan", "inf", "901"):
                    self.assertEqual(self.invoke(action, "--timeout", value)[0], 1)
        start.assert_not_called()
        stop.assert_not_called()

    def test_heartbeat_requires_timezone_and_valid_identity(self):
        for value in ({"pid": 3, "run_id": "x", "heartbeat": "2026-11-18T12:00:00"},
                      {"pid": True, "run_id": "x", "heartbeat": "2026-11-18T12:00:00Z"},
                      {"pid": 3, "run_id": "", "heartbeat": "2026-11-18T12:00:00Z"}):
            (self.root / "status.json").write_text(json.dumps(value), encoding="utf-8")
            self.assertIsNone(agent.read_status(self.root))

    def test_composed_output_redacts_database_and_agent_secrets_across_chunks(self):
        secret = agent.token(self.workspace)
        class Output:
            chunks = iter([b"db-secret\n", secret[:20].encode(), secret[20:].encode() + b"\n", b""])
            def read1(self, _size): return next(self.chunks)
        class Process:
            stdout = Output()
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def wait(self): return 23
        output = io.StringIO()
        with patch.object(local.subprocess, "Popen", return_value=Process()), contextlib.redirect_stdout(output):
            result = local.run_compose(["docker"], {}, "db-secret", redactions=(secret,), noninteractive=True)
        self.assertEqual(result, 23)
        self.assertEqual(output.getvalue(), "[REDACTED]\n[REDACTED]\n")

    def test_redaction_handles_overlapping_secret_prefixes_and_prompt_streaming(self):
        secrets = [b"prefix-long-token", b"prefix"]
        output, pending = local.redact_prefix(b"prompt: prefix", secrets)
        self.assertEqual((output, pending), (b"prompt: ", b"prefix"))
        output, pending = local.redact_prefix(pending + b"-long-token!", secrets)
        self.assertEqual((output, pending), (b"[REDACTED]!", b""))
        self.assertEqual(local.redact_prefix(b"prefix", secrets, final=True), (b"[REDACTED]", b""))


if __name__ == "__main__":
    unittest.main()
