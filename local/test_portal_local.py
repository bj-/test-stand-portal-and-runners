"""Проверки границ локальной установки; Docker для запуска не требуется."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import portal_local as local


class LocalInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="portal launcher ")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve() / "workspace with spaces"
        self.source = Path(self.temp.name).resolve() / "portal source with spaces"
        self.source.mkdir()
        (self.source / "docker-compose.yaml").write_text("services: {}\n", encoding="utf-8")
        (self.source / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = local.main(["--workspace", str(self.workspace), "--portal-source", str(self.source), *args])
        return code, stdout.getvalue() + stderr.getvalue()

    def init(self, name=None, port=None):
        return local.init_workspace(self.workspace, name, port, self.source)

    def test_init_is_empty_private_and_idempotent(self):
        code, output = self.invoke("init", "--name", "demo", "--port", "8765")
        self.assertEqual(code, 0)
        password = local.env_password(self.workspace)
        self.assertEqual(len(password), 64)
        self.assertNotIn(password, output)
        self.assertNotIn("POSTGRES_PASSWORD", output)
        self.assertEqual((self.workspace / "projects.yaml").read_text(encoding="utf-8").splitlines()[-1], "projects: {}")
        for relative in ("data/postgres", "data/artifacts", "results"):
            self.assertTrue((self.workspace / relative).is_dir())
        # Preserve configuration changes as well as credentials on repeated init.
        with (self.workspace / ".env").open("a", encoding="utf-8") as stream:
            stream.write("BASE_REGISTRY=example.invalid/library\n")
        snapshot = {path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in self.workspace.iterdir() if path.is_file()}
        self.assertEqual(self.invoke("init")[0], 0)
        self.assertEqual(snapshot, {path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                                   for path in self.workspace.iterdir() if path.is_file()})
        self.assertEqual(self.invoke("init", "--port", "9999")[0], 1)
        self.assertEqual(password, local.env_password(self.workspace))
        if os.name != "nt":
            self.assertEqual((self.workspace / ".env").stat().st_mode & 0o777, 0o600)

    def test_foreign_workspace_and_missing_marker_are_not_managed(self):
        self.workspace.mkdir()
        unrelated = self.workspace / "existing.txt"
        unrelated.write_text("keep me", encoding="utf-8")
        for command in ("init", "down", "up", "doctor"):
            code, output = self.invoke(command)
            self.assertEqual(code, 1)
            self.assertNotIn("Traceback", output)
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep me")
        self.assertEqual(list(self.workspace.iterdir()), [unrelated])

    def test_repo_workspace_is_rejected_before_write(self):
        with self.assertRaises(local.LocalError):
            local.workspace_path(str(local.REPO / "local-workspace"))
        with self.assertRaises(local.LocalError):
            local.workspace_path(str(local.REPO))
        with self.assertRaises(local.LocalError):
            local.workspace_path(str(self.source / "workspace"), self.source)

    def test_source_is_required_validated_and_pinned(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            code = local.main(["--workspace", str(self.workspace), "init"])
        self.assertEqual(code, 1)
        self.assertFalse(self.workspace.exists())
        self.assertIn("--portal-source", output.getvalue())
        with self.assertRaises(local.LocalError):
            local.portal_source_path(str(self.source / "missing"))
        data = self.init()
        self.assertEqual(data["portal_source"], str(self.source))
        other = Path(self.temp.name).resolve() / "other source"
        other.mkdir()
        for name in ("docker-compose.yaml", "Dockerfile"):
            (other / name).write_text("", encoding="utf-8")
        with contextlib.redirect_stderr(output), patch.object(local, "check_docker") as docker:
            code = local.main(["--workspace", str(self.workspace), "--portal-source", str(other), "down"])
        self.assertEqual(code, 1)
        docker.assert_not_called()
        # Subsequent init can use the pinned source without repeating the flag.
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(local.main(["--workspace", str(self.workspace), "init"]), 0)

    def test_marker_identity_prevents_other_installation_control(self):
        data = self.init()
        other_workspace = Path(self.temp.name) / "another workspace"
        self.assertNotEqual(data["project_name"], local.compose_name(other_workspace, data["name"]))
        marker = self.workspace / local.MARKER
        data["project_name"] = "another-installation"
        marker.write_text(json.dumps(data), encoding="utf-8")
        with patch.object(local, "check_docker") as docker:
            code, _ = self.invoke("down")
        self.assertEqual(code, 1)
        docker.assert_not_called()

    def test_paths_with_spaces_and_user_arguments_remain_single_arguments(self):
        data = self.init(port=8765)
        args = local.parser().parse_args(["--workspace", str(self.workspace), "user-add", "alice",
                                         "--display-name", "Alice $HOME; other", "--role", "admin"])
        command = local.compose_command("C:/Program Files/Docker/docker.exe", self.workspace, data, args)
        self.assertIn(str(self.workspace / ".env"), command)
        self.assertEqual(command[command.index("--file") + 1], str(self.source / "docker-compose.yaml"))
        self.assertEqual(command[command.index("--project-directory") + 1], str(self.source))
        self.assertEqual(command[command.index("--project-name") + 1], data["project_name"])
        self.assertEqual(command[-7:], ["portal.cli", "user", "add", "alice", "--role", "admin", "--name=Alice $HOME; other"])
        self.assertNotIn("-T", command)  # user password prompt retains stdin/TTY
        for action, expected in (("up", ["up", "-d", "--build"]), ("down", ["down"]),
                                 ("status", ["ps", "--all"]), ("logs", ["logs", "--tail", "100"])):
            args = local.parser().parse_args(["--workspace", str(self.workspace), action])
            command = local.compose_command("docker", self.workspace, data, args)
            self.assertEqual(command[-len(expected):], expected)
            self.assertNotIn("-v", command)
        args = local.parser().parse_args(["--workspace", str(self.workspace), "sync", "--project", "test suite"])
        command = local.compose_command("docker", self.workspace, data, args)
        self.assertEqual(command[-2:], ["sync", "--project=test suite"])
        self.assertIn("-T", command)

    def test_environment_cannot_redirect_data_or_bind(self):
        data = self.init(port=8765)
        with (self.workspace / ".env").open("a", encoding="utf-8") as stream:
            stream.write("PORTAL_BIND=0.0.0.0:9999\nPROJECTS_FILE=/foreign/projects.yaml\n")
        with patch.dict(os.environ, {"PORTAL_BIND": "0.0.0.0:1234", "POSTGRES_PASSWORD": "foreign",
                                     "PORTAL_DATA": "/foreign", "COMPOSE_PROJECT_NAME": "foreign"}):
            env = local.docker_environment(self.workspace, data)
        self.assertEqual(env["PORTAL_BIND"], "127.0.0.1:8765")
        self.assertEqual(env["PORTAL_DATA"], (self.workspace / "data").as_posix())
        self.assertEqual(env["PROJECTS_FILE"], (self.workspace / "projects.yaml").as_posix())
        self.assertEqual(env["POSTGRES_PASSWORD"], local.env_password(self.workspace))
        self.assertNotIn("COMPOSE_PROJECT_NAME", env)

    def test_admin_init_is_noninteractive_and_propagates_failure(self):
        self.init()
        marker = self.workspace / ".installer-admin-created"
        marker.write_text("legacy marker", encoding="utf-8")
        for status in (0, 17):
            with self.subTest(status=status), patch.object(local, "check_docker", return_value="docker"), \
                    patch.object(local, "run_compose", return_value=status) as run, \
                    patch.object(local, "run_interactive") as interactive:
                self.assertEqual(self.invoke("admin-init")[0], status)
                self.assertEqual(run.call_args.args[0][-8:],
                                 ["exec", "-T", "portal", "python", "-m", "portal.cli", "user", "ensure-default-admin"])
                self.assertEqual(run.call_args.kwargs, {"noninteractive": True,
                    "redactions": (local.agent_local.token(self.workspace),)})
                interactive.assert_not_called()
        self.assertEqual(marker.read_text(encoding="utf-8"), "legacy marker")

    def test_name_and_port_validation(self):
        for name in ("", "../other", "-oops", "UPPER", "a" * 41, "name with spaces"):
            with self.subTest(name=name), self.assertRaises(Exception):
                local.valid_name(name)
        for port in ("0", "80", "65536", "abc", "1.5"):
            with self.subTest(port=port), self.assertRaises(Exception):
                local.valid_port(port)
        self.assertEqual(local.valid_name("demo-1_ok"), "demo-1_ok")
        self.assertEqual(local.valid_port("8080"), 8080)

    def test_docker_failures_have_clear_errors_without_secret_or_traceback(self):
        self.init()
        password = local.env_password(self.workspace)
        failures = [(patch.object(local.shutil, "which", return_value=None), None, "Docker не найден"),
                    (patch.object(local.shutil, "which", return_value="docker.exe"),
                     [subprocess.CompletedProcess([], 1, "", password)], "Compose v2"),
                    (patch.object(local.shutil, "which", return_value="docker.exe"),
                     [subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0, "unix:///var/run/docker.sock"),
                      subprocess.CompletedProcess([], 1, password, "")], "daemon")]
        for docker_patch, responses, expected in failures:
            with docker_patch, patch.object(local.subprocess, "run", side_effect=responses or []):
                code, output = self.invoke("doctor")
            self.assertEqual(code, 1)
            self.assertIn(expected, output)
            self.assertNotIn(password, output)
            self.assertNotIn("Traceback", output)
        with patch.object(local.shutil, "which", return_value="docker.exe"), \
                patch.object(local.subprocess, "run", side_effect=OSError("secret")):
            self.assertEqual(self.invoke("doctor")[0], 1)

    def test_remote_daemon_is_rejected_before_contact_and_context_wins(self):
        def probe(command, **_kwargs):
            output = "unix:///var/run/docker.sock" if "context" in command else "linux"
            return subprocess.CompletedProcess(command, 0, output, "")
        with patch.object(local.shutil, "which", return_value="docker"), \
                patch.object(local.subprocess, "run", side_effect=probe) as run:
            for endpoint in ("ssh://host", "tcp://host:2376", "npipe:////remote/pipe/docker_engine"):
                with self.subTest(endpoint=endpoint), self.assertRaises(local.LocalError):
                    local.check_docker({"DOCKER_HOST": endpoint})
            self.assertFalse(any("info" in call.args[0] for call in run.call_args_list))
            self.assertEqual(local.check_docker({"DOCKER_HOST": "ssh://host", "DOCKER_CONTEXT": "desktop-linux"}), "docker")
            context_calls = [call.args[0] for call in run.call_args_list if "context" in call.args[0]]
            self.assertIn("desktop-linux", context_calls[-1])

    def test_local_windows_pipe_and_linux_mode(self):
        with patch.object(local.shutil, "which", return_value="docker"), \
                patch.object(local.subprocess, "run", side_effect=[
                    subprocess.CompletedProcess([], 0, "version"),
                    subprocess.CompletedProcess([], 0, "npipe:////./pipe/dockerDesktopLinuxEngine"),
                    subprocess.CompletedProcess([], 0, "linux"),
                ]):
            self.assertEqual(local.check_docker({}), "docker")
        with patch.object(local.shutil, "which", return_value="docker"), \
                patch.object(local.subprocess, "run", side_effect=[
                    subprocess.CompletedProcess([], 0, "version"),
                    subprocess.CompletedProcess([], 0, "windows"),
                ]):
            with self.assertRaisesRegex(local.LocalError, "Linux containers"):
                local.check_docker({"DOCKER_HOST": "npipe:////./pipe/docker_engine"})

    def test_process_output_redacts_split_password_and_preserves_stdin(self):
        password = "test-secret-that-spans-chunks"
        class Output:
            def __init__(self):
                russian = "Готово".encode()
                self.chunks = iter([b"prompt: ", password[:8].encode(), password[8:].encode() + b"\n",
                                    russian[:1], russian[1:], b""])
            def read1(self, _size):
                return next(self.chunks)
        class Process:
            stdout = Output()
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def wait(self): return 0
        output = io.StringIO()
        with patch.object(local.subprocess, "Popen", return_value=Process()) as popen, contextlib.redirect_stdout(output):
            code = local.run_compose(["docker", "compose"], {}, password)
        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue(), "prompt: [REDACTED]\nГотово")
        self.assertIsNone(popen.call_args.kwargs["stdin"])
        self.assertNotIn("shell", popen.call_args.kwargs)

    def test_noninteractive_process_closes_stdin_and_redacts_failure_output(self):
        class Output:
            def __init__(self):
                self.chunks = iter([b"failure: database-secret\n", b""])
            def read1(self, _size): return next(self.chunks)
        class Process:
            stdout = Output()
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def wait(self): return 23
        output = io.StringIO()
        with patch.object(local.subprocess, "Popen", return_value=Process()) as popen, contextlib.redirect_stdout(output):
            self.assertEqual(local.run_compose(["docker", "compose"], {}, "database-secret", noninteractive=True), 23)
        self.assertEqual(output.getvalue(), "failure: [REDACTED]\n")
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_redirected_cp1251_streams_forward_unicode_compose_output(self):
        self.init()
        script = """
import sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import portal_local as local
status = int(sys.argv[3])
child = "import sys; sys.stdout.buffer.write(bytes.fromhex('e2948120f09f9a802070726f67726573730a')); sys.exit(" + str(status) + ")"
with patch.object(local, "check_docker", return_value="docker"), patch.object(
        local, "compose_command", return_value=[sys.executable, "-c", child]):
    result = local.main(["--workspace", sys.argv[2], "up"])
print(chr(0x2501), file=sys.stderr)
raise SystemExit(result)
"""
        for status in (0, 17):
            with self.subTest(status=status):
                result = subprocess.run([sys.executable, "-c", script, str(local.REPO / "local"),
                                         str(self.workspace), str(status)],
                                        env=dict(os.environ, PYTHONIOENCODING="cp1251"),
                                        capture_output=True, timeout=30)
                self.assertEqual(result.returncode, status, result.stderr.decode("utf-8", errors="replace"))
                self.assertEqual(result.stdout.decode("utf-8").splitlines(), ["\u2501 \U0001f680 progress"])
                self.assertEqual(result.stderr.decode("utf-8").splitlines(), ["\u2501"])

    def test_user_add_preserves_terminal_and_rejects_a_pipe(self):
        with patch.object(local.sys, "stdin") as stdin, patch.object(local.sys, "stdout") as stdout, \
                patch.object(local.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            stdin.isatty.return_value = stdout.isatty.return_value = True
            self.assertEqual(local.run_interactive(["docker", "compose", "exec"], {}), 0)
            self.assertEqual(run.call_args.kwargs, {"env": {}})
            stdout.isatty.return_value = False
            with self.assertRaises(local.LocalError):
                local.run_interactive(["docker"], {})
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
