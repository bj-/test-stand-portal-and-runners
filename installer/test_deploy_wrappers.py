"""Deployment orchestration with inert installers; never install packages or run Docker."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


def bash_executable():
    if os.name == "nt":
        candidate = Path("C:/Program Files/Git/bin/bash.exe")
        return str(candidate) if candidate.is_file() else None
    return shutil.which("bash")


class DeployWrapperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="deploy wrapper ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "tool source"
        self.installer = self.repo / "installer"
        self.installer.mkdir(parents=True)
        for name in ("deploy.sh", "deploy.ps1"):
            shutil.copyfile(REPO / name, self.repo / name)
        self.workspace = self.root / "workspace with spaces"
        self.source = self.root / "portal source"
        self.source.mkdir()
        self.log = self.root / "installer-args"
        self.demo_log = self.root / "demo.json"
        self.env = dict(os.environ, PYTHONUTF8="1", DEPLOY_TEST_LOG=str(self.log),
                        DEPLOY_TEST_DEMO_LOG=str(self.demo_log), DEPLOY_TEST_EXIT="0")
        (self.installer / "deploy_demo.py").write_text(
            "import json, os, sys\nfrom pathlib import Path\n"
            "Path(os.environ['DEPLOY_TEST_DEMO_LOG']).write_text(json.dumps(sys.argv[1:]))\n"
            "raise SystemExit(int(os.environ.get('DEPLOY_TEST_DEMO_EXIT', '0')))\n",
            encoding="utf-8")

    def bash_fixture(self):
        self.bash = bash_executable()
        if not self.bash:
            self.skipTest("Bash unavailable")
        (self.installer / "install.sh").write_text(
            '#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$DEPLOY_TEST_LOG"\n'
            'exit "$DEPLOY_TEST_EXIT"\n', encoding="utf-8", newline="\n")
        guards = self.root / "bin"
        guards.mkdir()
        python_stub = guards / "python3.12"
        python_stub.write_text('#!/bin/sh\nexec "$DEPLOY_TEST_PYTHON" "$@"\n', encoding="utf-8", newline="\n")
        python_stub.chmod(0o755)
        guard_path = guards.as_posix()
        if os.name == "nt":
            guard_path = "/" + guard_path[0].lower() + guard_path[2:]
        bash_env = self.root / "bash-env"
        bash_env.write_text('export PATH="$DEPLOY_TEST_BIN:$PATH"\n', encoding="utf-8")
        self.env.update(BASH_ENV=bash_env.as_posix(), DEPLOY_TEST_BIN=guard_path,
                        DEPLOY_TEST_PYTHON=Path(sys.executable).as_posix())

    def run_bash(self, *args):
        return subprocess.run([self.bash, (self.repo / "deploy.sh").as_posix(),
                               "--workspace", self.workspace.as_posix(),
                               "--portal-source", self.source.as_posix(), *args],
                              env=self.env, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=30)

    def powershell_fixture(self):
        self.powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
        if not self.powershell or os.name != "nt":
            self.skipTest("Windows PowerShell unavailable")
        (self.installer / "install.ps1").write_text(
            "param([string]$Workspace,[string]$PortalSource,[string]$Name,[string]$Port,"
            "[switch]$CreateAdmin,[switch]$Plan,[switch]$CheckOnly)\n"
            "$PSBoundParameters | ConvertTo-Json | Set-Content -LiteralPath $env:DEPLOY_TEST_LOG\n"
            "exit ([int]$env:DEPLOY_TEST_EXIT)\n", encoding="utf-8-sig")

    def run_powershell(self, *args):
        return subprocess.run([self.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                               str(self.repo / "deploy.ps1"), "-Workspace", str(self.workspace),
                               "-PortalSource", str(self.source), *args], env=self.env,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40)

    def assert_demo(self, retry):
        args = json.loads(self.demo_log.read_text(encoding="utf-8"))
        self.assertEqual(args[0], "--workspace")
        self.assertEqual(Path(args[1]), self.workspace / "example")
        self.assertEqual(args[2:], ["--retry"] if retry else [])

    def test_bash_syntax(self):
        bash = bash_executable()
        if not bash:
            self.skipTest("Bash unavailable")
        result = subprocess.run([bash, "-n", str(REPO / "deploy.sh")], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_powershell_syntax(self):
        shells = [shutil.which(name) for name in ("powershell.exe", "pwsh")]
        if not any(shells):
            self.skipTest("PowerShell unavailable")
        env = dict(self.env, DEPLOY_TEST_SCRIPT=str(REPO / "deploy.ps1"))
        command = ("$tokens=$null; $errors=$null; "
                   "[System.Management.Automation.Language.Parser]::ParseFile("
                   "$env:DEPLOY_TEST_SCRIPT,[ref]$tokens,[ref]$errors) | Out-Null; "
                   "if ($errors.Count) { $errors | Out-String | Write-Output; exit 1 }; exit 0")
        for shell in filter(None, shells):
            with self.subTest(shell=shell):
                result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                                        env=env, capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_bash_success_passes_separate_workspaces_and_options(self):
        self.bash_fixture()
        result = self.run_bash("--retry", "--create-admin", "--name", "demo2", "--port", "8765")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = self.log.read_text().splitlines()
        self.assertEqual(Path(args[1]), self.workspace / "portal")
        self.assertEqual(Path(args[3]), self.source)
        self.assertEqual(args[4:], ["--name", "demo2", "--port", "8765", "--create-admin"])
        self.assert_demo(retry=True)

    def test_bash_plan_check_and_failures_do_not_run_demo(self):
        self.bash_fixture()
        for mode, status in (("--dry-run", 0), ("--check", 0), ("--check", 1), (None, 20), (None, 7)):
            with self.subTest(mode=mode, status=status):
                self.env["DEPLOY_TEST_EXIT"] = str(status)
                result = self.run_bash(*([mode] if mode else []))
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                self.assertFalse(self.demo_log.exists())
                self.assertFalse(self.workspace.exists())
                if mode:
                    self.assertIn(mode, self.log.read_text().splitlines())
                if status == 20:
                    self.assertIn("deploy.sh", result.stderr)

    def test_bash_demo_failure_is_propagated(self):
        self.bash_fixture()
        self.env["DEPLOY_TEST_DEMO_EXIT"] = "9"
        result = self.run_bash()
        self.assertEqual(result.returncode, 9, result.stdout + result.stderr)
        self.assert_demo(retry=False)

    def test_powershell_success_passes_separate_workspaces_and_options(self):
        self.powershell_fixture()
        result = self.run_powershell("-Retry", "-CreateAdmin", "-Name", "demo2", "-Port", "8765")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = json.loads(self.log.read_text(encoding="utf-8-sig"))
        self.assertEqual(Path(args["Workspace"]), self.workspace / "portal")
        self.assertEqual(Path(args["PortalSource"]), self.source)
        self.assertEqual(args["Name"], "demo2")
        self.assertEqual(args["Port"], "8765")
        self.assertTrue(args["CreateAdmin"])
        self.assert_demo(retry=True)

    def test_powershell_plan_check_and_failures_do_not_run_demo(self):
        self.powershell_fixture()
        for mode, status in (("-Plan", 0), ("-CheckOnly", 0), ("-CheckOnly", 1), (None, 20), (None, 7)):
            with self.subTest(mode=mode, status=status):
                self.env["DEPLOY_TEST_EXIT"] = str(status)
                result = self.run_powershell(*([mode] if mode else []))
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                self.assertFalse(self.demo_log.exists())
                self.assertFalse(self.workspace.exists())
                args = json.loads(self.log.read_text(encoding="utf-8-sig"))
                if mode:
                    self.assertTrue(args[mode[1:]])
                if status == 20:
                    self.assertIn("deploy.ps1", result.stdout)

    def test_powershell_demo_failure_is_propagated(self):
        self.powershell_fixture()
        self.env["DEPLOY_TEST_DEMO_EXIT"] = "9"
        result = self.run_powershell()
        self.assertEqual(result.returncode, 9, result.stdout + result.stderr)
        self.assert_demo(retry=False)


if __name__ == "__main__":
    unittest.main()
