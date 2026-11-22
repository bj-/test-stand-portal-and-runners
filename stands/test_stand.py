"""Contract boundaries and immutable export against real temporary Git repos."""

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from stands import stand


EXAMPLE = Path(__file__).parent / "examples" / "http-smoke.template.json"


def git(path, *args, data=None):
    result = subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=Stand Test", "-c",
         "user.email=stand@example.invalid", "-c", "core.autocrlf=false", *args],
        input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
    if result.returncode:
        raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
    return result.stdout


def commit(path, text):
    (path / "content.txt").write_text(text, encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-qm", text)
    return git(path, "rev-parse", "HEAD").decode().strip()


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.template = stand.read_json(EXAMPLE)

    def test_example_and_engine_names(self):
        self.template["smoke"]["env"] = "123-test"
        self.template["smoke"]["suite"] = "suites/API/smoke.yaml"
        self.assertEqual(stand.validate_template(self.template)["version"], "1.0.0")

    def test_optional_liveness_contract_and_legacy(self):
        self.assertEqual(stand.STAGES, ("build", "deploy", "migrate", "seed", "healthcheck", "smoke"))
        stand.validate_template(self.template)
        command = {"component": "smoke", "argv": ["docker", "compose", "run", "--rm", "--no-deps", "runner"]}
        self.template["liveness"] = {"api": command}
        stand.validate_template(self.template)
        for probes in ({}, [], {"absent": command}, {"api": {"component": "absent", "argv": ["docker"]}},
                       {"api": {"component": "smoke", "argv": []}},
                       {"api": {"component": "smoke", "argv": ["docker", None]}},
                       {"api": {**command, "extra": True}}):
            with self.subTest(probes=probes), self.assertRaises(stand.StandError):
                stand.validate_template({**self.template, "liveness": probes})

    def test_bad_template_boundaries(self):
        cases = [
            ("unknown root", lambda t: t.update(command=["rm"])),
            ("bool schema", lambda t: t.update(schema_version=True)),
            ("future schema", lambda t: t.update(schema_version=2)),
            ("bool memory", lambda t: t["profiles"]["laptop"]["resources"].update(memory_mb=True)),
            ("bool cpus", lambda t: t["profiles"]["laptop"]["resources"].update(cpus=True)),
            ("zero cpus", lambda t: t["profiles"]["laptop"]["resources"].update(cpus=0)),
            ("infinite cpus", lambda t: t["profiles"]["laptop"]["resources"].update(cpus=float("inf"))),
            ("extra resources", lambda t: t["profiles"]["laptop"]["resources"].update(disk=1)),
            ("no laptop", lambda t: t["profiles"].pop("laptop")),
            ("missing required service", lambda t: t["profiles"]["laptop"].update(services=["other"])),
            ("duplicate component", lambda t: t["components"].append(copy.deepcopy(t["components"][0]))),
            ("case duplicate component", lambda t: t["components"][1].update(id="API")),
            ("missing dependency", lambda t: t["components"][0].update(dependencies=["absent"])),
            ("cyclic dependency", lambda t: t["components"][0].update(dependencies=["smoke"])),
            ("duplicate dependency", lambda t: t["components"][1].update(dependencies=["api", "api"])),
            ("nonportable ID", lambda t: t["components"][0].update(id="CON")),
            ("dot ID", lambda t: t["components"][0].update(id="api.")),
            ("compose traversal", lambda t: t["compose"].update(path="../compose.yaml")),
            ("compose absolute", lambda t: t["compose"].update(path="/compose.yaml")),
            ("compose drive", lambda t: t["compose"].update(path="C:/compose.yaml")),
            ("compose backslash", lambda t: t["compose"].update(path="a\\compose.yaml")),
            ("compose component type", lambda t: t["compose"].update(component=[])),
            ("command component type", lambda t: t["lifecycle"]["build"][0].update(component={})),
            ("runner component type", lambda t: t["smoke"].update(runner=[])),
            ("binding component type", lambda t: t["output_bindings"]["baseUrl"].update(service={})),
            ("parameter type type", lambda t: t["parameters"]["verbose"].update(type=[])),
            ("parameter bool integer", lambda t: t["parameters"]["timeout_seconds"].update(default=True)),
            ("empty mandatory stage", lambda t: t["lifecycle"].update(build=[])),
            ("shell command", lambda t: t["lifecycle"]["build"][0].update(argv="docker compose build")),
            ("NUL arg", lambda t: t["lifecycle"]["build"][0].update(argv=["python", "x\x00y"])),
            ("extra command", lambda t: t["lifecycle"]["build"][0].update(shell=True)),
            ("secret values", lambda t: t.update(secret_refs={"password": "value"})),
            ("secret URL", lambda t: t.update(secret_refs=["https://example.invalid/secret"])),
            ("binding bool port", lambda t: t["output_bindings"]["baseUrl"].update(port=True)),
            ("binding nonrequired service", lambda t: t["output_bindings"]["baseUrl"].update(service="optional")),
            ("env traversal", lambda t: t["smoke"].update(env="../local")),
            ("suite traversal", lambda t: t["smoke"].update(suite="suites/../../smoke.yaml")),
        ]
        for label, mutate in cases:
            with self.subTest(label=label):
                template = copy.deepcopy(self.template)
                mutate(template)
                with self.assertRaises(stand.StandError):
                    stand.validate_template(template)

    def test_ref_grammar(self):
        for value in ("main", "test", "feature/a", "refs/heads/test", "refs/tags/v1", "v1.2.3", "a" * 40):
            with self.subTest(valid=value):
                stand.ref_name(value)
        for value in ("HEAD", "-main", "--help", "main~1", "main^", "main:path", "main@{1}",
                      "main..test", "refs/remotes/origin/main", "a//b", "a/../b", "a.lock",
                      "a/.hidden", "a b", "a\nb", "a\\b", "a?b", "a[b", "https://example.invalid/repo"):
            with self.subTest(invalid=value), self.assertRaises(stand.StandError):
                stand.ref_name(value)

    def test_portable_path_grammar(self):
        for value in ("..", "a/../x", "a//b", "./file", "a/.git/config", "AUX.txt", "a/CON",
                      "a/trailing.", "a/trailing ", "a\x00b", "a\nb", "a:b", "a\\b", "C:/file"):
            with self.subTest(value=value), self.assertRaises(stand.StandError):
                stand.relative_path(value, "test")

    def test_json_duplicate_keys_and_nonfinite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            for text in ('{"schema_version":1,"schema_version":1}', '{"value": NaN}', '{"value": Infinity}'):
                with self.subTest(text=text):
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaises(stand.StandError):
                        stand.read_json(path)


class GitPreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.TemporaryDirectory(prefix="stand-fixture-")
        cls.root = Path(cls.base.name)
        for name in ("api", "smoke"):
            path = cls.root / name
            path.mkdir()
            git(path, "init", "-q", "-b", "main")
            (path / "compose.yaml").write_text("services:\n  api:\n    image: scratch\n", encoding="utf-8")
            commit(path, name + "-main")
            git(path, "checkout", "-qb", "test")
            commit(path, name + "-test")
            git(path, "checkout", "-qb", "feature/work", "main")
            commit(path, name + "-feature")
            git(path, "checkout", "-q", "main")

    @classmethod
    def tearDownClass(cls):
        cls.base.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="stand-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sources = {}
        for name in ("api", "smoke"):
            self.sources[name] = self.root / name
            shutil.copytree(Path(self.base.name) / name, self.sources[name])
        self.template = stand.read_json(EXAMPLE)
        self.catalog = {"schema_version": 1, "repositories": [
            {"id": "service-src", "path": str(self.sources["api"]), "components": ["api"]},
            {"id": "runner-src", "path": str(self.sources["smoke"]), "components": ["smoke"]}]}
        self.request = {"schema_version": 1, "profile": "laptop", "parameters": {},
                        "components": {"api": {"ref": "test"}, "smoke": {"ref": "feature/work"}}}

    def prepare(self, name="build"):
        return stand.prepare(self.template, self.catalog, self.request, self.root / name)

    def assert_no_publication(self, name="build"):
        self.assertFalse((self.root / name).exists())
        self.assertEqual(list(self.root.glob(".stand-stage-*")), [])

    def test_different_refs_isolation_dirty_sources_and_snapshot(self):
        original = {}
        for name, path in self.sources.items():
            (path / "content.txt").write_text("dirty", encoding="utf-8")
            (path / "staged.txt").write_text("staged", encoding="utf-8")
            git(path, "add", "staged.txt")
            (path / "untracked.txt").write_text("untracked", encoding="utf-8")
            original[name] = (git(path, "rev-parse", "HEAD"), git(path, "status", "--porcelain=v1", "-z"),
                              (path / ".git" / "index").read_bytes(),
                              {p.name: p.read_bytes() for p in path.iterdir() if p.is_file()})
        snapshot_path = self.prepare()
        snapshot = stand.read_json(snapshot_path)
        by_id = {c["id"]: c for c in snapshot["components"]}
        self.assertNotEqual(by_id["api"]["sha"], by_id["smoke"]["sha"])
        self.assertEqual(by_id["api"]["sha"], git(self.sources["api"], "rev-parse", "test").decode().strip())
        self.assertEqual(by_id["smoke"]["sha"], git(self.sources["smoke"], "rev-parse", "feature/work").decode().strip())
        self.assertEqual(snapshot["parameters"], {"timeout_seconds": 30, "verbose": False})
        self.assertTrue((snapshot_path.parent / stand.MARKER).is_file())
        for name, path in self.sources.items():
            self.assertEqual(original[name], (git(path, "rev-parse", "HEAD"), git(path, "status", "--porcelain=v1", "-z"),
                                             (path / ".git" / "index").read_bytes(),
                                             {p.name: p.read_bytes() for p in path.iterdir() if p.is_file()}))
            context = snapshot_path.parent / "contexts" / name
            self.assertFalse((context / ".git").exists())
            self.assertFalse((context / "untracked.txt").exists())
            self.assertFalse((context / "staged.txt").exists())
        self.assertEqual((snapshot_path.parent / "contexts/api/content.txt").read_text(), "api-test")
        self.assertEqual((snapshot_path.parent / "contexts/smoke/content.txt").read_text(), "smoke-feature")
        serialized = json.dumps(snapshot)
        for path in self.sources.values():
            self.assertNotIn(str(path), serialized)
            self.assertNotIn(path.as_posix(), serialized)
        self.assertNotIn('"path": ' + json.dumps(str(self.sources["api"])), serialized)
        self.assertNotIn("https://", serialized)

    def test_replay_pins_old_sha_and_frozen_template(self):
        snapshot_path = self.prepare()
        snapshot = stand.read_json(snapshot_path)
        source = self.sources["api"]
        git(source, "checkout", "-q", "test")
        commit(source, "api-new-test")
        self.template["components"][0]["default_ref"] = "missing"
        self.template["lifecycle"]["build"][0]["argv"] = ["changed"]
        replayed = stand.replay(snapshot, self.catalog, self.root / "replayed")
        self.assertEqual(replayed.read_bytes(), snapshot_path.read_bytes())
        self.assertEqual((replayed.parent / "contexts/api/content.txt").read_text(), "api-test")

    def test_missing_ref_and_revision_expression_have_no_fallback(self):
        for ref in ("does-not-exist", "main~1", "--help", "HEAD"):
            with self.subTest(ref=ref):
                self.request["components"]["api"]["ref"] = ref
                with self.assertRaises(stand.StandError):
                    self.prepare()
                self.assert_no_publication()

    def test_tags_full_sha_and_ambiguous_names(self):
        source = self.sources["api"]
        git(source, "tag", "-a", "release-v1", "test", "-m", "release")
        repositories = stand.validate_catalog(self.catalog)
        expected = git(source, "rev-parse", "test").decode().strip()
        self.assertEqual(stand.resolve_ref(repositories["service-src"]["path"], "release-v1"), expected)
        self.assertEqual(stand.resolve_ref(source, "refs/heads/test"), expected)
        self.assertEqual(stand.resolve_ref(source, expected), expected)
        tag_object = git(source, "rev-parse", "release-v1").decode().strip()
        with self.assertRaises(stand.StandError):
            stand.resolve_ref(source, tag_object)
        git(source, "tag", "test", "main")
        with self.assertRaises(stand.StandError):
            stand.resolve_ref(source, "test")
        self.assertEqual(stand.resolve_ref(source, "refs/heads/test"), expected)

    def test_bad_request_fields_types_catalog_and_compatibility(self):
        repositories = stand.validate_catalog(self.catalog)
        cases = [
            ("unknown request", lambda r: r.update(argv=["arbitrary"])),
            ("request URL", lambda r: r["components"]["api"].update(repository="https://example.invalid/repo")),
            ("unknown component", lambda r: r["components"].update(other={})),
            ("command in choice", lambda r: r["components"]["api"].update(command="anything")),
            ("wrong repository compatibility", lambda r: r["components"]["api"].update(repository="runner-src")),
            ("unknown parameter", lambda r: r["parameters"].update(unlisted=1)),
            ("bool integer", lambda r: r["parameters"].update(timeout_seconds=True)),
            ("integer bool", lambda r: r["parameters"].update(verbose=1)),
            ("unknown profile", lambda r: r.update(profile="missing")),
            ("profile type", lambda r: r.update(profile=[])),
            ("secrets forbidden", lambda r: r.update(secret_refs={"password": "value"})),
        ]
        for label, mutate in cases:
            with self.subTest(label=label):
                request = copy.deepcopy(self.request)
                mutate(request)
                with self.assertRaises(stand.StandError):
                    stand.validate_request(self.template, request, repositories)
        for label, mutate in (
            ("duplicate", lambda c: c["repositories"].append(copy.deepcopy(c["repositories"][0]))),
            ("relative", lambda c: c["repositories"][0].update(path="sources/api")),
            ("absent", lambda c: c["repositories"][0].update(path=str(self.root / "absent"))),
            ("credentials", lambda c: c["repositories"][0].update(token="never")),
            ("unknown", lambda c: c.update(url="https://example.invalid"))):
            with self.subTest(catalog=label):
                catalog = copy.deepcopy(self.catalog)
                mutate(catalog)
                with self.assertRaises(stand.StandError):
                    stand.validate_catalog(catalog)

    def test_parameter_required_enum_and_defaults(self):
        self.template["parameters"]["mode"] = {"type": "string", "enum": ["quick", "full"]}
        repositories = stand.validate_catalog(self.catalog)
        with self.assertRaises(stand.StandError):
            stand.validate_request(self.template, self.request, repositories)
        self.request["parameters"]["mode"] = "other"
        with self.assertRaises(stand.StandError):
            stand.validate_request(self.template, self.request, repositories)
        self.request["parameters"]["mode"] = "quick"
        _, parameters = stand.validate_request(self.template, self.request, repositories)
        self.assertEqual(parameters["mode"], "quick")

    def test_workspace_containment_and_existing_target(self):
        repositories = stand.validate_catalog(self.catalog)
        for target in (self.sources["api"], self.sources["api"] / "build", self.root,
                       stand.SOURCE_ROOT / "stands" / "forbidden-build"):
            with self.subTest(target=target), self.assertRaises(stand.StandError):
                stand.check_workspace(target, repositories)
        target = self.root / "build"
        target.mkdir()
        sentinel = target / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        with self.assertRaises(stand.StandError):
            self.prepare()
        self.assertEqual(sentinel.read_text(), "keep")
        empty = self.root / "empty"
        empty.mkdir()
        snapshot = self.prepare("empty")
        original = snapshot.read_bytes()
        with self.assertRaises(stand.StandError):
            self.prepare("empty")
        self.assertEqual(snapshot.read_bytes(), original)

    def test_snapshot_hash_fields_components_and_unavailable_sha(self):
        snapshot = stand.read_json(self.prepare())
        cases = [
            ("hash", lambda s: s["template"].update(version="2.0.0")),
            ("unknown", lambda s: s.update(path=str(self.sources["api"]))),
            ("missing component", lambda s: s["components"].pop()),
            ("duplicate component", lambda s: s["components"].append(copy.deepcopy(s["components"][0]))),
            ("short SHA", lambda s: s["components"][0].update(sha="abcdef")),
            ("unknown SHA", lambda s: s["components"][0].update(sha="f" * 40)),
            ("missing parameter", lambda s: s["parameters"].pop("verbose")),
            ("secret values", lambda s: s.update(secret_refs={"token": "value"})),
        ]
        for label, mutate in cases:
            with self.subTest(label=label):
                broken = copy.deepcopy(snapshot)
                mutate(broken)
                with self.assertRaises(stand.StandError):
                    stand.replay(broken, self.catalog, self.root / "replayed")
                self.assert_no_publication("replayed")

    def test_missing_compose_cleans_staging(self):
        self.template["compose"]["path"] = "missing.yaml"
        with self.assertRaises(stand.StandError):
            self.prepare()
        self.assert_no_publication()

    def test_nested_tracked_symlink_and_gitlink_rejected(self):
        source = self.sources["api"]
        git(source, "checkout", "-q", "test")
        oid = git(source, "hash-object", "-w", "--stdin", data=b"../../../../outside\n").decode().strip()
        git(source, "update-index", "--add", "--cacheinfo", f"120000,{oid},nested/escape")
        git(source, "commit", "-qm", "tracked symlink")
        with self.assertRaisesRegex(stand.StandError, "symlinks"):
            self.prepare()
        self.assert_no_publication()
        git(source, "reset", "--hard", "HEAD~1")
        commit_sha = git(source, "rev-parse", "HEAD").decode().strip()
        git(source, "update-index", "--add", "--cacheinfo", f"160000,{commit_sha},nested/module")
        git(source, "commit", "-qm", "gitlink")
        with self.assertRaisesRegex(stand.StandError, "submodules"):
            self.prepare()
        self.assert_no_publication()

    def test_case_colliding_parent_directories_rejected(self):
        source = self.sources["api"]
        git(source, "checkout", "-q", "test")
        oid = git(source, "hash-object", "-w", "--stdin", data=b"content").decode().strip()
        for name in ("Dir/a", "dir/b"):
            git(source, "update-index", "--add", "--cacheinfo", f"100644,{oid},{name}")
        git(source, "commit", "-qm", "case collision")
        with self.assertRaisesRegex(stand.StandError, "case-colliding"):
            self.prepare()
        self.assert_no_publication()

    def test_workspace_nested_link_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.root / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            # Windows junctions need no Developer Mode or elevation.
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
            if result.returncode:
                self.skipTest("OS does not permit symlinks or junctions")
        try:
            with self.assertRaisesRegex(stand.StandError, "symlink/junction"):
                stand.prepare(self.template, self.catalog, self.request, link / "build")
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            if stand.is_link(link):
                if link.is_symlink():
                    link.unlink()
                else:
                    link.rmdir()


if __name__ == "__main__":
    unittest.main()
