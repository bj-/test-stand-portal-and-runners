#!/usr/bin/env python3
"""Агент портала: забирает задачи и выполняет их. Только стандартная библиотека Python, git и Docker на машине.

Настройки - переменные окружения:
  PORTAL_URL, AGENT_TOKEN       адрес портала и токен с ролью agent
  AGENT_CAPABILITIES            возможности через запятую, по умолчанию newman; stand-host - действия со стендами
  AGENT_WORK                    рабочий каталог: клоны тестов и результатов
  AGENT_ENGINE                  каталог движка test-engine
  AUTOTEST_LIBRARY_API_URL       HTTPS API библиотеки
  AUTOTEST_LIBRARY_API_KEY_FILE  закрытый файл ключа (либо AUTOTEST_LIBRARY_API_KEY в окружении)
  AGENT_LIBRARY_URL             репозиторий библиотеки только для прежнего локального режима
  RUNNER_IMAGE                  образ исполнителя
  GITLAB_TOKEN                  токен git; в аргументы процессов не попадает
  GITHUB_TOKEN                  токен чтения GitHub; отдельно от GITLAB_TOKEN
  AFTER_JOB                     команда после задачи, например обновление индекса портала
  AGENT_ENV_DIR                 каталог <ПРОЕКТ>.env (KEY=VALUE): окружение прогонов pytest - устройство, адрес Appium,
                                учётки стенда; в портал не уходит

Прогоны проекта с runner: pytest (например, Appium с телефоном на этой машине) идут не в образе, а здесь же:
venv из requirements.txt тестов, pytest с --junitxml, итог пишется в репозиторий результатов в формате движка.
"""
import hashlib
import ipaddress
import json
import math
import os
import queue
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

URL = os.environ["PORTAL_URL"].rstrip("/")
TOKEN = os.environ["AGENT_TOKEN"]
CAPABILITIES = os.environ.get("AGENT_CAPABILITIES", "newman").split(",")
MANAGED_STAND_HOST = os.environ.get("AGENT_MANAGED_STAND_HOST", "").strip()
MANAGED_STAND_LOOPBACK_HOST = os.environ.get("AGENT_MANAGED_STAND_LOOPBACK_HOST", "").strip()
if MANAGED_STAND_HOST and "managed-newman-v1" not in CAPABILITIES:
    CAPABILITIES.append("managed-newman-v1")
WORK = Path(os.environ.get("AGENT_WORK", "/srv/data/portal/agent"))
ENGINE = os.environ.get("AGENT_ENGINE", "/opt/test-engine")
LIBRARY_URL = os.environ.get("AGENT_LIBRARY_URL", "")
LIBRARY_API_NAMES = ("AUTOTEST_LIBRARY_API_URL", "AUTOTEST_LIBRARY_API_KEY_FILE", "AUTOTEST_LIBRARY_API_KEY")
IMAGE = os.environ.get("RUNNER_IMAGE", "test-portal-runner")
AFTER_JOB = os.environ.get("AFTER_JOB")
ENV_DIR = Path(os.environ.get("AGENT_ENV_DIR", "/srv/data/portal/agent-env"))
PUBLIC_URL = os.environ.get("PORTAL_PUBLIC_URL", "")  # адрес портала для людей - попадает в манифест прогона
GIT_MODE = os.environ.get("AGENT_GIT_MODE", "remote")
if GIT_MODE not in ("remote", "local-results"):
    raise ValueError("AGENT_GIT_MODE: допустимы remote и local-results")
GIT_TIMEOUT = float(os.environ.get("AGENT_GIT_TIMEOUT", "15"))
if not math.isfinite(GIT_TIMEOUT) or GIT_TIMEOUT <= 0:
    raise ValueError("AGENT_GIT_TIMEOUT должен быть положительным конечным числом")
TESTS_OVERLAY_DIR = os.environ.get("AGENT_TESTS_OVERLAY_DIR", "")
POLL = 5
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")  # цвета и управление курсором: в логе портала это мусор
FLUSH = 2
GIT_AUTH = ["-c", "credential.helper=", "-c",
            'credential.helper=!f() { [ "$1" = get ] || return 0; '
            'host=; while IFS= read -r line && [ -n "$line" ]; do '
            'case "$line" in host=*) host=${line#host=} ;; esac; done; '
            'case "$host" in github.com) echo username=x-access-token; echo "password=$GITHUB_TOKEN" ;; '
            '*) echo username=oauth2; echo "password=$GITLAB_TOKEN" ;; esac; }; f']


