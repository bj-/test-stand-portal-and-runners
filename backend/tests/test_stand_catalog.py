"""Portable v1 compatibility and installation-only, read-only Git observations."""

import copy
import json
import os
from pathlib import Path
import subprocess

import pytest

from portal import stand_catalog as catalog
from portal import stand_contract as contract


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def template():
    # Neutral portable example; checked in locally, with no sibling-checkout import.
    return json.loads((FIXTURES / "stand-http-smoke.template.json").read_text(encoding="utf-8"))


def request():
    return {"schema_version": 1, "profile": "laptop", "components": {}, "parameters": {}}


def repositories():
    return {"service-src": {"components": ["api"]}, "runner-src": {"components": ["smoke"]}}


def test_portable_template_and_request_contract(template, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Contract validation must not execute lifecycle commands")

    monkeypatch.setattr(subprocess, "run", forbidden)
    assert contract.validate_template(template) is template
    selected, parameters = contract.validate_request(template, request(), repositories())
    assert selected == [
        {"id": "api", "repository": "service-src", "requested_ref": "main"},
        {"id": "smoke", "repository": "runner-src", "requested_ref": "main"},
    ]
    assert parameters == {"timeout_seconds": 30, "verbose": False}
    alternate = request()
    alternate["components"] = {"api": {"repository": "alternate", "ref": "refs/tags/v1"}}
    alternate["parameters"] = {"verbose": True}
    chosen, params = contract.validate_request(
        template, alternate, repositories() | {"alternate": {"components": ["api"]}})
    assert chosen[0] == {"id": "api", "repository": "alternate", "requested_ref": "refs/tags/v1"}
    assert params == {"timeout_seconds": 30, "verbose": True}


def test_real_template_demo_compatibility():
    # Verbatim fixture from portable examples/template-demo/template.json, schema v1.
    template = json.loads((FIXTURES / "stand-template-demo.template.json").read_text(encoding="utf-8"))
    assert contract.validate_template(template)["id"] == "template-demo"
    selected, params = contract.validate_request(
        template, request(), {"demo": {"components": ["api", "smoke"]}})
    assert selected == [{"id": cid, "repository": "demo", "requested_ref": "test"}
                        for cid in ("api", "smoke")]
    assert params == {}
    assert template["lifecycle"]["migrate"][0]["argv"][-1] == "migrate"


@pytest.mark.parametrize("mutate", [
    lambda t: t.update(schema_version=True),
    lambda t: t.update(schema_version=2),
    lambda t: t.update(unexpected="value"),
    lambda t: t["components"][0].update(id="CON"),
    lambda t: t["components"][1].update(id="API"),
    lambda t: t["components"][0].update(dependencies=["smoke"]),
    lambda t: t["components"][0].update(dependencies=["missing"]),
    lambda t: t["compose"].update(path="../compose.yaml"),
    lambda t: t["compose"].update(path="a/.git/config"),
    lambda t: t["profiles"].pop("laptop"),
    lambda t: t["profiles"]["laptop"].update(services=["other"]),
    lambda t: t["profiles"]["laptop"]["resources"].update(cpus=float("inf")),
    lambda t: t["profiles"]["laptop"]["resources"].update(memory_mb=True),
    lambda t: t["lifecycle"].update(build=[]),
    lambda t: t["lifecycle"]["build"][0].update(argv="echo hello"),
    lambda t: t["lifecycle"]["build"][0].update(shell=True),
    lambda t: t["parameters"]["verbose"].update(type=[]),
    lambda t: t["parameters"]["verbose"].update(default="yes"),
    lambda t: t["parameters"]["verbose"].update(enum=[]),
    lambda t: t.update(secret_refs={"password": "value"}),
    lambda t: t.update(secret_refs=["https://example.invalid/value"]),
    lambda t: t["output_bindings"]["baseUrl"].update(port=True),
    lambda t: t["output_bindings"]["baseUrl"].update(service="other"),
    lambda t: t["output_bindings"]["baseUrl"].update(path="//example.invalid/"),
    lambda t: t["smoke"].update(env="../local"),
    lambda t: t["smoke"].update(suite="suites/../../file"),
])
def test_invalid_template_contract(template, mutate):
    mutate(template)
    with pytest.raises(contract.StandError):
        contract.validate_template(template)


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(schema_version=True),
    lambda r: r.update(extra=1),
    lambda r: r.update(profile="missing"),
    lambda r: r.update(components=[]),
    lambda r: r["components"].update(missing={}),
    lambda r: r["components"].update(api={"extra": 1}),
    lambda r: r["components"].update(api={"repository": "runner-src"}),
    lambda r: r["components"].update(api={"repository": "missing"}),
    lambda r: r["components"].update(api={"ref": "HEAD~1"}),
    lambda r: r["parameters"].update(verbose=1),
    lambda r: r["parameters"].update(timeout_seconds=True),
    lambda r: r["parameters"].update(extra="value"),
])
def test_invalid_request_contract(template, mutate):
    value = request()
    mutate(value)
    with pytest.raises(contract.StandError):
        contract.validate_request(template, value, repositories())


