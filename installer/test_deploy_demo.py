import importlib.util
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("deploy_demo", ROOT / "installer/deploy_demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


class DemoTests(unittest.TestCase):
    def test_real_http_smoke_and_failure_artifacts(self):
        def load(name):
            spec = importlib.util.spec_from_file_location("demo_" + name, ROOT / "examples/template-demo" / (name + ".py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        app, tasks, smoke = load("app"), load("tasks"), load("smoke")
        with tempfile.TemporaryDirectory() as tmp:
            app.DATABASE = Path(tmp) / "demo.db"
            server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = "http://127.0.0.1:" + str(server.server_port)
                self.assertEqual(smoke.run(url, Path(tmp) / "before"), 2)
                tasks.apply("migrate", app.DATABASE)
                tasks.apply("seed", app.DATABASE)
                self.assertEqual(smoke.run(url, Path(tmp) / "after"), 0)
                self.assertEqual(len(list((Path(tmp) / "after/allure-results").glob("*-result.json"))), 2)
                self.assertTrue((Path(tmp) / "before/junit.xml").is_file())
                self.assertIn("не принят", (Path(tmp) / "before/report.md").read_text(encoding="utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_real_git_refs_and_repeat_preserves_local_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "space in name"
            path = demo.prepare_demo(target, "feature-demo", "test")
            snapshot = json.loads(path.read_text())
            self.assertNotEqual(snapshot["components"][0]["sha"], snapshot["components"][1]["sha"])
            self.assertEqual(json.loads((path.parent / "contexts/api/version.json").read_text())["version"], "feature-demo")
            self.assertEqual(json.loads((path.parent / "contexts/smoke/version.json").read_text())["version"], "test")
            source = target / "source"
            (source / "version.json").write_text("local edit")
            self.assertEqual(demo.prepare_demo(target), path)
            self.assertEqual((source / "version.json").read_text(), "local edit")
            self.assertEqual(demo.git(source, "branch", "--show-current"), "test")
            with self.assertRaisesRegex(demo.StandError, "different refs"):
                demo.prepare_demo(target, "test")

    def test_nonempty_workspace_and_missing_ref_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "occupied"
            target.mkdir()
            (target / "keep.txt").write_text("keep")
            with self.assertRaises(demo.StandError):
                demo.prepare_demo(target)
            self.assertEqual((target / "keep.txt").read_text(), "keep")
            target = Path(tmp) / "missing-ref"
            with self.assertRaisesRegex(demo.StandError, "missing or ambiguous"):
                demo.prepare_demo(target, "not-a-branch")
            self.assertFalse((target / "build/snapshot.json").exists())
            self.assertTrue(demo.prepare_demo(target, "test").is_file())

    def test_migration_and_seed_are_repeatable_and_preserve_user_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(__import__("os").environ, DATABASE=str(Path(tmp) / "demo.db"))
            tasks = ROOT / "examples/template-demo/tasks.py"
            for action in ["migrate", "seed"]:
                subprocess.run([sys.executable, str(tasks), action], env=env, check=True, capture_output=True)
            with closing(sqlite3.connect(env["DATABASE"])) as db, db:
                db.execute("INSERT INTO items VALUES (2, 'keep')")
            for action in ["migrate", "seed"]:
                subprocess.run([sys.executable, str(tasks), action], env=env, check=True, capture_output=True)
            with closing(sqlite3.connect(env["DATABASE"])) as db:
                self.assertEqual(db.execute("SELECT * FROM items ORDER BY id").fetchall(), [(1, "demo"), (2, "keep")])


if __name__ == "__main__":
    unittest.main()
