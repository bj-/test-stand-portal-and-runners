"""Настройка раннера на искусственных данных; Docker и Git не запускаются."""
import importlib.util
import os
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
        with patch.object(agent, "LIBRARY_URL", "fixture-repository"):
            with self.assertRaises(RuntimeError):
                agent.library_configuration()

    def test_runner_has_no_library_mount_and_key_is_not_in_argv_or_log(self):
        job = {"suite": "suites/fixture.yaml", "env": "Fixture", "requested_by": "fixture", "destructive": False,
               "tags": [], "groups": []}
        log = Mock()
        with patch.object(agent, "runtime_environment", return_value={}), patch.object(agent, "stream", return_value=0) as stream, \
                patch.object(agent.os, "getuid", return_value=1000, create=True), \
                patch.object(agent.os, "getgid", return_value=1000, create=True):
            agent.run_tests(log, 1, job, Path("tests"), Path("results"), Path("missing-library"))
        command = stream.call_args.args[1]
        self.assertNotIn("fixture-library-key", " ".join(command))
        self.assertFalse(any(":/library" in item or item.startswith("AUTOTEST_LIBRARY=") for item in command))
        self.assertIn("AUTOTEST_LIBRARY_API_KEY", command)
        self.assertEqual(stream.call_args.kwargs["env"]["AUTOTEST_LIBRARY_API_KEY"], "fixture-library-key")
        self.assertNotIn("fixture-library-key", str(log.mock_calls))


if __name__ == "__main__":
    unittest.main()