def test_parameters_required_enum_and_no_secret_value_scanning(template):
    template["parameters"]["mode"] = {"type": "string", "enum": ["password=example"]}
    contract.validate_template(template)
    with pytest.raises(contract.StandError, match="required"):
        contract.validate_request(template, request(), repositories())
    value = request()
    value["parameters"] = {"mode": "password=example"}
    assert contract.validate_request(template, value, repositories())[1]["mode"] == "password=example"
    value["parameters"]["mode"] = "other"
    with pytest.raises(contract.StandError, match="enum"):
        contract.validate_request(template, value, repositories())


def test_canonical_hash_is_stable_and_rejects_nonfinite(template):
    reordered = dict(reversed(list(template.items())))
    assert contract.canonical({"b": "я", "a": 1}) == '{"a":1,"b":"я"}'.encode()
    assert contract.template_hash(template) == contract.template_hash(reordered)
    changed = copy.deepcopy(template)
    changed["components"][0]["default_ref"] = "feature"
    assert contract.template_hash(changed) != contract.template_hash(template)
    with pytest.raises(ValueError):
        contract.canonical({"value": float("nan")})


@pytest.fixture
def configure(tmp_path, monkeypatch):
    def install(data):
        filename = tmp_path / "private-host-catalogue.json"
        filename.write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setenv("PORTAL_STAND_HOSTS", str(filename))
        return filename
    return install


def hosts_data(path):
    return {"schema_version": 1, "hosts": [{
        "id": "build-one", "title": "Build one", "port_range": [20000, 20999],
        "repositories": {"PROJECT": {"source": str(path)}},
    }]}


def test_absent_hosts_empty_and_public_projection(configure, monkeypatch, tmp_path):
    monkeypatch.delenv("PORTAL_STAND_HOSTS", raising=False)
    assert catalog.load_hosts() == {}
    configure(hosts_data(tmp_path / "not-yet-mounted"))
    # Listing hosts does not require the clone to be mounted or valid yet.
    host = catalog.load_hosts()["build-one"]
    assert host.repositories == {"PROJECT": {"source": tmp_path / "not-yet-mounted"}}
    assert catalog.public_host(host) == {
        "id": "build-one", "title": "Build one", "port_range": [20000, 20999]}
    assert str(tmp_path) not in json.dumps(catalog.public_host(host))


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(schema_version=True),
    lambda d: d.update(schema_version=2),
    lambda d: d.update(extra="private-value"),
    lambda d: d.update(hosts={}),
    lambda d: d["hosts"].append(copy.deepcopy(d["hosts"][0])),
    lambda d: d["hosts"][0].update(agent="private-value"),
    lambda d: d["hosts"][0].update(id="--unsafe"),
    lambda d: d["hosts"][0].update(title=""),
    lambda d: d["hosts"][0].update(port_range=[True, 20000]),
    lambda d: d["hosts"][0].update(port_range=[0, 20000]),
    lambda d: d["hosts"][0].update(port_range=[20000, 65536]),
    lambda d: d["hosts"][0].update(port_range=[20001, 20000]),
    lambda d: d["hosts"][0].update(port_range=[20000]),
    lambda d: d["hosts"][0].update(repositories=[]),
    lambda d: d["hosts"][0].update(repositories={"bad/project": {}}),
    lambda d: d["hosts"][0].update(repositories={"x" * 33: {}}),
    lambda d: d["hosts"][0].update(repositories={"   ": {}}),
    lambda d: d["hosts"][0].update(repositories={"PROJECT": []}),
    lambda d: d["hosts"][0].update(repositories={"PROJECT": {"--bad": "/private"}}),
    lambda d: d["hosts"][0].update(repositories={"PROJECT": {"source": "relative/private"}}),
    lambda d: d["hosts"][0].update(repositories={"PROJECT": {"source": "https://example.invalid/repo"}}),
    lambda d: d["hosts"][0].update(repositories={"PROJECT": {"source": {"path": "private"}}}),
])
def test_invalid_hosts_are_sanitized(configure, tmp_path, mutate):
    value = hosts_data(tmp_path / "private-value")
    mutate(value)
    configure(value)
    with pytest.raises(catalog.CatalogError) as exc:
        catalog.load_hosts()
    assert str(exc.value) == "Некорректный каталог хостов стендов"


