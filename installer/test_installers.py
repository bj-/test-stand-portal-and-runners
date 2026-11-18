"""Installer checks never execute a real package manager or Docker startup."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "installer"


def bash_executable():
    if os.name == "nt":
        candidate = Path("C:/Program Files/Git/bin/bash.exe")
        return str(candidate) if candidate.is_file() else None
    return shutil.which("bash")


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="installer test ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "workspace with spaces"
        self.source = self.root / "product source"
        self.source.mkdir()
        (self.source / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        (self.source / "docker-compose.yaml").write_text("services: {}\n", encoding="utf-8")
        self.guards = self.root / "guard commands"
        self.guards.mkdir()
        self.log = self.root / "calls.jsonl"
        self.mutations = self.root / "unexpected-mutations"
        self.env = dict(os.environ, PYTHONUTF8="1", PORTAL_TEST_LOG=str(self.log),
                        PORTAL_TEST_MUTATIONS=str(self.mutations), PORTAL_TEST_PYTHON=sys.executable,
                        PORTAL_TEST_WORKSPACE=str(self.workspace), PORTAL_TEST_SOURCE=str(self.source),
                        PORTAL_TEST_INSTALL_PS=str(INSTALLER / "install.ps1"))
        self.env["PATH"] = str(self.guards) + os.pathsep + os.environ.get("PATH", "")

    def shell_file(self, name, text):
        path = self.guards / name
        path.write_text("#!/bin/sh\n" + text + "\n", encoding="utf-8", newline="\n")
        path.chmod(0o755)

    def bash_fixture(self):
        # Git Bash prepends its own executable directories during startup.
        # BASH_ENV places our harmless command doubles first after that startup.
        bash_env = self.root / "bash-env"
        guards = self.guards.as_posix()
        if os.name == "nt":
            guards = "/" + guards[0].lower() + guards[2:]
        self.env["PORTAL_TEST_GUARDS"] = guards
        bash_env.write_text('export PATH="$PORTAL_TEST_GUARDS:$PATH"\n', encoding="utf-8")
        self.env["BASH_ENV"] = bash_env.as_posix()
        self.shell_file("uname", "printf 'Darwin\\n'")
        self.shell_file("git", "exit 0")
        for name in ("brew", "sudo", "apt-get", "systemctl", "usermod", "curl", "open", "winget", "wsl"):
            self.shell_file(name, 'printf "%s\\n" "$0" >> "$PORTAL_TEST_MUTATIONS"; exit 99')
        mock = self.root / "mock_python.py"
        mock.write_text('''import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ["PORTAL_TEST_LOG"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")
if args and Path(args[0]).name == "portal_local.py" and "init" in args:
    raise SystemExit(subprocess.run([sys.executable, *args]).returncode)
if "admin-init" in args:
    print("Existing users unchanged")
    raise SystemExit(int(os.environ.get("PORTAL_TEST_ADMIN_EXIT", "0")))
if "agent-init" in args:
    raise SystemExit(int(os.environ.get("PORTAL_TEST_AGENT_INIT_EXIT", "0")))
if "agent-start" in args:
    raise SystemExit(int(os.environ.get("PORTAL_TEST_AGENT_START_EXIT", "0")))
raise SystemExit(0)
''', encoding="utf-8")
        self.env["PORTAL_TEST_MOCK"] = mock.as_posix()
        self.env["PORTAL_TEST_PYTHON"] = Path(sys.executable).as_posix()
        self.shell_file("python3.12", 'exec "$PORTAL_TEST_PYTHON" "$PORTAL_TEST_MOCK" "$@"')

    def run_bash(self, *args):
        bash = bash_executable()
        if not bash:
            self.skipTest("Bash unavailable")
        return subprocess.run([bash, (INSTALLER / "install.sh").as_posix(),
                               "--workspace", self.workspace.as_posix(),
                               "--portal-source", self.source.as_posix(), *args],
                              env=self.env, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", timeout=45)

    def test_bash_syntax(self):
        bash = bash_executable()
        if not bash:
            self.skipTest("Bash unavailable")
        result = subprocess.run([bash, "-n", str(INSTALLER / "install.sh")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_probe_does_not_create_import_cache(self):
        fixture = self.root / "read only probe"
        (fixture / "installer").mkdir(parents=True)
        (fixture / "local").mkdir()
        probe = fixture / "installer" / "probe_docker.py"
        shutil.copyfile(INSTALLER / "probe_docker.py", probe)
        (fixture / "local" / "portal_local.py").write_text(
            "class LocalError(Exception): pass\ndef check_docker(env): return 'fixture'\n", encoding="utf-8")
        env = dict(self.env)
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        result = subprocess.run([sys.executable, str(probe)], env=env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((fixture / "local" / "__pycache__").exists())

    def test_bash_plan_and_check_do_not_write_or_call_installers(self):
        self.bash_fixture()
        for mode in ("--dry-run", "--plan", "--check"):
            result = self.run_bash(mode, "--create-admin")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(self.workspace.exists())
            self.assertFalse(self.mutations.exists())
            self.assertIn("admin-init", result.stdout)
            self.assertIn("agent-init", result.stdout)
            self.assertIn("agent-start", result.stdout)
        if self.log.exists():
            self.assertFalse(any(action in self.log.read_text(encoding="utf-8") for action in ("agent-init", "agent-start")))
        self.assertEqual((self.source / "docker-compose.yaml").read_text(), "services: {}\n")

    def test_bash_initializes_admin_after_health_on_every_install(self):
        self.bash_fixture()
        for options in ([], ["--create-admin"]):
            result = self.run_bash(*options, "--port", "8765")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(self.mutations.exists())
            self.assertNotIn("admin/admin", result.stdout)
            self.assertFalse((self.workspace / ".installer-admin-created").exists())
        entries = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        commands = [entry for entry in entries if entry and Path(entry[0]).name == "portal_local.py"]
        self.assertEqual(sum("init" in entry for entry in commands), 2)
        self.assertEqual(sum("doctor" in entry for entry in commands), 2)
        self.assertEqual(sum("up" in entry for entry in commands), 2)
        self.assertEqual(sum("admin-init" in entry for entry in commands), 2)
        self.assertEqual(sum("agent-init" in entry for entry in commands), 2)
        self.assertEqual(sum("agent-start" in entry for entry in commands), 2)
        self.assertFalse(any("user-add" in entry for entry in commands))
        for index, entry in enumerate(entries):
            if "admin-init" in entry:
                self.assertEqual(Path(entries[index - 1][0]).name, "wait_health.py")
                self.assertEqual(entry[-1], "admin-init")
                self.assertEqual(entries[index + 1][-1], "agent-init")
                self.assertEqual(entries[index + 2][-1], "agent-start")
        password_line = next(line for line in (self.workspace / ".env").read_text().splitlines()
                             if line.startswith("POSTGRES_PASSWORD="))
        self.assertNotIn(password_line.split("=", 1)[1].strip("'"), result.stdout + result.stderr)

    def test_bash_legacy_marker_cannot_bypass_admin_check_or_hide_failure(self):
        self.bash_fixture()
        self.assertEqual(self.run_bash().returncode, 0)
        marker = self.workspace / ".installer-admin-created"
        marker.write_text("legacy marker", encoding="utf-8")
        self.env["PORTAL_TEST_ADMIN_EXIT"] = "19"
        for options in ([], ["--create-admin"]):
            result = self.run_bash(*options)
            self.assertEqual(result.returncode, 19, result.stdout + result.stderr)
            self.assertEqual(marker.read_text(encoding="utf-8"), "legacy marker")
        entries = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sum("admin-init" in entry for entry in entries), 3)
        self.assertEqual(sum("agent-init" in entry for entry in entries), 1)
        self.assertEqual(sum("agent-start" in entry for entry in entries), 1)

    def test_bash_agent_provision_and_start_failures_are_fatal_without_successor(self):
        self.bash_fixture()
        for action, setting in (("agent-init", "PORTAL_TEST_AGENT_INIT_EXIT"), ("agent-start", "PORTAL_TEST_AGENT_START_EXIT")):
            with self.subTest(action=action):
                self.env[setting] = "23"
                result = self.run_bash()
                self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
                entries = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(entries[-1][-1], action)
                self.assertFalse(self.mutations.exists())
                self.env.pop(setting)

    def test_bash_invalid_port_stops_before_any_dependency_work(self):
        self.bash_fixture()
        result = self.run_bash("--dry-run", "--port", "65536")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.log.exists())
        self.assertFalse(self.workspace.exists())
        self.assertFalse(self.mutations.exists())

    def powershells(self):
        result = []
        for name in ("powershell.exe", "pwsh"):
            executable = shutil.which(name)
            if executable:
                result.append(executable)
        if not result:
            self.skipTest("PowerShell unavailable")
        return result

    def test_powershell_native_parsers_accept_utf8_script(self):
        script = '''$tokens=$null; $errors=$null
[System.Management.Automation.Language.Parser]::ParseFile($env:PORTAL_TEST_INSTALL_PS,[ref]$tokens,[ref]$errors) | Out-Null
if ($errors.Count) { $errors | Out-String | Write-Output; exit 1 }
exit 0
'''
        for executable in self.powershells():
            with self.subTest(executable=executable):
                result = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-Command", script],
                                        env=self.env, capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_powershell_plan_and_check_cannot_install_or_create_workspace(self):
        wrapper = r'''$ErrorActionPreference='Stop'
function Mark-Mutation { [IO.File]::AppendAllText($env:PORTAL_TEST_MUTATIONS, "unexpected\n"); throw 'Forbidden installer action in test' }
function winget.exe { Mark-Mutation }
function Start-Process { Mark-Mutation }
function Set-Content { Mark-Mutation }
$invokeArgs = @{ Workspace=$env:PORTAL_TEST_WORKSPACE; PortalSource=$env:PORTAL_TEST_SOURCE; CreateAdmin=$true }
$invokeArgs[$env:PORTAL_TEST_MODE] = $true
& $env:PORTAL_TEST_INSTALL_PS @invokeArgs
'''
        for executable in self.powershells():
            for mode in ("Plan", "CheckOnly"):
                with self.subTest(executable=executable, mode=mode):
                    env = dict(self.env, PORTAL_TEST_MODE=mode)
                    result = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-Command", wrapper],
                                            env=env, capture_output=True, timeout=45)
                    # CheckOnly can report the actual missing Docker on this host.
                    self.assertIn(result.returncode, (0, 1), result.stdout + result.stderr)
                    if mode == "Plan":
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertFalse(self.workspace.exists())
                    self.assertFalse(self.mutations.exists())

    def run_powershell_install(self, executable, *, create_admin=False):
        wrapper = r'''$ErrorActionPreference='Stop'
function Get-Command {
    param([string]$Name, $ErrorAction)
    if ($Name -eq 'python.exe') { return [pscustomobject]@{ Source='Invoke-TestPython' } }
    if ($Name -eq 'git.exe') { return [pscustomobject]@{ Source='unused-git' } }
    return $null
}
function Invoke-TestPython {
    $arguments = @($args)
    $line = ConvertTo-Json -InputObject $arguments -Compress
    [IO.File]::AppendAllText($env:PORTAL_TEST_LOG, $line + [Environment]::NewLine)
    $global:LASTEXITCODE = 0
    if ($arguments -contains 'init') {
        & $env:PORTAL_TEST_PYTHON @arguments
    } elseif ($arguments -contains 'admin-init') {
        Write-Output 'Existing users unchanged'
        $global:LASTEXITCODE = [int]$env:PORTAL_TEST_ADMIN_EXIT
    } elseif ($arguments -contains 'agent-init') {
        $global:LASTEXITCODE = [int]$env:PORTAL_TEST_AGENT_INIT_EXIT
    } elseif ($arguments -contains 'agent-start') {
        $global:LASTEXITCODE = [int]$env:PORTAL_TEST_AGENT_START_EXIT
    }
}
function Mark-Mutation { [IO.File]::AppendAllText($env:PORTAL_TEST_MUTATIONS, "unexpected"); throw 'Forbidden installer action in test' }
function winget.exe { Mark-Mutation }
function Start-Process { Mark-Mutation }
function Set-Content { Mark-Mutation }
$invokeArgs = @{ Workspace=$env:PORTAL_TEST_WORKSPACE; PortalSource=$env:PORTAL_TEST_SOURCE }
if ($env:PORTAL_TEST_CREATE_ADMIN -eq '1') { $invokeArgs['CreateAdmin'] = $true }
& $env:PORTAL_TEST_INSTALL_PS @invokeArgs
'''
        env = dict(self.env, PORTAL_TEST_CREATE_ADMIN="1" if create_admin else "0")
        env.setdefault("PORTAL_TEST_ADMIN_EXIT", "0")
        env.setdefault("PORTAL_TEST_AGENT_INIT_EXIT", "0")
        env.setdefault("PORTAL_TEST_AGENT_START_EXIT", "0")
        return subprocess.run([executable, "-NoProfile", "-NonInteractive", "-Command", wrapper],
                              env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=45)

    def test_powershell_admin_init_is_automatic_repeated_and_failure_is_fatal(self):
        for executable in self.powershells():
            with self.subTest(executable=executable):
                # Each shell uses the same workspace, including an old installer marker.
                for create_admin in (False, True):
                    result = self.run_powershell_install(executable, create_admin=create_admin)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertNotIn(b"admin/admin", result.stdout)
                    self.assertFalse(self.mutations.exists())
                marker = self.workspace / ".installer-admin-created"
                marker.write_text("legacy marker", encoding="utf-8")
                before = marker.read_bytes()
                self.env["PORTAL_TEST_ADMIN_EXIT"] = "19"
                result = self.run_powershell_install(executable)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(marker.read_bytes(), before)
                self.env.pop("PORTAL_TEST_ADMIN_EXIT")
        entries = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        admin_calls = [index for index, entry in enumerate(entries) if "admin-init" in entry]
        self.assertEqual(len(admin_calls), 3 * len(self.powershells()))
        self.assertFalse(any("user-add" in entry for entry in entries))
        for index in admin_calls:
            self.assertEqual(Path(entries[index - 1][0]).name, "wait_health.py")
            self.assertEqual(entries[index][-1], "admin-init")
            if index + 1 < len(entries) and "agent-init" in entries[index + 1]:
                self.assertEqual(entries[index + 2][-1], "agent-start")
        self.assertEqual(sum("agent-init" in entry for entry in entries), 2 * len(self.powershells()))
        self.assertEqual(sum("agent-start" in entry for entry in entries), 2 * len(self.powershells()))

    def test_powershell_agent_provision_and_start_failures_are_fatal(self):
        for executable in self.powershells():
            for action, setting in (("agent-init", "PORTAL_TEST_AGENT_INIT_EXIT"), ("agent-start", "PORTAL_TEST_AGENT_START_EXIT")):
                with self.subTest(executable=executable, action=action):
                    self.env[setting] = "23"
                    result = self.run_powershell_install(executable)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    entries = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
                    self.assertEqual(entries[-1][-1], action)
                    self.assertFalse(self.mutations.exists())
                    self.env.pop(setting)


if __name__ == "__main__":
    unittest.main()
