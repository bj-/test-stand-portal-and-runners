"""Private workspace configuration and cooperative host-worker lifecycle."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]


class AgentError(Exception):
    pass


def safe_path(path: Path) -> Path:
    if path.is_symlink() or path.resolve() != path:
        raise AgentError("путь агента не должен быть ссылкой")
    return path


def read_json(path: Path) -> dict:
    safe_path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError):
        raise AgentError("некорректный JSON конфигурации/состояния агента") from None


def create_json(path: Path, value: dict) -> None:
    safe_path(path)
    if not path.exists():
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")


def configuration(workspace: Path) -> dict:
    config = read_json(workspace / "agent/config.json")
    try:
        work_root = Path(config["work_root"])
        token_file = Path(config["token_file"])
        if (config["schema_version"] != 1 or not work_root.is_absolute()
                or not token_file.is_absolute() or token_file != workspace / "agent/token"
                or not isinstance(config["host_id"], str) or not config["host_id"]
                or not isinstance(config["portal_url"], str)
                or not isinstance(config["catalogs"], dict)):
            raise ValueError
        safe_path(work_root)
        for source in (REPO,):
            if work_root == source or source in work_root.parents or work_root in source.parents:
                raise ValueError
    except (KeyError, TypeError, ValueError):
        raise AgentError("некорректная конфигурация агента; token_file должен указывать на workspace/agent/token, work_root — вне исходников") from None
    return config


def ensure_files(workspace: Path, data: dict) -> dict:
    for relative in ("agent", "agent/work", "agent/mirrors"):
        directory = safe_path(workspace / relative)
        directory.mkdir(mode=0o700, exist_ok=True)
    token_file = safe_path(workspace / "agent/token")
    if not token_file.exists():
        try:
            descriptor = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(secrets.token_urlsafe(48) + "\n")
    token(workspace)
    create_json(workspace / "agent/config.json", {
        "schema_version": 1, "portal_url": f"http://127.0.0.1:{data['port']}",
        "token_file": str(token_file), "host_id": "local",
        "work_root": str(workspace / "agent/work"), "catalogs": {},
        "fetch": False, "poll_seconds": 2, "timeout": 900,
    })
    create_json(workspace / "agent/hosts.json", {
        "schema_version": 1, "hosts": [{"id": "local", "title": "Локальный Docker",
                                         "port_range": [20000, 20999], "repositories": {}}],
    })
    create_json(workspace / "agent.compose.json", {"services": {"portal": {
        "environment": {"PORTAL_STAND_HOSTS": "/config/local-agent/hosts.json"},
        "volumes": [
            {"type": "bind", "source": (workspace / "agent").as_posix(),
             "target": "/config/local-agent", "read_only": True},
            {"type": "bind", "source": (workspace / "agent/mirrors").as_posix(),
             "target": "/stand-mirrors", "read_only": True},
        ],
    }}})
    config = configuration(workspace)
    work_root = Path(config["work_root"])
    source = Path(data["portal_source"])
    if work_root == source or source in work_root.parents or work_root in source.parents:
        raise AgentError("work_root агента должен находиться вне исходников портала")
    hosts = read_json(workspace / "agent/hosts.json").get("hosts")
    if not isinstance(hosts, list) or not any(isinstance(host, dict) and host.get("id") == config["host_id"] for host in hosts):
        raise AgentError("host_id агента отсутствует в agent/hosts.json")
    work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return config


def token(workspace: Path) -> str:
    path = safe_path(workspace / "agent/token")
    value = path.read_text(encoding="utf-8").strip()
    if len(value) < 32 or any(char.isspace() for char in value):
        raise AgentError("некорректный файл токена агента; существующий секрет не изменён")
    if os.name != "nt":
        path.chmod(0o600)
    return value


@contextmanager
def file_lock(path: Path):
    """The same nonblocking byte/file lock convention as stands.worker."""
    safe_path(path)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            yield False
            return
        try:
            yield True
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def worker_locked(work_root: Path) -> bool:
    path = safe_path(work_root / ".worker.lock")
    if not path.exists():
        return False
    with file_lock(path) as acquired:
        return not acquired


def read_status(work_root: Path) -> dict | None:
    path = safe_path(work_root / "status.json")
    if not path.exists():
        return None
    try:
        status = read_json(path)
        stamp = datetime.fromisoformat(status["heartbeat"].replace("Z", "+00:00"))
        if (not isinstance(status["pid"], int) or isinstance(status["pid"], bool)
                or status["pid"] < 1 or not isinstance(status["run_id"], str)
                or not status["run_id"] or len(status["run_id"]) > 200 or stamp.tzinfo is None):
            return None
        return status
    except (AgentError, FileNotFoundError, KeyError, TypeError, ValueError, AttributeError):
        return None


def fresh(status: dict | None) -> bool:
    if status is None:
        return False
    stamp = datetime.fromisoformat(status["heartbeat"].replace("Z", "+00:00"))
    age = (datetime.now(timezone.utc) - stamp).total_seconds()
    return -5 <= age <= 30


def describe(workspace: Path) -> str:
    if not (workspace / "agent/config.json").exists():
        return "Агент: не настроен (выполните installer)."
    root = Path(configuration(workspace)["work_root"])
    status = read_status(root)
    locked = worker_locked(root)
    if locked and fresh(status):
        return f"Агент: работает (PID {status['pid']}, heartbeat {status['heartbeat']})."
    if locked:
        return "Агент: процесс удерживает lock, свежий heartbeat отсутствует; проверьте agent/agent.log."
    return "Агент: остановлен."


def start(workspace: Path, *, timeout: float = 15) -> str:
    if not math.isfinite(timeout) or timeout <= 0:
        raise AgentError("timeout агента должен быть положительным конечным числом")
    if sys.version_info < (3, 12):
        raise AgentError("для агента требуется Python >=3.12")
    config = configuration(workspace)
    root = Path(config["work_root"])
    with file_lock(workspace / "agent/.control.lock") as acquired:
        if not acquired:
            raise AgentError("другая команда уже управляет агентом; повторите после её завершения")
        if worker_locked(root):
            if fresh(read_status(root)):
                return describe(workspace)
            raise AgentError("агент удерживает lock без свежего heartbeat; второй процесс не запущен")
        # Windows venv redirectors may spawn a Python child with a different PID.
        # A fresh per-launch nonce ties readiness to this launch across redirectors.
        run_id = secrets.token_hex(16)
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        log_path = safe_path(workspace / "agent/agent.log")
        with log_path.open("ab") as output:
            process = subprocess.Popen(
                [sys.executable, "-m", "stands.worker", "--config", str(workspace / "agent/config.json"),
                 "--run-id", run_id],
                cwd=REPO, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, **options)
        deadline = time.monotonic() + timeout
        while True:
            result = process.poll()
            if result is not None:
                raise AgentError(f"агент завершился при запуске (код {result}); проверьте agent/agent.log")
            status = read_status(root)
            if fresh(status) and status["run_id"] == run_id and worker_locked(root):
                return describe(workspace)
            if time.monotonic() >= deadline:
                raise AgentError("агент не подтвердил запуск за отведённое время; проверьте agent/agent.log и agent-status; повторный запуск защищён lock")
            time.sleep(0.2)


def stop(workspace: Path, *, timeout: float = 30) -> str:
    if not math.isfinite(timeout) or timeout <= 0:
        raise AgentError("timeout агента должен быть положительным конечным числом")
    if not (workspace / "agent/config.json").exists():
        return "Агент: не настроен."
    root = Path(configuration(workspace)["work_root"])
    with file_lock(workspace / "agent/.control.lock") as acquired:
        if not acquired:
            raise AgentError("другая команда уже управляет агентом; повторите после её завершения")
        if not worker_locked(root):
            return "Агент: остановлен."
        status = read_status(root)
        if status is None:
            raise AgentError("нет достоверного run_id агента; остановка по PID запрещена, проверьте agent/agent.log")
        stop_path = safe_path(root / ".stop.json")
        temporary = root / f".stop.{secrets.token_hex(12)}.tmp"
        try:
            create_json(temporary, {"run_id": status["run_id"]})
            os.replace(temporary, stop_path)
        finally:
            temporary.unlink(missing_ok=True)
        deadline = time.monotonic() + timeout
        while worker_locked(root):
            current = read_status(root)
            if current is not None and current["run_id"] != status["run_id"]:
                raise AgentError("run_id агента изменился; новый процесс не остановлен")
            if time.monotonic() >= deadline:
                raise AgentError("агент не подтвердил остановку; портал оставлен работать, данные сохранены; проверьте agent/agent.log и повторите agent-stop")
            time.sleep(0.2)
        return "Агент: остановлен."