@pytest.mark.parametrize("raw", [
    '{"schema_version":1,"hosts":[],"hosts":[]}',
    '{"schema_version":1,"hosts":[{"id":"private","id":"other"}]}',
    '{"schema_version":1,"hosts":[],"value":NaN}',
    '{"schema_version":1,"hosts":[],"value":Infinity}',
    'private invalid JSON',
])
def test_invalid_json_is_sanitized(configure, raw):
    filename = configure({})
    filename.write_text(raw, encoding="utf-8")
    with pytest.raises(catalog.CatalogError, match="^Некорректный каталог хостов стендов$"):
        catalog.load_hosts()


def test_missing_catalogue_file_sanitized(monkeypatch, tmp_path):
    monkeypatch.setenv("PORTAL_STAND_HOSTS", str(tmp_path / "private-file-does-not-exist"))
    with pytest.raises(catalog.CatalogError, match="^Некорректный каталог хостов стендов$"):
        catalog.load_hosts()


def git(path, *args):
    done = subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=Stand Test", "-c",
         "user.email=stand@example.invalid", "-c", "core.autocrlf=false", *args],
        capture_output=True, shell=False, timeout=20, check=True,
    )
    return done.stdout.decode("utf-8").strip()


@pytest.fixture
def clone(tmp_path, configure):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q", "--initial-branch=main")
    (source / "content.txt").write_text("initial\n", encoding="utf-8")
    git(source, "add", ".")
    git(source, "commit", "-qm", "initial")
    target = tmp_path / "clone"
    git(tmp_path, "clone", "-q", "--no-local", str(source), str(target))
    git(target, "branch", "feature/orders")
    git(target, "tag", "v1")
    git(target, "tag", "-a", "annotated", "-m", "annotated")
    git(target, "branch", "collision")
    git(target, "tag", "collision")
    configure(hosts_data(target))
    return target, catalog.load_hosts()["build-one"], git(target, "rev-parse", "HEAD")


def test_local_refs_and_full_sha_resolution(clone):
    path, host, sha = clone
    assert catalog.list_refs(host, "PROJECT", "source") == [
        {"name": "collision", "kind": "branch"},
        {"name": "feature/orders", "kind": "branch"},
        {"name": "main", "kind": "branch"},
        {"name": "annotated", "kind": "tag"},
        {"name": "collision", "kind": "tag"},
        {"name": "v1", "kind": "tag"},
    ]
    for ref in ("main", "feature/orders", "v1", "annotated", "refs/heads/collision",
                "refs/tags/collision", sha, sha.upper()):
        assert catalog.resolve_ref(host, "PROJECT", "source", ref) == sha
    tag_object = git(path, "rev-parse", "refs/tags/annotated")
    with pytest.raises(catalog.CatalogError):
        catalog.resolve_ref(host, "PROJECT", "source", tag_object)
    with pytest.raises(catalog.CatalogError, match="неоднозначна"):
        catalog.resolve_ref(host, "PROJECT", "source", "collision")
    with pytest.raises(catalog.CatalogError):
        catalog.resolve_ref(host, "PROJECT", "source", "a" * 40)


