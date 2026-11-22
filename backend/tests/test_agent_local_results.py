"""Переносимый агент: только искусственные локальные Git-репозитории, без сети и Docker."""
import importlib.util
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def agent(monkeypatch, tmp_path):
    monkeypatch.setenv("PORTAL_URL", "http://unused.invalid")
    monkeypatch.setenv("AGENT_TOKEN", "fixture-agent")
    monkeypatch.setenv("AGENT_GIT_MODE", "local-results")
    monkeypatch.setenv("AGENT_GIT_TIMEOUT", "0.5")
    monkeypatch.delenv("AGENT_TESTS_OVERLAY_DIR", raising=False)
    monkeypatch.setenv("AUTOTEST_LIBRARY_API_URL", "https://library.invalid")
    monkeypatch.setenv("AUTOTEST_LIBRARY_API_KEY", "fixture-library")
    monkeypatch.delenv("AUTOTEST_LIBRARY_API_KEY_FILE", raising=False)
    spec = importlib.util.spec_from_file_location("local_results_agent", Path(__file__).parents[2] / "agent" / "agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.WORK = tmp_path / "agent"
    module.api = Mock(return_value={"cancel": False})
    return module


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                            encoding="utf-8", check=True)
    return result.stdout.strip()


@pytest.fixture
def repos(tmp_path):
    remotes = {}
    for name in ("tests", "results"):
        root = tmp_path / name
        root.mkdir()
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.name", "Fixture")
        git(root, "config", "user.email", "fixture@localhost")
        (root / ".gitignore").write_text(".local/\n**/allure-report.html\n**/raw/\n", encoding="utf-8")
        (root / "README.md").write_text(name, encoding="utf-8")
        git(root, "add", ".")
        git(root, "commit", "-qm", "fixture")
        remote = tmp_path / f"{name}.git"
        git(tmp_path, "clone", "--bare", str(root), str(remote))
        remotes[name] = remote
    return remotes


@pytest.fixture
def job(repos):
    return {"project": "FIXTURE", "ref": "main", "tests_url": str(repos["tests"]),
            "results_url": str(repos["results"]), "requested_by": "fixture", "runner": "newman"}


def run_file(results, number):
    run_id = f"FIXTURE-20261118-12000{number}-smoke"
    run = results / "runs" / "2026" / "11" / run_id
    run.mkdir(parents=True)
    (run / "run.yaml").write_text(f"id: {run_id}\n", encoding="utf-8")
    (run / "allure-report.html").write_text("fixture allure", encoding="utf-8")
    (results / "runs" / "index.jsonl").write_text(f"{number}\n", encoding="utf-8")
    history = results / "history" / "Fixture"
    history.mkdir(parents=True, exist_ok=True)
    with (history / "allure-history.jsonl").open("a", encoding="utf-8") as output:
        output.write(f"{number}\n")
    return run_id, run


def test_configuration_defaults_remain_remote(agent, monkeypatch):
    monkeypatch.delenv("AGENT_GIT_MODE")
    monkeypatch.delenv("AGENT_GIT_TIMEOUT")
    spec = importlib.util.spec_from_file_location("default_results_agent", agent.__file__)
    default = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(default)
    assert default.GIT_MODE == "remote" and default.GIT_TIMEOUT == 15


@pytest.mark.parametrize("failure", [RuntimeError("offline"), subprocess.TimeoutExpired("git", 0.5)])
def test_source_fetch_failure_uses_cache_and_reports_commit(agent, job, monkeypatch, failure):
    tests, _, _ = agent.prepare(Mock(), job)
    original = agent.git
    def offline(log, *args, **kwargs):
        if args[0] == "fetch":
            assert kwargs["timeout"] == 0.5
            raise failure
        return original(log, *args, **kwargs)
    monkeypatch.setattr(agent, "git", offline)
    log = Mock()
    agent.prepare(log, job)
    messages = "".join(call.args[0] for call in log.write.call_args_list)
    assert "кэш" in messages and git(tests, "rev-parse", "HEAD") in messages
    assert git(tests, "remote", "get-url", "origin") == job["tests_url"]


def test_cached_source_missing_ref_still_fails(agent, job, monkeypatch):
    agent.prepare(Mock(), job)
    original = agent.git
    def offline(log, *args, **kwargs):
        if args[0] == "fetch":
            raise RuntimeError("offline")
        return original(log, *args, **kwargs)
    monkeypatch.setattr(agent, "git", offline)
    with pytest.raises(RuntimeError, match="checkout"):
        agent.prepare(Mock(), {**job, "ref": "missing"})


def test_source_clone_failure_without_cache_is_not_hidden(agent, job, monkeypatch):
    monkeypatch.setattr(agent, "git", Mock(side_effect=subprocess.TimeoutExpired("git", 0.5)))
    with pytest.raises(subprocess.TimeoutExpired):
        agent.prepare(Mock(), job)
    assert agent.git.call_args.kwargs["timeout"] == 0.5


