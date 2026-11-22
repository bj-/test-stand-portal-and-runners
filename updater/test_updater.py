"""Real local Git plus a deterministic host adapter; no Docker or network needed."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

spec = importlib.util.spec_from_file_location("host_updater", Path(__file__).with_name("run.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FakeAdapter(module.Runner):
    def __init__(self, current):
        self.current = current
        self.previous = current
        self.calls = []
        self.fail = set()
        self.running = [0]
        self.maintenance = False
        self.callback = None
        self.activated = False

    def run(self, args, timeout, capture=False, output=None):
        if len(args) < 2 or args[1] != "fake-adapter":
            return super().run(args, timeout, capture=capture, output=output)
        command = args[2]
        self.calls.append(command)
        if self.callback:
            self.callback(command)
        if command in self.fail or (command == "health" and self.activated and "new-health" in self.fail):
            raise module.UpdateError("password=secret-which-must-not-leak")
        result = {}
        if command == "current":
            result = {"revision": self.current}
        elif command in {"drain", "idle"}:
            self.maintenance = True
            result = {"maintenance": True, "running": self.running[0]}
            if len(self.running) > 1:
                self.running.pop(0)
        elif command == "backup":
            result = {"backup": "/private-backup/location"}
        elif command == "activate":
            self.previous, self.current = self.current, args[4]
            self.activated = True
        elif command == "rollback":
            self.current = self.previous
            self.activated = False
        elif command == "resume":
            self.maintenance = False
        return json.dumps(result) if capture else None


class Clock:
    def __init__(self):
        self.value = 0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "origin"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.email", "updater@example.invalid")
        self.git("config", "user.name", "Updater Test")
        self.first = self.commit("first")
        self.config = module.Config(dict(repository=str(self.repo), ref="refs/heads/main",
                                         state_dir=str(self.root / "state"), work_dir=str(self.root / "work"),
                                         adapter=[sys.executable, "fake-adapter"], poll_seconds=1,
                                         drain_timeout_seconds=3))
        self.adapter = FakeAdapter(self.first)
        self.clock = Clock()
        self.updater = self.make_updater()

    def make_updater(self):
        return module.Updater(self.config, self.adapter, self.clock.now, self.clock.sleep)

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return result.stdout.decode().strip()

    def commit(self, contents):
        (self.repo / "content.txt").write_text(contents)
        self.git("add", "content.txt")
        self.git("commit", "-m", contents)
        return self.git("rev-parse", "HEAD")

    def request(self, action="apply", queued=False):
        request = dict(id=str(uuid.uuid4()), action=action, requested_by="test", created_at=module.now())
        if queued:
            module.atomic_json(self.config.state_dir / "requests" / (request["id"] + ".json"), request)
        return request

    def result(self, request):
        return module.read_json(self.config.state_dir / "operations" / (request["id"] + ".json"))

    def test_unchanged_does_not_build_or_drain(self):
        request = self.request()
        self.updater.execute(request)
        self.assertEqual(self.result(request)["phase"], "unchanged")
        self.assertEqual(self.adapter.calls, ["current"])

    def test_update_orders_backup_activation_and_retains_source(self):
        second = self.commit("second")
        request = self.request()
        self.updater.execute(request)
        self.assertEqual(self.adapter.calls, ["current", "prepare", "drain", "backup", "activate", "health", "resume"])
        self.assertEqual(self.result(request)["phase"], "succeeded")
        self.assertEqual(self.updater.status["current_revision"], second)
        self.assertEqual((self.config.work_dir / "releases" / second / "content.txt").read_text(), "second")
        self.assertEqual((self.repo / "content.txt").read_text(), "second")
        self.assertFalse(self.adapter.maintenance)

    def test_check_does_not_apply_even_with_auto_apply_enabled(self):
        self.commit("second")
        request = self.request("check")
        self.updater.execute(request)
        self.assertEqual(self.result(request)["phase"], "available")
        self.assertEqual(self.adapter.calls, ["current"])

    def test_offline_leaves_existing_portal_alone(self):
        self.config.repository = str(self.root / "absent")
        request = self.request()
        self.updater.execute(request)
        self.assertEqual(self.result(request)["phase"], "failed")
        self.assertEqual(self.adapter.calls, ["current"])
        self.assertEqual(self.adapter.current, self.first)

    def test_build_failure_does_not_drain_or_leak_output(self):
        self.commit("second")
        self.adapter.fail.add("prepare")
        request = self.request()
        self.updater.execute(request)
        self.assertEqual(self.adapter.calls, ["current", "prepare"])
        self.assertEqual(self.result(request)["phase"], "failed")
        for path in self.config.state_dir.rglob("*.json"):
            self.assertNotIn("secret", path.read_text())

    def test_drains_until_zero(self):
        self.commit("second")
        self.adapter.running = [2, 1, 0]
        self.updater.execute(self.request())
        self.assertEqual(self.adapter.calls.count("idle"), 2)
        self.assertEqual(self.adapter.current, self.updater.status["available_revision"])

    def test_drain_timeout_resumes_without_activation(self):
        self.commit("second")
        self.adapter.running = [2]
        request = self.request()
        self.updater.execute(request)
        self.assertEqual(self.result(request)["phase"], "failed")
        self.assertNotIn("activate", self.adapter.calls)
        self.assertEqual(self.adapter.calls[-2:], ["health", "resume"])
        self.assertFalse(self.adapter.maintenance)

    def test_backup_failure_resumes_without_activation(self):
        self.commit("second")
        self.adapter.fail.add("backup")
        self.updater.execute(self.request())
        self.assertNotIn("activate", self.adapter.calls)
        self.assertEqual(self.adapter.calls[-2:], ["health", "resume"])

    def test_failed_health_rolls_back_only_through_adapter(self):
        self.commit("second")
        self.adapter.fail.add("new-health")
        request = self.request()
        self.updater.execute(request)
        self.assertEqual(self.result(request)["phase"], "failed")
        self.assertEqual(self.adapter.calls[-3:], ["rollback", "health", "resume"])
        self.assertEqual(self.adapter.current, self.first)
        self.assertFalse(self.adapter.maintenance)

    def test_schema_change_rollback_refusal_keeps_maintenance_across_checks_and_restart(self):
        self.commit("second")
        self.adapter.fail.update({"new-health", "rollback"})
        request = self.request()
        self.updater.execute(request)
        self.assertEqual(self.result(request)["phase"], "recovery_required")
        self.assertTrue(self.adapter.maintenance)
        self.assertNotIn("resume", self.adapter.calls)
        self.updater.execute(self.request("check"))
        self.assertEqual(self.updater.status["phase"], "recovery_required")
        resumed = self.make_updater()
        resumed.recover_startup()
        resumed.execute(self.request())
        self.assertEqual(resumed.status["phase"], "recovery_required")
        self.assertEqual(self.adapter.calls.count("activate"), 1)

    def test_startup_activation_interruption_never_resumes(self):
        self.updater.save_status("activating", "Interrupted")
        resumed = self.make_updater()
        resumed.recover_startup()
        self.assertEqual(resumed.status["phase"], "recovery_required")
        self.assertEqual(self.adapter.calls, [])

    def test_restart_during_blocked_check_retains_protected_recovery_latch(self):
        self.updater.save_status("recovery_required", "Repair required")
        self.updater.status["phase"] = "checking"
        module.atomic_json(self.config.state_dir / "status.json", self.updater.status)
        resumed = self.make_updater()
        resumed.recover_startup()
        self.assertEqual(resumed.status["phase"], "recovery_required")
        self.assertTrue(module.read_json(self.config.work_dir / "worker.json")["recovery_required"])
        self.assertFalse((self.config.state_dir / "worker.json").exists())

    def test_resume_failure_never_rolls_back_after_admission_might_have_opened(self):
        self.commit("second")
        self.adapter.fail.add("resume")
        request = self.request()
        self.updater.execute(request)
        self.assertEqual(self.result(request)["phase"], "recovery_required")
        self.assertEqual(self.result(request)["failure_phase"], "resuming")
        self.assertNotIn("rollback", self.adapter.calls)

    def test_failure_phase_keeps_original_stage_after_rollback(self):
        self.commit("second")
        self.adapter.fail.add("new-health")
        request = self.request()
        self.updater.execute(request)
        self.assertEqual(self.result(request)["failure_phase"], "verifying")
        self.assertEqual(self.result(request)["error_code"], "update_failed")

    def test_startup_drain_interruption_verifies_before_resume(self):
        self.updater.save_status("draining", "Interrupted")
        resumed = self.make_updater()
        resumed.recover_startup()
        self.assertEqual(self.adapter.calls, ["health", "resume"])
        self.assertEqual(resumed.status["phase"], "idle")

    def test_concurrent_requests_share_one_build_and_all_finish(self):
        self.commit("second")
        first = self.request(queued=True)
        second = self.request(queued=True)
        check = self.request("check", queued=True)
        late = self.request()
        def enqueue(command):
            if command == "prepare":
                module.atomic_json(self.config.state_dir / "requests" / (late["id"] + ".json"), late)
        self.adapter.callback = enqueue
        self.updater.tick()
        for request in (first, second, check, late):
            self.assertEqual(self.result(request)["phase"], "succeeded")
        self.assertEqual(self.adapter.calls.count("prepare"), 1)
        self.assertEqual(self.updater.pending(), [])

    def test_apply_arriving_during_check_is_queued_for_next_tick(self):
        self.commit("second")
        check = self.request("check", queued=True)
        late = self.request()
        def enqueue(command):
            if command == "current":
                module.atomic_json(self.config.state_dir / "requests" / (late["id"] + ".json"), late)
        self.adapter.callback = enqueue
        self.updater.tick()
        self.assertEqual(self.result(check)["phase"], "available")
        self.assertIsNone(self.result(late))
        self.adapter.callback = None
        self.updater.tick()
        self.assertEqual(self.result(late)["phase"], "succeeded")

    def test_startup_and_hourly_monotonic_schedule(self):
        self.commit("second")
        self.updater.tick()
        self.assertEqual(self.adapter.calls.count("activate"), 1)
        self.clock.value = 3599
        self.updater.tick()
        self.assertEqual(self.adapter.calls.count("current"), 1)
        self.clock.value = 3600
        self.updater.tick()
        self.assertEqual(self.adapter.calls.count("current"), 2)
        self.assertEqual(self.adapter.calls.count("prepare"), 1)

    def test_readiness_wait_does_not_consume_startup_check(self):
        self.adapter.fail.add("health")
        self.updater.tick()
        self.assertTrue(self.updater.startup_pending)
        self.assertEqual(self.adapter.calls, ["health"])
        self.adapter.fail.clear()
        self.updater.tick()
        self.assertFalse(self.updater.startup_pending)
        self.assertEqual(self.adapter.calls[-1], "current")

    def test_failed_revision_not_rebuilt_automatically_but_manual_retry_allowed(self):
        self.commit("second")
        self.adapter.fail.add("prepare")
        self.updater.tick()
        self.clock.value = 3600
        self.updater.tick()
        self.assertEqual(self.adapter.calls.count("prepare"), 1)
        self.adapter.fail.clear()
        self.updater.execute(self.request())
        self.assertEqual(self.adapter.calls.count("prepare"), 2)
        self.assertEqual(self.updater.status["phase"], "succeeded")

    def test_failed_apply_invalidates_prepared_marker_for_explicit_retry(self):
        candidate = self.commit("second")
        self.adapter.fail.add("backup")
        self.updater.execute(self.request())
        self.assertFalse((self.config.work_dir / "releases" / (candidate + ".prepared.json")).exists())
        self.adapter.fail.clear()
        self.updater.execute(self.request())
        self.assertEqual(self.adapter.calls.count("prepare"), 2)
        self.assertEqual(self.updater.status["phase"], "succeeded")

    def test_manual_recovery_requires_health_before_resume(self):
        self.updater.save_status("recovery_required", "Repair required")
        self.adapter.fail.add("health")
        with self.assertRaises(module.UpdateError):
            self.updater.acknowledge_recovery()
        self.assertNotIn("resume", self.adapter.calls)
        self.adapter.fail.clear()
        self.updater.acknowledge_recovery()
        self.assertEqual(self.updater.status["phase"], "idle")
        self.assertNotIn("recovery_required", self.updater.internal)

    def test_archive_rejects_symlinks_before_prepare(self):
        # Git plumbing creates a symlink entry even on Windows without symlink privileges.
        blob = subprocess.run(["git", "-C", str(self.repo), "hash-object", "-w", "--stdin"],
                              input=b"../../outside", stdout=subprocess.PIPE, check=True).stdout.decode().strip()
        self.git("update-index", "--add", "--cacheinfo", "120000," + blob + ",unsafe-link")
        self.git("commit", "-m", "unsafe-link")
        request = self.request()
        self.updater.execute(request)
        self.assertEqual(self.result(request)["phase"], "failed")
        self.assertNotIn("prepare", self.adapter.calls)

    def test_status_and_operation_expose_only_contract_fields(self):
        request = self.request()
        self.updater.execute(request)
        self.assertLessEqual(set(self.result(request)), {"id", "action", "requested_by", "created_at", "started_at", "finished_at", "phase", "message", "current_revision", "available_revision", "failure_phase", "error_code"})
        self.assertLessEqual(set(self.updater.status), {"enabled", "current_revision", "available_revision", "last_checked_at", "phase", "message", "last_operation_id", "updated_at", "failure_phase", "error_code"})

    @unittest.skipUnless(os.name == "posix", "Unix filesystem permissions")
    def test_export_source_readable_by_unprivileged_agents_under_restrictive_umask(self):
        (self.repo / "nested").mkdir()
        (self.repo / "nested" / "file").write_text("readable")
        self.git("add", "nested/file")
        self.git("commit", "-m", "nested")
        revision = self.git("rev-parse", "HEAD")
        previous = os.umask(0o077)
        try:
            self.updater.execute(self.request())
        finally:
            os.umask(previous)
        source = self.config.work_dir / "releases" / revision
        for path in (self.config.work_dir, source.parent, source, source / "nested"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o755)
        self.assertEqual((source / "nested" / "file").stat().st_mode & 0o777, 0o644)

    def test_changed_worker_restarts_only_after_safe_terminal_result(self):
        self.updater.script_digest = b"changed"
        with mock.patch.object(module.os, "execv") as execute:
            self.updater.status["phase"] = "preparing"
            self.updater.restart_if_updated()
            execute.assert_not_called()
            self.updater.status["phase"] = "recovery_required"
            self.updater.restart_if_updated()
            execute.assert_not_called()
            self.updater.status["phase"] = "succeeded"
            self.updater.restart_if_updated()
            execute.assert_called_once_with(sys.executable, [sys.executable, *sys.argv])

    @unittest.skipUnless(os.name == "posix", "Linux process group and flock behavior")
    def test_process_lock_rejects_second_worker(self):
        with module.process_lock(self.config.state_dir / "worker.lock"):
            with self.assertRaises(module.UpdateError):
                with module.process_lock(self.config.state_dir / "worker.lock"):
                    pass

    def test_command_timeout_is_bounded_and_output_not_in_exception(self):
        with self.assertRaises(module.UpdateError) as error:
            module.Runner().run([sys.executable, "-c", "import time; print('private-token'); time.sleep(20)"], 0.1, capture=True)
        self.assertEqual(str(error.exception), "Command timed out")


if __name__ == "__main__":
    unittest.main()