def api(method: str, path: str, body=None, raw: bytes | None = None, timeout: int = 120):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    request = urllib.request.Request(URL + path, data=data, method=method, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/octet-stream" if raw is not None else "application/json",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read()
    return json.loads(payload) if payload else None


class Log:
    """Копит вывод и раз в несколько секунд отправляет в портал; ответ говорит, не отменена ли задача."""

    def __init__(self, job_id: int, stop: threading.Event | None = None):
        self.job_id = job_id
        self.buffer: list[str] = []
        self.sent = 0.0
        self.cancel = False
        self.stop = stop

    def write(self, text: str) -> None:
        self.buffer.append(text)
        if time.time() - self.sent >= FLUSH:
            self.flush()

    def flush(self) -> None:
        text, self.buffer = "".join(self.buffer), []
        self.sent = time.time()
        self.cancel = api("POST", f"/api/agent/jobs/{self.job_id}/log", {"text": text}, timeout=15)["cancel"] or self.cancel
        if self.stop and self.stop.is_set():
            self.cancel = True


def git(log: Log, *args: str, cwd: Path | None = None, timeout: float | None = None) -> str:
    done = subprocess.run(["git", *GIT_AUTH, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)
    if done.returncode:
        log.write(done.stderr)
        raise RuntimeError(f"git {args[0]}: код {done.returncode}")
    return done.stdout


def clone_or_update(log: Log, url: str, path: Path) -> None:
    timeout = GIT_TIMEOUT if GIT_MODE == "local-results" else None
    if (path / ".git").is_dir():
        try:
            git(log, "fetch", "-q", "--prune", "origin", cwd=path, timeout=timeout)
        except (RuntimeError, subprocess.TimeoutExpired):
            if GIT_MODE != "local-results":
                raise
            commit = git(log, "rev-parse", "--verify", "HEAD^{commit}", cwd=path).strip()
            log.write(f"== Git недоступен: используем локальный кэш {path.name}, HEAD {commit}\n")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        git(log, "clone", "-q", url, str(path), timeout=timeout)


def guard_local_results(log: Log, results: Path) -> None:
    """Не подмешиваем посторонние правки в локальный коммит; незавершённые runs не удаляем."""
    changed = git(log, "status", "--porcelain=v1", "-z", "--no-renames", "--untracked-files=all", cwd=results)
    for entry in changed.split("\0"):
        if not entry:
            continue
        name = entry[3:]
        if not re.fullmatch(r"(?:runs/(?:INDEX\.md|index\.jsonl|\d{4}/\d{2}/[^/]+/.+)"
                            r"|history/[^/]+/allure-history\.jsonl)", name):
            raise RuntimeError("локальные результаты содержат изменения вне runs/истории Allure; исправьте рабочую копию")


def apply_tests_overlay(log: Log, project: str, tests: Path, managed_environment: str | None = None) -> None:
    """Копируем только плоские YAML overrides в ignored .local приватного checkout агента."""
    if not TESTS_OVERLAY_DIR:
        return
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", project) or ".." in project:
        raise ValueError("недопустимый проект для overlay")
    root = Path(TESTS_OVERLAY_DIR).absolute()
    source = root / project / ".local"
    destination = tests / ".local"

    def reject_link(path: Path) -> None:
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError("overlay не допускает символические ссылки")

    for path in (*root.parents, root, root / project, source, tests, destination):
        reject_link(path)
    if not source.exists():
        return
    if not source.is_dir():
        raise ValueError("overlay .local должен быть каталогом")
    files = []
    for path in sorted(source.iterdir()):
        reject_link(path)
        if path.suffix != ".yaml":
            continue
        if managed_environment is not None:
            if path.stem != managed_environment:
                continue
        elif re.match(r"stand-[a-f0-9]{32}--", path.stem, re.IGNORECASE):
            continue
        if not path.is_file():
            raise ValueError("overlay допускает только файлы .local/*.yaml")
        target = destination / path.name
        reject_link(target)
        # Штатный механизм engine читает .local/<environment>.yaml. Он не должен менять tracked файлы.
        git(log, "check-ignore", "-q", "--", f".local/{path.name}", cwd=tests)
        if git(log, "ls-files", "--", f".local/{path.name}", cwd=tests).strip():
            raise ValueError("overlay не может менять файл из Git")
        files.append((path, target))
    if files:
        destination.mkdir(exist_ok=True)
        for source_file, target in files:
            shutil.copyfile(source_file, target)
            target.chmod(0o600)
        log.write(f"== локальные окружения: наложено файлов {len(files)}\n")


def library_configuration():
    """API имеет приоритет; ошибочная настройка не разрешает клон библиотеки."""
    if not any(key in os.environ for key in LIBRARY_API_NAMES):
        if not LIBRARY_URL:
            raise RuntimeError("не настроен Library API")
        return {}, []
    url = os.environ.get("AUTOTEST_LIBRARY_API_URL", "")
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        raise RuntimeError("Library API: неверный HTTPS URL") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise RuntimeError("Library API: требуется HTTPS URL без учётных данных")
    env = {"AUTOTEST_LIBRARY_API_URL": url}
    key_file = os.environ.get("AUTOTEST_LIBRARY_API_KEY_FILE")
    if key_file:
        path = Path(key_file).resolve()
        if not path.is_file():
            raise RuntimeError("Library API: файл ключа недоступен")
        env["AUTOTEST_LIBRARY_API_KEY_FILE"] = "/run/secrets/library-api-key"
        return env, ["-v", f"{path}:/run/secrets/library-api-key:ro"]
    key = os.environ.get("AUTOTEST_LIBRARY_API_KEY", "").strip()
    if not key or any(c.isspace() for c in key):
        raise RuntimeError("Library API: ключ не настроен")
    env["AUTOTEST_LIBRARY_API_KEY"] = key
    return env, []


def prepare(log: Log, spec: dict) -> tuple[Path, Path, Path]:
    if (GIT_MODE == "local-results" or TESTS_OVERLAY_DIR) and (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", spec["project"]) or ".." in spec["project"]):
        raise ValueError("недопустимый проект для локальной рабочей копии")
    base = WORK / spec["project"]
    tests, results, library = base / "tests", base / "results", WORK / "Test-Library"
    log.write(f"== подготовка: тесты {spec['ref']}\n")
    clone_or_update(log, spec["tests_url"], tests)
    git(log, "checkout", "-q", "-f", "--detach", f"origin/{spec['ref']}", cwd=tests)
    git(log, "clean", "-qfdx", cwd=tests)
    git(log, "config", "user.name", spec["requested_by"], cwd=tests)
    if GIT_MODE == "local-results":
        commit = git(log, "rev-parse", "HEAD", cwd=tests).strip()
        log.write(f"== тесты из локальной рабочей копии: {spec['ref']} @ {commit}\n")
    apply_tests_overlay(log, spec["project"], tests, spec["env"] if "stand_target" in spec else None)
    if GIT_MODE == "local-results":
        if not (results / ".git").is_dir():
            clone_or_update(log, spec["results_url"], results)
        guard_local_results(log, results)
        git(log, "checkout", "-q", "main", cwd=results)
    else:
        clone_or_update(log, spec["results_url"], results)
        git(log, "checkout", "-q", "-f", "main", cwd=results)
        git(log, "reset", "-q", "--hard", "origin/main", cwd=results)
        git(log, "clean", "-qfdx", cwd=results)
    if spec.get("runner", "newman") == "newman":
        api_env, _ = library_configuration()
        if not api_env:  # явная настройка прежней установки
            clone_or_update(log, LIBRARY_URL, library)
            git(log, "reset", "-q", "--hard", "origin/HEAD", cwd=library)
    return tests, results, library


NEWMAN_SELECTION = r'''
import atexit, json, os, re, ssl, sys, tempfile
sys.path.insert(0, "/engine/engine")
import run
cfg = run.yaml.safe_load(run.Path("autotest.yaml").read_text(encoding="utf-8")) or {}
want = str((cfg.get("engine") or {}).get("version", run.project.VERSION))
if want != run.project.VERSION:
    print(f"версия движка {run.project.VERSION}, проект рассчитан на {want}", file=sys.stderr)
payload = json.loads(sys.argv.pop(1))
selected = payload.get("test_ids") if isinstance(payload, dict) else payload
if isinstance(payload, dict):
    variables = payload["variables"]
    def managed_ca(certificate):
        if (not isinstance(certificate, str) or len(certificate) > 65536 or not re.fullmatch(
                r"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\r\n]+\s+-----END CERTIFICATE-----",
                certificate.strip())):
            raise ValueError()
        certificate = certificate.strip()
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=certificate)
        system = ssl.get_default_verify_paths().cafile
        system_roots = run.Path(system).read_bytes() if system else b""
        temp = tempfile.TemporaryDirectory(prefix="portal-stand-ca-")
        atexit.register(temp.cleanup)
        bundle = run.Path(temp.name) / "ca.pem"
        bundle.write_bytes(system_roots + b"\n" + certificate.encode("ascii") + b"\n")
        os.environ["SSL_CERT_FILE"] = str(bundle)
        os.environ["NODE_EXTRA_CA_CERTS"] = str(bundle)

    def managed_env(name):
        # Не читаем статические профили других стендов. Секреты остаются в .local,
        # а подтверждённые адреса всегда имеют приоритет над локальными overrides.
        try:
            local = run.Path(".local") / f"{name}.yaml"
            overlay = run.yaml.safe_load(local.read_text(encoding="utf-8")) if local.exists() else {}
            overlay = {} if overlay is None else overlay
            if not isinstance(overlay, dict) or any(
                not isinstance(k, str) or not k or not isinstance(v, (str, int, float, bool, type(None)))
                for k, v in overlay.items()
            ):
                raise ValueError()
            if "__portal_ca_pem" in overlay:
                managed_ca(overlay.pop("__portal_ca_pem"))
            values = {k: v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, allow_nan=False)
                      for k, v in overlay.items()}
        except Exception:
            raise SystemExit("локальное окружение managed-прогона не прочитано") from None
        values.setdefault("safeFailedLogins", "1")
        values.update(variables)
        return values
    run.load_env = managed_env
original = run.suite_items
first = True
def exact(suite, scan, metas, volume):
    global first
    allowed = original(suite, scan, metas, volume)
    if not first:
        return allowed
    first = False
    if not selected or len(set(selected)) != len(selected) or not set(selected) <= set(allowed) & set(scan):
        raise SystemExit("выбранные ID отсутствуют в наборе после применения ограничений; запуск отменён")
    if "no" == sys.argv[sys.argv.index("--destructive") + 1] and (suite.get("only") == "destructive" or any("allowDestructive" in scan.get(i, []) for i in selected)):
        raise SystemExit("выбран разрушающий тест без разрешения; запуск отменён")
    return [i for i in allowed if i in selected]
if selected is not None:
    run.suite_items = exact
sys.exit(run.main())
'''


PYTEST_SELECTION = r'''
import json, os, pytest
SELECTED = json.loads(os.environ["PORTAL_TEST_IDS"])
def matches(nodeid, ident):
    return nodeid == ident or nodeid.startswith(ident + "[")
def validate(items):
    if not SELECTED or any(not any(matches(item.nodeid, ident) for item in items) for ident in SELECTED):
        raise pytest.UsageError("выбранные ID отсутствуют после collection/markers; запуск отменён")
@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    validate(items)
    removed = [item for item in items if not any(matches(item.nodeid, ident) for ident in SELECTED)]
    items[:] = [item for item in items if item not in removed]
    config.hook.pytest_deselected(items=removed)
def pytest_collection_finish(session):
    validate(session.items)
    if any(not any(matches(item.nodeid, ident) for ident in SELECTED) for item in session.items):
        raise pytest.UsageError("collection изменила точный выбор; запуск отменён")
'''


def validate_selection(spec):
    if spec.get("test_ids") is not None:
        ids = spec["test_ids"]
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i for i in ids) or spec.get("tests"):
            raise ValueError("недопустимый точный выбор тестов")


def managed_newman_variables(spec: dict) -> dict:
    """Только URL из подтверждённой сборки; никакого fallback на статический стенд."""
    target = spec["stand_target"]
    if (not MANAGED_STAND_HOST or not isinstance(target, dict)
            or target.get("host_id") != MANAGED_STAND_HOST):
        raise ValueError("managed-прогон назначен другому хосту")
    environment = spec.get("env")
    if (not isinstance(environment, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", environment)
            or ".." in environment):
        raise ValueError("недопустимое окружение managed-прогона")
    bindings, binding = target.get("bindings"), target.get("binding")
    if (not isinstance(bindings, dict) or not bindings or not isinstance(binding, str) or binding not in bindings
            or any(not isinstance(key, str) or not key for key in bindings)):
        raise ValueError("неполные адреса managed-прогона")

    def hostname(value):
        try:
            return ipaddress.ip_address(value)
        except ValueError:
            if (len(value) > 253 or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                                             for label in value.rstrip(".").split("."))):
                raise ValueError("неверный host managed-прогона") from None
            return None

    replacement = MANAGED_STAND_LOOPBACK_HOST
    if replacement:
        hostname(replacement)
    values = {}
    for key, url in bindings.items():
        try:
            if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 for c in url) or "\\" in url:
                raise ValueError()
            parsed = urllib.parse.urlsplit(url)
            host, port = parsed.hostname, parsed.port
            if (parsed.scheme not in ("http", "https") or not host or parsed.username is not None
                    or parsed.password is not None or parsed.fragment or parsed.netloc.endswith(":")
                    or (port is not None and not 1 <= port <= 65535)):
                raise ValueError()
            address = hostname(host)
        except (ValueError, TypeError):
            raise ValueError("неверный URL managed-прогона") from None
        if replacement and (host.rstrip(".").lower() == "localhost" or (address and address.is_loopback)):
            netloc = f"[{replacement}]" if ":" in replacement else replacement
            if port is not None:
                netloc += f":{port}"
            url = urllib.parse.urlunsplit(parsed._replace(netloc=netloc))
        values[key] = url
    base_url = urllib.parse.urlsplit(values[binding])
    values["baseUrl"] = urllib.parse.urlunsplit(base_url._replace(path=base_url.path.rstrip("/")))
    return values


def run_tests(log: Log, job_id: int, spec: dict, tests: Path, results: Path, library: Path) -> int:
    validate_selection(spec)
    managed = "stand_target" in spec
    variables = managed_newman_variables(spec) if managed else None
    library_env, library_mounts = library_configuration()
    name = f"portal-job-{job_id}"
    command = [
        "docker", "run", "--rm", "--name", name, "--network", "host", "--hostname", "portal-runner",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", "/etc/localtime:/etc/localtime:ro",  # RUN-ID и время в отчётах - по часам машины
        "-e", "HOME=/tmp", "-e", "USERNAME=portal", "-e", "PYTHONUNBUFFERED=1", "-e", "PYTHONIOENCODING=utf-8",
        "-e", "AUTOTEST_ENGINE=/engine", "-e", "AUTOTEST_RESULTS=/work/results",
        "-e", f"AUTOTEST_AGENT=portal:{spec['requested_by']}",
        # Прогон из портала (движок 0.11.1+): trigger.type portal, кто запустил и номер задачи.
        "-e", "AUTOTEST_TRIGGER=portal", "-e", f"AUTOTEST_PORTAL_USER={spec['requested_by']}",
        "-e", f"AUTOTEST_PORTAL_JOB={job_id}", "-e", f"AUTOTEST_PORTAL_URL={PUBLIC_URL}",
        "-v", f"{tests}:/work/tests", "-v", f"{results}:/work/results",
        "-v", f"{ENGINE}:/engine:ro",
        "-w", "/work/tests", IMAGE,
        "python3", "autotest.py", "run", spec["suite"], "--env", spec["env"],
        "--destructive", "yes" if spec["destructive"] else "no",
    ]
    runtime = runtime_environment(job_id, spec)
    runtime.update(library_env)
    if not library_env:
        library_mounts = ["-e", "AUTOTEST_LIBRARY=/library", "-v", f"{library}:/library:ro"]
    # Значения, особенно bearer, передаются через окружение docker CLI, не argv и не лог.
    image_index = command.index(IMAGE)
    command[image_index:image_index] = [part for key in runtime for part in ("-e", key)]
    image_index = command.index(IMAGE)
    command[image_index:image_index] = library_mounts
    for tag in spec["tags"]:
        command += ["--tag", tag]
    for group in spec["groups"]:
        command += ["--group", group]
    for test in spec.get("tests") or []:
        command.append(f"--test={test}")  # через =: подстрока с минусом в начале не станет ключом
    if managed or spec.get("test_ids") is not None:
        index = command.index(IMAGE) + 1
        payload = {"test_ids": spec.get("test_ids"), "variables": variables} if managed else spec["test_ids"]
        command[index:index + 3] = ["python3", "-c", NEWMAN_SELECTION, json.dumps(payload, ensure_ascii=False)]
    if managed:
        command += ["--secrets", "mask"]
    # Что развёрнуто на стенде - портал добавляет при выдаче задачи; иначе в прогоне build: unknown.
    product = spec.get("product") or {}
    if product.get("build"):
        command += ["--build", product["build"]]
    for module, commit in (product.get("commits") or {}).items():
        command += ["--product-commit", f"{module}={commit}"]
    log.write("== managed-прогон Newman; gate набора сохраняется, секреты скрыты\n" if managed else
              "== точный выбор: " + json.dumps(spec["test_ids"], ensure_ascii=False) + "; gate набора сохраняется\n"
              if spec.get("test_ids") is not None else "== прогон: " + " ".join(command[command.index(IMAGE) + 1:]) + "\n")

    return stream(log, command, kill=lambda _: subprocess.run(["docker", "kill", name], capture_output=True),
                  env={**os.environ, **runtime})


def stream(log: Log, command: list[str], kill, **popen) -> int:
    """Выполнить команду, отдавая вывод в лог портала; при отмене задачи - kill(процесс)."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                               text=True, encoding="utf-8", errors="replace", **popen)
    lines: queue.Queue = queue.Queue()

    def read() -> None:
        for line in process.stdout:
            lines.put(line)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    killed = False
    while process.poll() is None or not lines.empty():
        try:
            log.write(ANSI.sub("", lines.get(timeout=FLUSH)))
        except queue.Empty:
            log.flush()
        if log.cancel and not killed:
            kill(process)
            killed = True
    reader.join(timeout=5)
    log.flush()
    return process.returncode


def publish_results(log: Log, job_id: int, results: Path, requested_by: str) -> str | None:
    if GIT_MODE == "local-results":
        guard_local_results(log, results)
    changed = git(log, "status", "--porcelain", "--untracked-files=all", cwd=results)
    found = re.search(r"runs/\d{4}/\d{2}/([^/\s\"]+)/", changed)
    if not found:
        return None
    run_id = found.group(1)
    git(log, "add", "-A", cwd=results)
    git(log, "-c", "user.name=Test Portal", "-c", "user.email=portal@localhost", "commit", "-q", "-m",
        f"Прогон {run_id} (портал, {requested_by})", cwd=results)
    # В репозиторий пишут и люди: при гонке повторяем поверх свежего состояния.
    for attempt in range(0 if GIT_MODE == "local-results" else 5):
        try:
            git(log, "-c", "user.name=Test Portal", "-c", "user.email=portal@localhost", "pull", "-q", "--rebase", "origin", "main", cwd=results)
            git(log, "push", "-q", "origin", "HEAD:main", cwd=results)
            break
        except RuntimeError:
            if attempt == 4:
                raise
            time.sleep(3)
    log.write(f"== результаты записаны: {run_id}\n")
    if GIT_MODE == "local-results":
        log.write("== результаты только локально; отправка в remote отключена\n")

    allure = next(results.glob(f"runs/*/*/{run_id}/allure-report.html"), None)
    if allure:
        # Allure - не в git (как у движка): только артефакт портала.
        api("PUT", f"/api/agent/jobs/{job_id}/artifact/{run_id}/{allure.name}", raw=allure.read_bytes())
    return run_id


# ---------------- прогоны pytest ----------------

VERDICTS = {"passed": "✅", "failed": "❌", "error": "⛔", "skipped": "⏭"}


def read_env_file(path: Path) -> dict:
    values = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("'\"")
    return values


def runtime_environment(job_id: int, spec: dict) -> dict[str, str]:
    values = {
        "TEST_DATA_PARENT_JOB_ID": str(job_id), "TEST_DATA_PROJECT": spec["project"],
        "TEST_DATA_STAND": spec.get("stand") or "", "TEST_DATA_PORTAL_URL": URL,
    }
    # Только специально заданный токен проекта. Агентский bearer никогда не попадает в тесты.
    configured = read_env_file(ENV_DIR / f"{spec['project']}.env").get("TEST_DATA_API_TOKEN")
    if configured:
        values["TEST_DATA_API_TOKEN"] = configured
    return values


def pytest_venv(log: Log, project: str, tests: Path) -> Path:
    """venv проекта; зависимости ставятся заново, только когда меняется requirements.txt."""
    venv = WORK / project / "venv"
    python = venv / "bin" / "python"
    requirements = tests / "requirements.txt"
    digest = hashlib.sha256(requirements.read_bytes()).hexdigest() if requirements.exists() else "none"
    marker = venv / ".requirements.sha256"
    if python.exists() and marker.exists() and marker.read_text() == digest:
        return python
    log.write("== окружение: venv и зависимости тестов\n")
    if not python.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True)
    packages = ["pytest", "allure-pytest", "pyyaml"] + (["-r", str(requirements)] if requirements.exists() else [])
    code = stream(log, [str(python), "-m", "pip", "install", "-q", "--disable-pip-version-check", *packages],
                  kill=lambda process: process.kill())
    if code:
        raise RuntimeError(f"pip install: код {code}")
    marker.write_text(digest)
    return python


def pytest_command(python: Path, suite: dict, spec: dict, junit: Path, allure: Path) -> list[str]:
    """Набор - suites/*.yaml репозитория тестов: paths (что запускать), markers (-m), args (прочие ключи pytest)."""
    command = [str(python), "-m", "pytest", *[str(p) for p in suite.get("paths") or []],
               f"--junitxml={junit}", f"--alluredir={allure}", "-o", "junit_family=xunit2", "-p", "no:cacheprovider",
               "-rA", "--color=no"]
    markers = [m for m in [suite.get("markers")] if m]
    if spec["tags"]:  # метки формы запуска - маркеры pytest: тест подходит, если у него есть любая из них
        markers.append(" or ".join(spec["tags"]))
    if markers:
        command += ["-m", " and ".join(f"({m})" for m in markers)]
    if spec.get("tests"):  # подстроки имени теста: -k
        command += ["-k", " or ".join(re.sub(r"[^\w\[\]-]+", " ", t).strip().replace(" ", " and ") or t
                                       for t in spec["tests"])]
    return command + [str(a) for a in suite.get("args") or []]


def parse_junit(junit: Path) -> list[dict]:
    executions = []
    if not junit.exists():
        return executions
    for case in ET.parse(junit).getroot().iter("testcase"):
        classname, name = case.get("classname", ""), case.get("name", "")
        verdict, message = "passed", ""
        for tag, kind in (("failure", "failed"), ("error", "error"), ("skipped", "skipped")):
            node = case.find(tag)
            if node is not None:
                verdict = kind
                message = (node.get("message") or "") + ("\n" + node.text if node.text and kind != "skipped" else "")
                break
        module = classname.replace(".", "/")
        executions.append({
            "path": f"{module}::{name}", "name": name, "method": None, "url": None, "code": None,
            "time_ms": round(float(case.get("time") or 0) * 1000), "verdict": verdict,
            "gate": False, "groups": [], "message": message.strip()[:20000],
        })
    return executions


def git_head(path: Path) -> tuple[str | None, bool]:
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=path, capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=path, capture_output=True, text=True).stdout.strip())
    return commit or None, dirty


def write_pytest_run(spec: dict, job_id: int, results: Path, tests: Path, executions: list[dict], started: datetime,
                     finished: datetime, env: dict, allure_html: Path | None) -> str:
    """Прогон в формате движка (runs/<ГГГГ>/<ММ>/<RUN-ID>/run.yaml, results.json, report.md) - его читает импорт портала."""
    suite_stem = Path(spec["suite"]).stem
    run_id = f"{spec['project']}-{started:%Y%m%d-%H%M%S}-{re.sub(r'[^A-Za-z0-9._-]', '-', suite_stem)}"
    run_dir = results / "runs" / f"{started:%Y}" / f"{started:%m}" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    counts = {k: sum(1 for e in executions if e["verdict"] == k) for k in VERDICTS}
    commit, dirty = git_head(tests)
    device = env.get("DEVICE_NAME") or env.get("DEVICE_UDID") or ""
    manifest = {
        "id": run_id, "project": spec["project"], "modules": [suite_stem], "kind": "pytest",
        "trigger": {"type": "portal", "user": spec["requested_by"], "job": job_id, "url": PUBLIC_URL or None},
        "who": f"portal:{spec['requested_by']}", "started": started.isoformat(timespec="seconds"),
        "finished": finished.isoformat(timespec="seconds"), "suite": spec["suite"], "gate": None, "gate_failed": False,
        "stand": {"environment": spec["env"], "url": env.get("STAND_URL"), "kind": "device" if device else None},
        "product": {"build": env.get("APP_BUILD") or "unknown", "device": device or None},
        "tests": {"repo": spec["tests_url"].rstrip("/").rsplit("/", 1)[-1].removesuffix(".git"), "commit": commit,
                  "dirty": dirty, "branch": spec["ref"], "tag": None},
        "engine": {"version": "pytest", "commit": None},
        "runner": os.uname().nodename,
        "summary": {"tests": len(executions), **counts, "checks": len(executions),
                    "checks_failed": counts["failed"] + counts["error"]},
    }
    # JSON - тоже YAML: агент обходится стандартной библиотекой.
    (run_dir / "run.yaml").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (run_dir / "results.json").write_text(json.dumps(
        {"started": manifest["started"], "completed": manifest["finished"], "executions": executions},
        ensure_ascii=False, indent=1), encoding="utf-8")

    lines = [f"# Прогон {run_id}", "", "| | |", "|---|---|",
             f"| Набор | `{spec['suite']}` |", f"| Окружение | {spec['env']} |",
             f"| Устройство | {device or '-'} |", f"| Сборка | {manifest['product']['build']} |",
             f"| Тесты | {spec['ref']} @ {commit or '?'} |", f"| Запустил | {spec['requested_by']} (задача {job_id}) |",
             f"| Итог | " + ", ".join(f"{VERDICTS[k]} {v}" for k, v in counts.items() if v) + " |", "",
             "## Тесты", ""]
    lines += [f"- {VERDICTS[e['verdict']]} `{e['path']}`" for e in executions]
    bad = [e for e in executions if e["verdict"] in ("failed", "error")]
    if bad:
        lines += ["", "## Падения", ""]
        for e in bad:
            lines += [f"### {VERDICTS[e['verdict']]} {e['name']}", "", f"`{e['path']}`", "", "```",
                      e["message"][:4000], "```", ""]
    (run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if allure_html and allure_html.exists():
        shutil.copy(allure_html, run_dir / "allure-report.html")
    return run_id


def allure_single_file(log: Log, allure: Path, out: Path, name: str) -> Path | None:
    """Allure-отчёт одним файлом (как у движка), если на машине есть allure 3.x; не обязателен."""
    cli = shutil.which("allure")
    if not cli or not any(allure.glob("*-result.json")):
        return None
    config = out / "allurerc.mjs"
    config.write_text(
        "export default {\n"
        f"  name: {json.dumps(name, ensure_ascii=False)},\n"
        f"  output: {json.dumps(str(out / 'report'))},\n"
        "  plugins: { awesome: { options: { singleFile: true, reportLanguage: \"ru\" } } },\n"
        "};\n", encoding="utf-8")
    done = subprocess.run([cli, "generate", str(allure), "--config", str(config)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    report = out / "report" / "index.html"
    if done.returncode or not report.exists():
        log.write("== allure: отчёт не собран\n" + done.stdout[-1000:] + done.stderr[-1000:])
        return None
    return report


def run_pytest(log: Log, job_id: int, spec: dict, tests: Path, results: Path) -> tuple[int, str | None]:
    validate_selection(spec)
    python = pytest_venv(log, spec["project"], tests)
    # YAML набора читает python venv (там есть pyyaml): у агента только стандартная библиотека.
    suite = json.loads(subprocess.run(
        [str(python), "-c", "import json, sys, yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1], encoding='utf-8')) or {}))",
         str(tests / spec["suite"])], check=True, capture_output=True, text=True).stdout)
    if spec.get("test_ids") is not None and suite.get("only") == "destructive" and not spec["destructive"]:
        raise ValueError("разрушающий набор без разрешения; запуск отменён")
    env = {k: v for k, v in os.environ.items() if k not in ("AGENT_TOKEN", "GITLAB_TOKEN", "TEST_DATA_API_TOKEN", *LIBRARY_API_NAMES)}
    project_env = read_env_file(ENV_DIR / f"{spec['project']}.env")
    env.update(project_env)
    env.pop("AGENT_TOKEN", None)
    env.pop("GITLAB_TOKEN", None)
    for key in LIBRARY_API_NAMES:
        env.pop(key, None)
    env.update(runtime_environment(job_id, spec))
    env.update({"AUTOTEST_ENV": spec["env"], "AUTOTEST_DESTRUCTIVE": "yes" if spec["destructive"] else "no",
                "AUTOTEST_PORTAL_USER": spec["requested_by"], "AUTOTEST_PORTAL_JOB": str(job_id),
                "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
    if spec["groups"]:
        log.write(f"== группы {spec['groups']} для pytest не применяются\n")
    with tempfile.TemporaryDirectory(prefix=f"portal-job-{job_id}-") as tmp:
        out = Path(tmp)
        junit, allure = out / "junit.xml", out / "allure"
        command = pytest_command(python, suite, spec, junit, allure)
        if spec.get("test_ids") is not None:
            if "portal_exact_selection" in " ".join(command) + env.get("PYTEST_ADDOPTS", ""):
                raise ValueError("набор не может переопределять плагин точного выбора")
            (out / "portal_exact_selection.py").write_text(PYTEST_SELECTION, encoding="utf-8")
            env["PORTAL_TEST_IDS"] = json.dumps(spec["test_ids"], ensure_ascii=False)
            env["PYTHONPATH"] = str(out) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            command += ["-p", "portal_exact_selection"]
        log.write("== прогон: " + " ".join(command[1:]) + "\n")
        started = datetime.now().astimezone()
        code = stream(log, command, lambda process: os.killpg(process.pid, signal.SIGTERM),
                      cwd=tests, env=env, start_new_session=True)
        finished = datetime.now().astimezone()
        if log.cancel:
            return code, None
        executions = parse_junit(junit)
        # 0 - всё прошло, 1 - есть падения, 5 - тестов не нашлось; прочее - сбой самого pytest.
        if code not in (0, 1, 5) and not executions:
            return code, None
        report = allure_single_file(log, allure, out, f"{spec['project']} - {Path(spec['suite']).stem} - {started:%Y-%m-%d %H:%M}")
        run_id = write_pytest_run(spec, job_id, results, tests, executions, started, finished, project_env, report)
    return code, run_id


def stand_action(log: Log, spec: dict) -> int:
    """Действие со стендом: команда из манифеста установки, значения параметров - только переменными окружения."""
    env = {k: v for k, v in os.environ.items() if k not in ("AGENT_TOKEN", *LIBRARY_API_NAMES)}
    env.update(spec["env"])
    for key in LIBRARY_API_NAMES:
        env.pop(key, None)
    log.write(f"== {spec['action']} стенда {spec['stand']}: {spec['command']}\n")
    for key, value in spec["env"].items():
        log.write(f"   {key}={value}\n")
    # Команда - оболочка с дочерними процессами (сборка, docker compose): при отмене останавливаем всю группу.
    return stream(log, ["bash", "-c", spec["command"]], lambda process: os.killpg(process.pid, signal.SIGTERM),
                  cwd=spec["workdir"], env=env, start_new_session=True)


DATA_LIMIT = 2 * 1024 * 1024


class DataAdapterFailure(RuntimeError):
    def __init__(self, result):
        super().__init__("адаптер данных завершился с ошибкой")
        self.result = result


def run_data_adapter(log: Log, operation: dict) -> dict:
    """Протокол файлов: ни payload, ни stdout/stderr адаптера не становятся логом задачи."""
    adapter, request = operation["adapter"], operation["request"]
    with tempfile.TemporaryDirectory(prefix="portal-data-") as directory:
        root = Path(directory)
        root.chmod(0o700)
        source, target = root / "input.json", root / "output.json"
        for path in (source, target):
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        # Envelope добавляет служебные поля; лимит 2 MiB относится к data/query и результату.
        source.write_text(json.dumps(request, ensure_ascii=False, allow_nan=False, separators=(",", ":")), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k not in ("AGENT_TOKEN", "GITLAB_TOKEN", "TEST_DATA_API_TOKEN", *LIBRARY_API_NAMES)}
        env.update(read_env_file(ENV_DIR / f"{request['project']}.env"))
        for key in ("AGENT_TOKEN", "GITLAB_TOKEN", "TEST_DATA_API_TOKEN", *LIBRARY_API_NAMES):
            env.pop(key, None)
        env.update({"PORTAL_DATA_INPUT": str(source), "PORTAL_DATA_OUTPUT": str(target)})
        process = None
        timer = None
        timed_out = threading.Event()
        try:
            log.flush()
            if log.cancel:
                raise RuntimeError("операция отменена")
            process = subprocess.Popen(["bash", "-c", adapter["command"]], cwd=adapter["workdir"],
                env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)

            def expire():
                timed_out.set()
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

            # Локальный deadline убивает группу даже при зависшем HTTP heartbeat.
            timer = threading.Timer(adapter["timeout"], expire)
            timer.daemon = True
            timer.start()
            deadline = time.monotonic() + adapter["timeout"]
            while process.poll() is None:
                if time.time() - log.sent >= FLUSH:
                    log.flush()  # heartbeat и отмена продолжают работать во время молчащего адаптера
                if log.stop and log.stop.is_set():
                    log.cancel = True
                if log.cancel:
                    raise RuntimeError("операция отменена")
                if time.monotonic() >= deadline:
                    raise DataAdapterFailure({"ok": False, "error": {"code": "adapter_timeout"}})
                time.sleep(0.2)
            # Deadline относится к адаптеру, а не к последующей доставке heartbeat/результата.
            timer.cancel()
            timer.join()
            log.flush()
            if timed_out.is_set():
                raise DataAdapterFailure({"ok": False, "error": {"code": "adapter_timeout"}})
            if log.cancel:
                raise RuntimeError("операция отменена")
            if not stat.S_ISREG(target.lstat().st_mode) or target.stat().st_size > DATA_LIMIT:
                raise DataAdapterFailure({"ok": False, "error": {"code": "invalid_adapter_result"}})
            with target.open("rb") as output:
                raw = output.read(DATA_LIMIT + 1)
            if len(raw) > DATA_LIMIT:
                raise DataAdapterFailure({"ok": False, "error": {"code": "invalid_adapter_result"}})
            result = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(result, dict):
                raise ValueError()
            # Проверяем в том числе переполнение float (1e999) и глубину при повторной сериализации.
            json.dumps(result, ensure_ascii=False, allow_nan=False)
            if process.returncode:
                raise DataAdapterFailure(result)
            return result
        except (OSError, ValueError, TypeError, RecursionError):
            code = "adapter_configuration_error" if process is None else "invalid_adapter_result"
            raise DataAdapterFailure({"ok": False, "error": {"code": code}}) from None
        finally:
            if timer is not None:
                timer.cancel()
                timer.join()
            if process is not None:
                # Убираем и дочерние процессы после штатного выхода оболочки адаптера.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()


def data_operations(log: Log, spec: dict) -> None:
    for operation in spec.get("data_operations") or []:
        try:
            result = run_data_adapter(log, operation)
        except DataAdapterFailure as error:
            api("PUT", f"/api/agent/jobs/{log.job_id}/test-data/{operation['id']}/result", {"data": error.result})
            raise
        api("PUT", f"/api/agent/jobs/{log.job_id}/test-data/{operation['id']}/result", {"data": result})


def execute_stand(job: dict, stop: threading.Event | None = None) -> None:
    job_id, spec = job["id"], job["spec"]
    log = Log(job_id, stop)
    status, message = "failed", None
    try:
        code = 0 if spec.get("data_operation") else stand_action(log, spec)
        if code == 0 and not log.cancel:
            data_operations(log, spec)
        if log.cancel:
            status, message = "cancelled", "отменено"
        else:
            status = "done" if code == 0 else "failed"
            message = None if code == 0 else f"команда завершилась с кодом {code}"
    except Exception as error:  # noqa: BLE001
        # В исключениях библиотек/HTTP может быть закрытый input. Только фиксированное сообщение.
        if spec.get("data_operation") or spec.get("data_operations"):
            message = "операция с тестовыми данными не выполнена"
            log.write("== " + message + "\n")
        else:
            log.write(f"== ошибка агента: {error}\n")
            message = str(error)
        if log.cancel:
            status, message = "cancelled", "отменено"
    log.flush()
    api("POST", f"/api/agent/jobs/{job_id}/finish", {"status": status, "message": message})


def pump_data_children(parent_id: int, stop: threading.Event) -> None:
    """Единственный дополнительный поток; выдача ограничена детьми текущего прогона."""
    while not stop.is_set():
        try:
            child = api("POST", f"/api/agent/jobs/{parent_id}/data-claim", {"capabilities": ["stand-host"]}, timeout=10)
            if child:
                # При гонке с stop execute_stand увидит отмену до запуска адаптера.
                execute_stand(child, stop)
                continue
        except Exception:  # закрытые ответы и ошибки не печатаются в stdout агента
            pass
        stop.wait(1)


def execute(job: dict) -> None:
    if job["kind"] == "stand":
        execute_stand(job)
        return
    job_id, spec = job["id"], job["spec"]
    log = Log(job_id)
    status, run_id, message = "failed", None, None
    stop = threading.Event()
    pump = None
    if "stand-host" in CAPABILITIES:
        pump = threading.Thread(target=pump_data_children, args=(job_id, stop), daemon=True)
        pump.start()
    try:
        tests, results, library = prepare(log, spec)
        if spec.get("runner", "newman") == "pytest":
            code, _ = run_pytest(log, job_id, spec, tests, results)
        else:
            code = run_tests(log, job_id, spec, tests, results, library)
        if log.cancel:
            status, message = "cancelled", "отменено"
        else:
            run_id = publish_results(log, job_id, results, spec["requested_by"])
            # Упавшие тесты - штатный итог прогона; сбой - когда прогона нет вообще.
            status = "done" if run_id else "failed"
            message = None if run_id else f"прогон не создан, код {code}"
    except Exception as error:  # noqa: BLE001
        log.write(f"== ошибка агента: {error}\n")
        message = str(error)
    finally:
        stop.set()
        if pump:
            # Не завершаем родителя, пока дочерний адаптер и его TemporaryDirectory ещё живы.
            pump.join()
    log.flush()
    api("POST", f"/api/agent/jobs/{job_id}/finish", {"status": status, "run_id": run_id, "message": message})
    if AFTER_JOB:
        subprocess.run(AFTER_JOB, shell=True, check=False)


def main() -> None:
    print(f"агент: {URL}, возможности: {CAPABILITIES}", flush=True)
    while True:
        try:
            claim = {"capabilities": CAPABILITIES}
            if MANAGED_STAND_HOST:
                claim["host_id"] = MANAGED_STAND_HOST
            job = api("POST", "/api/agent/claim", claim)
            if job:
                print(f"задача {job['id']}", flush=True)
                execute(job)
                continue
        except (urllib.error.URLError, OSError) as error:
            print(f"портал недоступен: {error}", flush=True)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