def test_two_local_runs_survive_prepare_and_never_touch_results_remote(agent, job, repos, monkeypatch):
    original = agent.git
    commands = []
    def observe(log, *args, **kwargs):
        if kwargs.get("cwd", Path()).name == "results":
            commands.append(args)
            assert not {"fetch", "pull", "push", "reset", "clean"}.intersection(args)
        return original(log, *args, **kwargs)
    monkeypatch.setattr(agent, "git", observe)
    remote_before = git(repos["results"], "rev-parse", "main")
    kept = []
    for number in (1, 2):
        _, results, _ = agent.prepare(Mock(), job)
        run_id, run = run_file(results, number)
        kept.append(run)
        assert agent.publish_results(Mock(), number, results, "fixture") == run_id
        assert all((path / "run.yaml").exists() for path in kept)
        assert all((path / "allure-report.html").exists() for path in kept)
    assert git(results, "status", "--porcelain") == ""
    assert git(results, "rev-list", "--count", "origin/main..main") == "2"
    assert (results / "history" / "Fixture" / "allure-history.jsonl").read_text(encoding="utf-8") == "1\n2\n"
    assert git(repos["results"], "rev-parse", "main") == remote_before
    assert git(results, "remote", "get-url", "origin") == job["results_url"]
    assert agent.api.call_count == 2
    assert all(call.args[0] == "PUT" and "/artifact/" in call.args[1] for call in agent.api.call_args_list)


def test_local_results_preserve_uncommitted_runs_but_reject_unrelated_changes(agent, job):
    _, results, _ = agent.prepare(Mock(), job)
    _, run = run_file(results, 1)
    agent.prepare(Mock(), job)
    assert (run / "run.yaml").exists()
    unexpected = results / "README.md"
    unexpected.write_text("manual change", encoding="utf-8")
    with pytest.raises(RuntimeError, match="вне runs"):
        agent.prepare(Mock(), job)
    with pytest.raises(RuntimeError, match="вне runs"):
        agent.publish_results(Mock(), 1, results, "fixture")
    assert unexpected.read_text(encoding="utf-8") == "manual change"


def test_default_mode_fetch_failure_does_not_fall_back(agent, job, monkeypatch):
    tests, _, _ = agent.prepare(Mock(), job)
    monkeypatch.setattr(agent, "GIT_MODE", "remote")
    monkeypatch.setattr(agent, "git", Mock(side_effect=RuntimeError("offline")))
    with pytest.raises(RuntimeError, match="offline"):
        agent.clone_or_update(Mock(), job["tests_url"], tests)
    assert agent.git.call_args.kwargs["timeout"] is None


def test_available_source_is_updated_without_changing_origin(agent, job, repos, tmp_path):
    tests, _, _ = agent.prepare(Mock(), job)
    source = tmp_path / "tests"
    (source / "new.txt").write_text("updated", encoding="utf-8")
    git(source, "add", "new.txt")
    git(source, "commit", "-qm", "new source")
    git(source, "push", str(repos["tests"]), "main")
    agent.prepare(Mock(), job)
    assert (tests / "new.txt").read_text(encoding="utf-8") == "updated"
    assert git(tests, "remote", "get-url", "origin") == job["tests_url"]


def test_default_mode_resets_results_and_pushes(agent, job, repos, monkeypatch):
    monkeypatch.setattr(agent, "GIT_MODE", "remote")
    _, results, _ = agent.prepare(Mock(), job)
    stale = results / "stale.txt"
    stale.write_text("stale", encoding="utf-8")
    agent.prepare(Mock(), job)
    assert not stale.exists()
    run_id, _ = run_file(results, 1)
    assert agent.publish_results(Mock(), 1, results, "fixture") == run_id
    assert git(repos["results"], "rev-parse", "main") == git(results, "rev-parse", "HEAD")


def test_overlay_is_ignored_reapplied_and_does_not_change_source(agent, job, repos, tmp_path, monkeypatch):
    overlay = tmp_path / "overlay"
    local = overlay / "FIXTURE" / ".local"
    local.mkdir(parents=True)
    (local / "Fixture.yaml").write_text("baseUrl: http://127.0.0.1:3102\n", encoding="utf-8")
    (local / "ignored.txt").write_text("not copied", encoding="utf-8")
    monkeypatch.setattr(agent, "TESTS_OVERLAY_DIR", str(overlay))
    for _ in range(2):
        tests, _, _ = agent.prepare(Mock(), job)
        assert (tests / ".local" / "Fixture.yaml").read_text(encoding="utf-8").startswith("baseUrl:")
        assert not (tests / ".local" / "ignored.txt").exists()
        assert git(tests, "status", "--porcelain") == ""
    assert not git(repos["tests"], "ls-tree", "-r", "HEAD", ".local")


