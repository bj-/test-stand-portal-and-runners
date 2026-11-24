"""Настройка раннера на искусственных данных; Docker и Git не запускаются."""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

with patch.dict(os.environ, {"PORTAL_URL": "https://portal.invalid", "AGENT_TOKEN": "fixture-agent"}):
    spec = importlib.util.spec_from_file_location("fixture_portal_agent", Path(__file__).with_name("agent.py"))
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)


class LibraryAPIConfigurationTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"AUTOTEST_LIBRARY_API_URL": "https://library.invalid",
                                     "AUTOTEST_LIBRARY_API_KEY": "fixture-library-key"}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_prepare_never_clones_library_in_api_mode(self):
        job = {"project": "FIXTURE", "ref": "main", "tests_url": "fixture-tests", "results_url": "fixture-results",
               "requested_by": "fixture"}
        with patch.object(agent, "clone_or_update") as clone, patch.object(agent, "git"):
            agent.prepare(Mock(), job)
        self.assertEqual([call.args[1] for call in clone.call_args_list], ["fixture-tests", "fixture-results"])

    def test_missing_key_does_not_fall_back_to_configured_repository(self):
        os.environ.pop("AUTOTEST_LIBRARY_API_KEY")
        os.environ["AGENT_LIBRARY_URL"] = "fixture-repository"
        with self.assertRaisesRegex(RuntimeError, "ключ"):
            agent.library_configuration()

    def test_missing_api_fails_before_git_even_with_legacy_repository(self):
        os.environ.clear()
        os.environ["AGENT_LIBRARY_URL"] = "fixture-repository"
        with patch.object(agent, "clone_or_update") as clone, patch.object(agent, "git") as git:
            with self.assertRaisesRegex(RuntimeError, "AUTOTEST_LIBRARY_API_URL"):
                agent.prepare(Mock(), {"project": "FIXTURE"})
        clone.assert_not_called()
        git.assert_not_called()

    def test_missing_key_fails_before_git(self):
        os.environ.pop("AUTOTEST_LIBRARY_API_KEY")
        with patch.object(agent, "clone_or_update") as clone, patch.object(agent, "git") as git:
            with self.assertRaisesRegex(RuntimeError, "ключ"):
                agent.prepare(Mock(), {"project": "FIXTURE"})
        clone.assert_not_called()
        git.assert_not_called()

    def test_configuration_failure_finishes_claimed_job(self):
        os.environ.clear()
        with patch.object(agent, "CAPABILITIES", ["newman"]), patch.object(agent, "AFTER_JOB", None), \
                patch.object(agent, "api", return_value={"cancel": False}) as api, \
                patch.object(agent, "clone_or_update") as clone:
            agent.execute({"id": 7, "kind": "run", "spec": {"project": "FIXTURE"}})
        clone.assert_not_called()
        finish = api.call_args
        self.assertEqual(finish.args[1], "/api/agent/jobs/7/finish")
        self.assertEqual(finish.args[2]["status"], "failed")
        self.assertIn("AUTOTEST_LIBRARY_API_URL", finish.args[2]["message"])

    def test_pytest_prepare_and_execute_do_not_require_api(self):
        os.environ.clear()
        job = {"project": "FIXTURE", "runner": "pytest", "ref": "main", "tests_url": "fixture-tests",
               "results_url": "fixture-results", "requested_by": "fixture"}
        with patch.object(agent, "clone_or_update") as clone, patch.object(agent, "git"):
            tests, results = agent.prepare(Mock(), job)
        self.assertEqual([call.args[1] for call in clone.call_args_list], ["fixture-tests", "fixture-results"])
        with patch.object(agent, "prepare", return_value=(tests, results)), \
                patch.object(agent, "CAPABILITIES", ["pytest"]), patch.object(agent, "AFTER_JOB", None), \
                patch.object(agent, "api", return_value={"cancel": False}) as api, \
                patch.object(agent, "run_pytest", return_value=(0, None)) as run, \
                patch.object(agent, "run_tests") as newman, \
                patch.object(agent, "publish_results", return_value="fixture-run"):
            agent.execute({"id": 8, "kind": "run", "spec": job})
        run.assert_called_once()
        newman.assert_not_called()
        self.assertEqual(api.call_args.args[2]["status"], "done")

    def test_pytest_subprocess_excludes_library_configuration(self):
        job = {"project": "FIXTURE", "suite": "fixture.yaml", "env": "Fixture", "requested_by": "fixture",
               "destructive": False, "tags": [], "groups": []}
        log = Mock(cancel=True)
        with patch.object(agent, "pytest_venv", return_value=Path("fixture-python")), \
                patch.object(agent.subprocess, "run", return_value=Mock(stdout="{}")), \
                patch.object(agent, "read_env_file", return_value={"AUTOTEST_LIBRARY_API_KEY": "project-key"}), \
                patch.object(agent, "runtime_environment", return_value={}), \
                patch.object(agent, "pytest_command", return_value=["fixture-python", "-m", "pytest"]), \
                patch.object(agent, "stream", return_value=0) as stream:
            self.assertEqual(agent.run_pytest(log, 1, job, Path("tests"), Path("results")), (0, None))
        for name in agent.LIBRARY_API_NAMES:
            self.assertNotIn(name, stream.call_args.kwargs["env"])

    def test_stand_job_does_not_require_api(self):
        os.environ.clear()
        job = {"id": 9, "kind": "stand"}
        with patch.object(agent, "execute_stand") as stand, patch.object(agent, "prepare") as prepare:
            agent.execute(job)
        stand.assert_called_once_with(job)
        prepare.assert_not_called()

    def test_key_file_takes_priority_and_is_mounted_readonly(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "client-key"
            path.write_text("fixture-file-key", encoding="utf-8")
            os.environ["AUTOTEST_LIBRARY_API_KEY_FILE"] = str(path)
            job = {"suite": "fixture.yaml", "env": "Fixture", "requested_by": "fixture",
                   "destructive": False, "tags": [], "groups": []}
            log = Mock()
            with patch.object(agent, "runtime_environment", return_value={}), \
                    patch.object(agent, "stream", return_value=0) as stream, \
                    patch.object(agent.os, "getuid", return_value=1000, create=True), \
                    patch.object(agent.os, "getgid", return_value=1000, create=True):
                agent.run_tests(log, 1, job, Path("tests"), Path("results"))
            command = stream.call_args.args[1]
            self.assertIn(f"{path.resolve()}:/run/secrets/library-api-key:ro", command)
            self.assertIn("AUTOTEST_LIBRARY_API_KEY_FILE", command)
            self.assertNotIn("AUTOTEST_LIBRARY_API_KEY", command)
            self.assertEqual(stream.call_args.kwargs["env"]["AUTOTEST_LIBRARY_API_KEY_FILE"],
                             "/run/secrets/library-api-key")
            self.assertNotIn("fixture-file-key", str(command) + str(log.mock_calls))

    def test_runner_has_no_library_mount_and_key_is_not_in_argv_or_log(self):
        job = {"suite": "suites/fixture.yaml", "env": "Fixture", "requested_by": "fixture", "destructive": False,
               "tags": [], "groups": []}
        log = Mock()
        with patch.object(agent, "runtime_environment", return_value={}), patch.object(agent, "stream", return_value=0) as stream, \
                patch.object(agent.os, "getuid", return_value=1000, create=True), \
                patch.object(agent.os, "getgid", return_value=1000, create=True):
            agent.run_tests(log, 1, job, Path("tests"), Path("results"))
        command = stream.call_args.args[1]
        self.assertNotIn("fixture-library-key", " ".join(command))
        self.assertFalse(any(":/library" in item or item.startswith("AUTOTEST_LIBRARY=") for item in command))
        self.assertIn("AUTOTEST_LIBRARY_API_KEY", command)
        self.assertEqual(stream.call_args.kwargs["env"]["AUTOTEST_LIBRARY_API_KEY"], "fixture-library-key")
        self.assertNotIn("fixture-library-key", str(log.mock_calls))


if __name__ == "__main__":
    unittest.main()
