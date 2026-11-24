"""Только искусственные тесты: без Docker, сети, устройств и тестируемых систем."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml


@pytest.fixture
def agent_module(monkeypatch):
    monkeypatch.setenv("PORTAL_URL", "http://unused.invalid")
    monkeypatch.setenv("AGENT_TOKEN", "unit-test")
    monkeypatch.delenv("AGENT_MANAGED_STAND_HOST", raising=False)
    monkeypatch.delenv("AGENT_MANAGED_STAND_LOOPBACK_HOST", raising=False)
    spec = importlib.util.spec_from_file_location("selection_agent", Path(__file__).parents[2] / "agent" / "agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("host", ["", "local-host"])
def test_managed_claim_is_opt_in(agent_module, monkeypatch, host):
    monkeypatch.setenv("AGENT_CAPABILITIES", "newman,fixture")
    monkeypatch.setenv("AGENT_MANAGED_STAND_HOST", host)
    spec = importlib.util.spec_from_file_location("managed_claim_agent", agent_module.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def claim(method, path, body):
        assert (method, path) == ("POST", "/api/agent/claim")
        assert body == ({"capabilities": ["newman", "fixture", "managed-newman-v1"], "host_id": host}
                        if host else {"capabilities": ["newman", "fixture"]})
        raise RuntimeError("stop fixture")
    monkeypatch.setattr(module, "api", claim)
    with pytest.raises(RuntimeError, match="stop fixture"):
        module.main()


def managed_spec():
    return {"project": "FIXTURE", "requested_by": "fixture", "suite": "suite.yaml", "env": "stand-1-api",
            "stand": "stand-1", "destructive": False, "tags": ["smoke"], "groups": [], "tests": [],
            "stand_target": {"host_id": "local-host", "binding": "apiUrl", "build_id": 7,
                             "bindings": {"apiUrl": "http://127.0.0.1:3102/api/v1?q=1",
                                          "auth.url": "https://auth.invalid:8443/oauth"}}}


@pytest.mark.parametrize("selected", [None, ["P01"]])
def test_managed_wrapper_overlay_precedence_and_gate(agent_module, monkeypatch, tmp_path, selected):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "autotest.yaml").write_text("{}", encoding="utf-8")
    local = tmp_path / ".local"
    local.mkdir()
    (local / "stand-1-api.yaml").write_text(
        "secretKey: fixture-private-value\nbaseUrl: http://wrong.invalid\napiUrl: http://wrong.invalid\n"
        "auth.url: http://wrong.invalid\nnumber: 42\n", encoding="utf-8")
    (local / "other-stand.yaml").write_text("unrelated: forbidden\n", encoding="utf-8")
    monkeypatch.setattr(agent_module, "MANAGED_STAND_HOST", "local-host")
    variables = agent_module.managed_newman_variables(managed_spec())
    fake = SimpleNamespace(suite_items=lambda suite, scan, metas, volume: list(suite["items"]),
                           yaml=yaml, Path=Path, project=SimpleNamespace(VERSION="unit"))
    def main():
        assert sys.argv[1:] == ["suite.yaml", "--env", "stand-1-api", "--destructive", "no"]
        assert fake.load_env("stand-1-api") == {
            "secretKey": "fixture-private-value", "number": "42", "safeFailedLogins": "1", **variables,
        }
        scan = {"P01": [], "P02": [], "gate": []}
        assert fake.suite_items({"items": ["P01", "P02"]}, scan, {}, {}) == (["P01"] if selected else ["P01", "P02"])
        assert fake.suite_items({"items": ["gate"]}, scan, {}, {}) == ["gate"]
        return 0
    fake.main = main
    monkeypatch.setitem(sys.modules, "run", fake)
    monkeypatch.setattr(sys, "argv", ["-c", json.dumps({"test_ids": selected, "variables": variables}),
                                      "suite.yaml", "--env", "stand-1-api", "--destructive", "no"])
    with pytest.raises(SystemExit) as error:
        exec(agent_module.NEWMAN_SELECTION, {})
    assert error.value.code == 0
    assert not (tmp_path / "postman").exists()


@pytest.mark.parametrize("overlay", ["[]", "secretKey: [private-value]", "secretKey: {private-key: private-value}", "a: ["])
def test_managed_invalid_overlay_never_exposes_values(agent_module, monkeypatch, tmp_path, overlay):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "autotest.yaml").write_text("{}", encoding="utf-8")
    (tmp_path / ".local").mkdir()
    (tmp_path / ".local" / "stand-1-api.yaml").write_text(overlay, encoding="utf-8")
    fake = SimpleNamespace(suite_items=lambda *args: [], yaml=yaml, Path=Path,
                           project=SimpleNamespace(VERSION="unit"))
    fake.main = lambda: fake.load_env("stand-1-api")
    monkeypatch.setitem(sys.modules, "run", fake)
    monkeypatch.setattr(sys, "argv", ["-c", json.dumps({"test_ids": None, "variables": {}})])
    with pytest.raises(SystemExit) as error:
        exec(agent_module.NEWMAN_SELECTION, {})
    assert str(error.value) == "локальное окружение managed-прогона не прочитано"


@pytest.mark.parametrize("loopback", ["127.0.0.1", "127.0.0.2", "localhost", "[::1]"])
def test_managed_loopback_translation_preserves_url(agent_module, monkeypatch, loopback):
    monkeypatch.setattr(agent_module, "MANAGED_STAND_HOST", "local-host")
    monkeypatch.setattr(agent_module, "MANAGED_STAND_LOOPBACK_HOST", "host.docker.internal")
    spec = managed_spec()
    spec["stand_target"]["bindings"]["apiUrl"] = f"https://{loopback}:3102/api/v1/?q=1"
    values = agent_module.managed_newman_variables(spec)
    assert values == {"apiUrl": "https://host.docker.internal:3102/api/v1/?q=1",
                      "baseUrl": "https://host.docker.internal:3102/api/v1?q=1",
                      "auth.url": "https://auth.invalid:8443/oauth"}


@pytest.mark.parametrize("url", ["ftp://localhost/x", "http://", "http://u:p@localhost", "http://localhost:0",
                                  "http://localhost:65536", "http://localhost:bad", "http://localhost:",
                                  "http://bad_host/x", "http://localhost/a b", "http://localhost\\evil/x"])
def test_managed_rejects_invalid_url(agent_module, monkeypatch, url):
    monkeypatch.setattr(agent_module, "MANAGED_STAND_HOST", "local-host")
    spec = managed_spec()
    spec["stand_target"]["bindings"]["apiUrl"] = url
    with pytest.raises(ValueError, match="URL"):
        agent_module.managed_newman_variables(spec)


@pytest.mark.parametrize("selected", [None, ["P01"]])
def test_managed_docker_argv_retains_bridge_shape_without_overlay(agent_module, monkeypatch, tmp_path, selected):
    monkeypatch.setattr(agent_module, "MANAGED_STAND_HOST", "local-host")
    monkeypatch.setattr(agent_module, "library_configuration", lambda: ({"AUTOTEST_LIBRARY_API_URL": "https://library.invalid"}, []))
    monkeypatch.setattr(agent_module, "runtime_environment", lambda *args: {})
    monkeypatch.setattr(os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(os, "getgid", lambda: 1000, raising=False)
    log_messages, calls = [], []
    monkeypatch.setattr(agent_module, "stream", lambda log, command, **kwargs: calls.append(command) or 0)
    spec = managed_spec()
    spec["test_ids"] = selected
    (tmp_path / ".local").mkdir()
    (tmp_path / ".local" / "stand-1-api.yaml").write_text("secretKey: private-value\n", encoding="utf-8")
    assert agent_module.run_tests(SimpleNamespace(write=log_messages.append), 9, spec, tmp_path, tmp_path) == 0
    command = calls[0]
    argv = command[command.index(agent_module.IMAGE) + 1:]
    assert argv[:3] == ["python3", "-c", agent_module.NEWMAN_SELECTION]
    payload = json.loads(argv[3])
    assert payload == {"test_ids": selected, "variables": agent_module.managed_newman_variables(spec)}
    assert argv[4:7] == ["suite.yaml", "--env", "stand-1-api"]
    assert argv[-2:] == ["--secrets", "mask"]
    assert "--tag" in argv and "smoke" in argv
    assert "secretKey" not in json.dumps(command) and "private-value" not in json.dumps(command)
    assert "apiUrl" not in "".join(log_messages)


@pytest.mark.parametrize("spec", [{"test_ids": []}, {"test_ids": ["x"], "tests": ["x"]}, {"test_ids": [""]}])
def test_agent_rejects_invalid_selection(agent_module, spec):
    with pytest.raises(ValueError):
        agent_module.validate_selection(spec)


@pytest.mark.parametrize(("selected", "allowed", "destructive", "expected"), [
    (["P01"], ["P01", "P010"], False, 0),
    (["missing"], ["P01"], False, None),
    (["P01"], [], False, None),
    (["P01"], ["P01"], True, None),
])
def test_newman_exact_adapter_preserves_gate(agent_module, monkeypatch, selected, allowed, destructive, expected):
    calls = []
    fake = SimpleNamespace(suite_items=lambda suite, scan, metas, volume: list(suite["items"]),
                           yaml=SimpleNamespace(safe_load=lambda text: {}),
                           Path=lambda path: SimpleNamespace(read_text=lambda **kwargs: ""),
                           project=SimpleNamespace(VERSION="unit"))
    def main():
        assert sys.argv[1:] == ["suite.yaml", "--destructive", "no"]
        scan = {"P01": ["allowDestructive"] if destructive else [], "P010": [], "gate": []}
        calls.append(fake.suite_items({"items": allowed}, scan, {}, {}))
        calls.append(fake.suite_items({"items": ["gate"]}, scan, {}, {}))
        return 0
    fake.main = main
    monkeypatch.setitem(sys.modules, "run", fake)
    monkeypatch.setattr(sys, "argv", ["-c", json.dumps(selected), "suite.yaml", "--destructive", "no"])
    with pytest.raises(SystemExit) as error:
        exec(agent_module.NEWMAN_SELECTION, {})
    if expected is None:
        assert error.value.code != 0 and not calls
    else:
        assert error.value.code == expected and calls == [["P01"], ["gate"]]


@pytest.mark.parametrize(("selected", "marker", "success"), [
    (["test_dummy.py::test_one"], None, True),
    (["test_dummy.py::test_missing"], None, False),
    (["test_dummy.py::test_one"], "other", False),
    ([], None, False),
])
def test_pytest_exact_after_markers(agent_module, tmp_path, selected, marker, success):
    (tmp_path / "portal_exact_selection.py").write_text(agent_module.PYTEST_SELECTION, encoding="utf-8")
    (tmp_path / "test_dummy.py").write_text(
        'from pathlib import Path\nimport pytest\n'
        '@pytest.mark.parametrize("value", [1, 2])\n'
        'def test_one(value):\n    Path(f"ran-{value}").write_text("yes")\n'
        'def test_one_more():\n    raise AssertionError("must not run")\n', encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(tmp_path), "PORTAL_TEST_IDS": json.dumps(selected), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    command = [sys.executable, "-m", "pytest", "-q", "-p", "portal_exact_selection", "test_dummy.py"]
    if marker:
        command += ["-m", marker]
    result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True)
    assert (result.returncode == 0) is success, result.stdout + result.stderr
    assert len(list(tmp_path.glob("ran-*"))) == (2 if success else 0)