@pytest.mark.parametrize("project", ["../escape", "..\\escape", "/escape", "a/../../b"])
def test_overlay_rejects_path_traversal(agent, tmp_path, project, monkeypatch):
    monkeypatch.setattr(agent, "TESTS_OVERLAY_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="проект"):
        agent.apply_tests_overlay(Mock(), project, tmp_path / "tests")
    monkeypatch.setattr(agent, "clone_or_update", Mock())
    with pytest.raises(ValueError, match="проект"):
        agent.prepare(Mock(), {"project": project})
    agent.clone_or_update.assert_not_called()


@pytest.mark.parametrize("link_part", ["project", "local", "file", "destination"])
def test_overlay_rejects_symlinks(agent, job, tmp_path, monkeypatch, link_part):
    tests, _, _ = agent.prepare(Mock(), job)
    overlay = tmp_path / "overlay"
    local = overlay / "FIXTURE" / ".local"
    local.mkdir(parents=True)
    (local / "Fixture.yaml").write_text("baseUrl: http://unused.invalid\n", encoding="utf-8")
    target = tmp_path / "link-target"
    target.mkdir()
    try:
        if link_part == "project":
            local.rename(target / ".local")
            local.parent.rmdir()
            local.parent.symlink_to(target, target_is_directory=True)
        elif link_part == "local":
            local.rename(target / ".local")
            local.symlink_to(target / ".local", target_is_directory=True)
        elif link_part == "file":
            file = local / "Fixture.yaml"
            file.rename(target / "Fixture.yaml")
            file.symlink_to(target / "Fixture.yaml")
        else:
            (tests / ".local").symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")
    monkeypatch.setattr(agent, "TESTS_OVERLAY_DIR", str(overlay))
    with pytest.raises(ValueError, match="ссылки"):
        agent.apply_tests_overlay(Mock(), "FIXTURE", tests)


def test_overlay_cannot_overwrite_tracked_yaml(agent, job, tmp_path, monkeypatch):
    tests, _, _ = agent.prepare(Mock(), job)
    local = tests / ".local"
    local.mkdir()
    (local / "Fixture.yaml").write_text("tracked", encoding="utf-8")
    git(tests, "add", "-f", ".local/Fixture.yaml")
    overlay = tmp_path / "overlay" / "FIXTURE" / ".local"
    overlay.mkdir(parents=True)
    (overlay / "Fixture.yaml").write_text("override", encoding="utf-8")
    monkeypatch.setattr(agent, "TESTS_OVERLAY_DIR", str(overlay.parents[1]))
    with pytest.raises((RuntimeError, ValueError)):
        agent.apply_tests_overlay(Mock(), "FIXTURE", tests)
    assert (local / "Fixture.yaml").read_text(encoding="utf-8") == "tracked"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction")
@pytest.mark.parametrize("link_part", ["project", "local", "destination"])
def test_overlay_rejects_windows_junctions(agent, job, tmp_path, monkeypatch, link_part):
    import _winapi
    tests, _, _ = agent.prepare(Mock(), job)
    overlay = tmp_path / "overlay"
    project = overlay / "FIXTURE"
    local = project / ".local"
    local.mkdir(parents=True)
    target = tmp_path / "junction-target"
    target.mkdir()
    if link_part == "project":
        local.rmdir()
        project.rmdir()
        _winapi.CreateJunction(str(target), str(project))
    elif link_part == "local":
        local.rmdir()
        _winapi.CreateJunction(str(target), str(local))
    else:
        _winapi.CreateJunction(str(target), str(tests / ".local"))
    monkeypatch.setattr(agent, "TESTS_OVERLAY_DIR", str(overlay))
    with pytest.raises(ValueError, match="ссылки"):
        agent.apply_tests_overlay(Mock(), "FIXTURE", tests)


def test_execute_local_result_still_uploads_and_runs_after_job(agent, job, monkeypatch):
    agent.AFTER_JOB = "fixture sync"
    def run(log, job_id, spec, tests, results, library):
        run_file(results, 1)
        return 0
    monkeypatch.setattr(agent, "run_tests", run)
    original = agent.subprocess.run
    after = []
    def subprocess_run(command, **kwargs):
        if command == "fixture sync":
            after.append(command)
            return subprocess.CompletedProcess(command, 0)
        return original(command, **kwargs)
    monkeypatch.setattr(agent.subprocess, "run", subprocess_run)
    agent.execute({"id": 1, "kind": "run", "spec": job})
    assert after == ["fixture sync"]
    finish = [call for call in agent.api.call_args_list if call.args[1].endswith("/finish")]
    assert finish[0].args[2]["status"] == "done"
    assert any("/artifact/" in call.args[1] for call in agent.api.call_args_list)
