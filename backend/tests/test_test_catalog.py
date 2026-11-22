from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from portal.test_catalog import Repository, newman_ids, pytest_ids


def test_newman_catalog_config_and_suite_restrictions(tmp_path):
    (tmp_path / "autotest.yaml").write_text("collections: custom\n", encoding="utf-8")
    root = tmp_path / "custom" / "Orders" / "api"
    root.mkdir(parents=True)
    for name, desc, code in [
        ("P01", "**Метаданные теста:** приоритет `P1` · профили `Smoke` · метки `sg-auth`", ""),
        ("P010", "**Метаданные теста:** приоритет `P1` · профили `Smoke` · метки `sg-auth`", ""),
        ("unsafe", "", 'needFlag("allowDestructive")'),
        ("utility", "**Метаданные теста:** вид `utility`", ""),
    ]:
        (root / f"{name}.request.yaml").write_text(yaml.safe_dump({
            "description": desc, "scripts": [{"type": "beforeRequest", "code": code}]}), encoding="utf-8")
    repo = Repository(tmp_path, "main")
    suite = {"collection": "Orders", "items": ["api/"], "only": "security", "exclude": ["api/P010"], "profile": "Smoke"}
    assert newman_ids(repo, suite) == ["api/P01"]
    assert newman_ids(repo, {**suite, "exclude_groups": ["sg-auth"]}) == []
    with pytest.raises(ValueError):
        newman_ids(repo, {"collection": "../../outside"})


def test_static_python_catalog_rejects_escape(tmp_path):
    repo = Repository(tmp_path, "main")
    with pytest.raises(ValueError):
        pytest_ids(repo, {"paths": ["../outside"]})
    with pytest.raises(ValueError):
        repo.read("../outside")


def test_git_catalog_reads_requested_ref_in_one_batch(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    paths = {"custom/тест.request.yaml": "method: GET\n", "suites/x.yaml": "collection: Заказы\n"}
    calls = []
    def fake_git(command, **options):
        calls.append(command)
        assert options["timeout"] == 20
        assert f"safe.directory={tmp_path.resolve()}" in command
        if "rev-parse" in command:
            assert command[-1] == "refs/remotes/origin/feature^{commit}"
            return SimpleNamespace(returncode=0, stdout="a" * 40 + "\n")
        if "ls-tree" in command:
            return SimpleNamespace(stdout="".join(f"100644 blob {'b' * 40}\t{p}\0" for p in paths))
        assert command[-2:] == ["cat-file", "--batch"]
        assert options["input"].decode().splitlines() == ["a" * 40 + ":" + p for p in sorted(paths)]
        data = b""
        for name in sorted(paths):
            content = paths[name].encode()
            data += f"{'b' * 40} blob {len(content)}\n".encode() + content + b"\n"
        return SimpleNamespace(stdout=data)
    monkeypatch.setattr("portal.test_catalog.subprocess.run", fake_git)
    repo = Repository(tmp_path, "feature")
    assert repo.yaml("suites/x.yaml") == {"collection": "Заказы"}
    assert repo.yaml("custom/тест.request.yaml") == {"method": "GET"}
    assert sum("cat-file" in command for command in calls) == 1
