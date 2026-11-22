"""Статический каталог: YAML и AST, без импорта кода репозитория тестов."""
import ast
import fnmatch
import re
import subprocess
from pathlib import Path, PurePosixPath

import yaml


class Repository:
    def __init__(self, root: Path, ref: str, *, batch=True):
        self.root = root.resolve()
        self.batch = batch
        self.commit = None
        self.cache = None
        if (root / ".git").exists():
            for name in (f"refs/remotes/origin/{ref}", f"refs/heads/{ref}", f"refs/tags/{ref}"):
                result = self.git("rev-parse", "--verify", f"{name}^{{commit}}", check=False)
                if result.returncode == 0:
                    self.commit = result.stdout.strip()
                    break
            if not self.commit:
                raise ValueError("ветка каталога отсутствует в локальном зеркале")
            listing = self.git("ls-tree", "-rz", "--full-tree", self.commit).stdout
            self.files = {line.split("\t", 1)[1] for line in listing.split("\0")
                          if line.startswith("100")}
        else:
            if ref != "main":
                raise ValueError("каталог без git доступен только для main")
            self.files = {p.relative_to(root).as_posix() for p in root.rglob("*")
                          if p.is_file() and p.resolve().is_relative_to(self.root)}

    def git(self, *args, check=True, binary=False, input=None):
        return subprocess.run(["git", "-c", f"safe.directory={self.root}", "-c", "core.quotePath=false", "-C", str(self.root), *args],
                              check=check, capture_output=True, text=not binary,
                              encoding=None if binary else "utf-8", input=input, timeout=20)

    def read(self, path):
        path = str(path)
        if path not in self.files or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
            raise ValueError(f"файл отсутствует в репозитории: {path}")
        if not self.commit:
            return (self.root / path).read_text(encoding="utf-8")
        if not self.batch:
            return self.git("cat-file", "blob", f"{self.commit}:{path}").stdout
        if self.cache is None:
            # Один процесс на каталог, а не git show для каждого из тысяч запросов.
            paths = sorted(p for p in self.files if p.endswith((".yaml", ".py")) and not any(c in p for c in "\r\n"))
            payload = "".join(f"{self.commit}:{p}\n" for p in paths).encode("utf-8")
            output = self.git("cat-file", "--batch", binary=True, input=payload).stdout
            self.cache = {}
            offset = 0
            for name in paths:
                end = output.index(b"\n", offset)
                size = int(output[offset:end].rsplit(b" ", 1)[1])
                offset = end + 1
                self.cache[name] = output[offset:offset + size].decode("utf-8")
                offset += size + 1
        if path not in self.cache:
            raise ValueError("неподдерживаемый файл каталога")
        return self.cache[path]

    def yaml(self, path):
        return yaml.safe_load(self.read(path)) or {}


def metadata(description):
    match = re.search(r"^\*\*Метаданные теста:\*\*(.*)$", description or "", re.M)
    text = match[1] if match else ""
    def field(name):
        m = re.search(name + r" (.*?)(?: · |$)", text)
        return re.findall(r"`([^`]+)`", m[1]) if m else []
    kind = re.search(r"вид `(\w+)`", text)
    return {"kind": kind[1] if kind else "test", "profiles": field("профили"),
            "tags": field("метки"), "priority": (field("приоритет") or [None])[0]}


def newman_ids(repo, suite):
    config = repo.yaml("autotest.yaml") if "autotest.yaml" in repo.files else {}
    root = str(PurePosixPath(config.get("collections", "postman/collections")) / suite["collection"])
    if PurePosixPath(root).is_absolute() or ".." in PurePosixPath(root).parts:
        raise ValueError("каталог коллекций должен находиться внутри репозитория")
    out = []
    for path in sorted(repo.files):
        if not path.startswith(root + "/") or not path.endswith(".request.yaml"):
            continue
        ident = path[len(root) + 1:-len(".request.yaml")]
        data = repo.yaml(path)
        meta = metadata(data.get("description"))
        flags = " ".join(s.get("code", "") for s in data.get("scripts") or []
                         if s.get("type") in ("beforeRequest", "http:beforeRequest"))
        destructive = bool(re.search(r'needFlag\(\s*"allowDestructive"', flags))
        groups = {t for t in meta["tags"] if t.startswith("sg-")}
        items = suite.get("items")
        if items not in (None, "all") and not any(ident == x or (x.endswith("/") and ident.startswith(x)) for x in items):
            continue
        if any(ident == x or ident.startswith(x.rstrip("/") + "/") for x in suite.get("exclude") or []):
            continue
        only = suite.get("only")
        if only not in (None, "destructive", "security", "functional"):
            raise ValueError("неизвестное ограничение only")
        if (only == "destructive" and not destructive) or (only in ("security", "functional") and
                (destructive or bool(groups) != (only == "security"))):
            continue
        if meta["kind"] != "test" or groups & set(suite.get("exclude_groups") or []):
            continue
        if suite.get("profile") and suite["profile"] not in meta["profiles"]:
            continue
        if suite.get("max_priority") and (meta["priority"] is None or meta["priority"] > suite["max_priority"]):
            continue
        if suite.get("tags") and not set(suite["tags"]) & set(meta["tags"]):
            continue
        out.append(ident)
    return out


def pytest_ids(repo, suite):
    out = set()
    for target in suite.get("paths") or ["."]:
        target = str(target).replace("\\", "/").rstrip("/")
        if "::" in target or ".." in PurePosixPath(target).parts or PurePosixPath(target).is_absolute():
            raise ValueError("для статического каталога paths должны быть путями внутри репозитория")
        for path in sorted(repo.files):
            if not (target == "." or path == target or path.startswith(target + "/")) or not path.endswith(".py"):
                continue
            if path != target and not any(fnmatch.fnmatch(PurePosixPath(path).name, pattern) for pattern in ("test_*.py", "*_test.py")):
                continue
            tree = ast.parse(repo.read(path), filename=path)
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                    out.add(f"{path}::{node.name}")
                if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                    for method in node.body:
                        if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and method.name.startswith("test"):
                            out.add(f"{path}::{node.name}::{method.name}")
    return sorted(out)


def _suite(repo, suite_path):
    if not suite_path.startswith("suites/") or not suite_path.endswith(".yaml"):
        raise ValueError("недопустимый набор")
    suite = repo.yaml(suite_path)
    if not isinstance(suite, dict):
        raise ValueError("набор должен быть объектом YAML")
    return suite


def suite_metadata(root, suite_path, ref):
    """Проверка набора в выбранном ref без discovery/AST полного каталога тестов."""
    repo = Repository(root, ref, batch=False)
    suite = _suite(repo, suite_path)
    return {"destructive": suite.get("only") == "destructive"}


def test_catalog(root, runner, suite_path, ref):
    repo = Repository(root, ref)
    suite = _suite(repo, suite_path)
    ids = pytest_ids(repo, suite) if runner == "pytest" else newman_ids(repo, suite)
    return {"runner": runner, "ref": ref, "commit": repo.commit, "gate": suite.get("gate"),
            "destructive": suite.get("only") == "destructive",
            "items": [{"id": ident, "label": ident} for ident in ids]}