def test_existing_cyrillic_project_mapping_and_refs(clone, configure):
    path, _, sha = clone
    project = "Учебный проект: API"
    data = hosts_data(path)
    data["hosts"][0]["repositories"] = {project: {"source": str(path)}}
    configure(data)
    host = catalog.load_hosts()["build-one"]
    assert host.repositories[project]["source"] == path
    assert {"name": "main", "kind": "branch"} in catalog.list_refs(host, project, "source")
    assert catalog.resolve_ref(host, project, "source", "main") == sha


@pytest.mark.parametrize("ref", [
    "HEAD", "FETCH_HEAD", "@", "--help", "-main", "main~1", "main^", "main:path",
    "main@{1}", "main..feature", "refs/remotes/origin/main", "a//b", "a.lock", "a/.hidden",
    "a b", "a\nb", "a\\b", "a?b", "a[b", "https://example.invalid/repo", "", [],
])
def test_invalid_ref_cannot_reach_git(clone, monkeypatch, ref):
    _, host, _ = clone
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid ref reached Git")
    monkeypatch.setattr(catalog, "_git", forbidden)
    with pytest.raises(catalog.CatalogError, match="^Некорректная ссылка на ветку, тег или коммит репозитория стенда$"):
        catalog.resolve_ref(host, "PROJECT", "source", ref)


def test_ref_lookup_does_not_modify_dirty_clone(clone):
    path, host, sha = clone
    (path / "content.txt").write_text("dirty\n", encoding="utf-8")
    (path / "staged.txt").write_text("staged\n", encoding="utf-8")
    git(path, "add", "staged.txt")
    (path / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    observed = {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if p.is_file()}
    before_status = git(path, "status", "--porcelain=v1")
    assert catalog.resolve_ref(host, "PROJECT", "source", "main") == sha
    assert catalog.list_refs(host, "PROJECT", "source")
    assert git(path, "status", "--porcelain=v1") == before_status
    assert {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if p.is_file()} == observed


def test_git_environment_isolated(clone, monkeypatch):
    _, host, sha = clone
    monkeypatch.setenv("GIT_DIR", "/private/not-a-repository")
    monkeypatch.setenv("GIT_WORK_TREE", "/private/wrong-tree")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "alias.rev-parse")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "!exit 99")
    assert catalog.resolve_ref(host, "PROJECT", "source", "main") == sha


@pytest.mark.parametrize("project,repository", [("OTHER", "source"), ("PROJECT", "missing"), ("--bad", "source")])
def test_unmapped_repository_has_no_fallback(clone, project, repository):
    _, host, _ = clone
    with pytest.raises(catalog.CatalogError, match="^Репозиторий стенда недоступен$"):
        catalog.list_refs(host, project, repository)
    with pytest.raises(catalog.CatalogError, match="^Репозиторий стенда недоступен$"):
        catalog.resolve_ref(host, project, repository, "main")


@pytest.mark.parametrize("kind", ["missing", "directory", "subdirectory", "bare"])
def test_repository_requires_existing_nonbare_clone_root(configure, tmp_path, kind):
    path = tmp_path / "private-repository"
    if kind != "missing":
        path.mkdir()
    if kind == "subdirectory":
        git(path, "init", "-q")
        path = path / "child"
        path.mkdir()
    if kind == "bare":
        git(path, "init", "-q", "--bare")
    configure(hosts_data(path))
    host = catalog.load_hosts()["build-one"]
    with pytest.raises(catalog.CatalogError, match="^Репозиторий стенда недоступен$"):
        catalog.list_refs(host, "PROJECT", "source")


def test_git_timeout_errors_and_env_are_sanitized(tmp_path, monkeypatch):
    def timeout(argv, **kwargs):
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == 20
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0"
        assert kwargs["env"]["GIT_NO_REPLACE_OBJECTS"] == "1"
        assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
        assert kwargs["env"]["GIT_CONFIG_SYSTEM"] == os.devnull
        assert "GIT_SSH_COMMAND" not in kwargs["env"]
        raise subprocess.TimeoutExpired(argv, 20, stderr=b"private path credential-value")
    monkeypatch.setenv("GIT_SSH_COMMAND", "private credential command")
    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(catalog.CatalogError, match="^Репозиторий стенда недоступен$"):
        catalog._git(tmp_path, "rev-parse", "--show-toplevel")
