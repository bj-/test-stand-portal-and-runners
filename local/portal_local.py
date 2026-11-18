#!/usr/bin/env python3
"""Изолированная локальная установка портала; Python 3.12+, только stdlib."""
from __future__ import annotations

import argparse
import codecs
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys

import agent_local

REPO = Path(__file__).resolve().parents[1]
MARKER = ".portal-local.json"
ENV_KEYS = {"POSTGRES_PASSWORD", "BASE_REGISTRY", "PORTAL_COOKIE_SECURE"}


class LocalError(Exception):
    pass


def valid_name(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", value):
        raise argparse.ArgumentTypeError("имя: 1–40 строчных латинских букв, цифр, _ или -; первый символ — буква/цифра")
    return value


def valid_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        port = 0
    if not 1024 <= port <= 65535:
        raise argparse.ArgumentTypeError("порт должен быть целым числом от 1024 до 65535")
    return port


def portal_source_path(value: str) -> Path:
    source = Path(value).expanduser().resolve()
    if not all((source / name).is_file() for name in ("docker-compose.yaml", "Dockerfile")):
        raise LocalError("portal source должен содержать docker-compose.yaml и Dockerfile продукта")
    return source


def workspace_path(value: str, source: Path | None = None) -> Path:
    path = Path(value).expanduser().resolve()
    for root in (REPO, source):
        if root is not None and (path == root or root in path.parents):
            raise LocalError("workspace должен находиться вне исходников launcher и портала")
    # Compose dotenv uses single quotes for literal paths.
    if any(char in str(path) for char in ("'", "\n", "\r")):
        raise LocalError("путь workspace не должен содержать одинарные кавычки или переводы строки")
    return path


def owned_path(workspace: Path, relative: str, *, directory: bool = False) -> Path:
    path = workspace / relative
    if path.is_symlink() or path.resolve() != path:
        raise LocalError(f"путь установки не должен быть ссылкой: {relative}")
    if not (path.is_dir() if directory else path.is_file()):
        raise LocalError(f"в workspace отсутствует {'каталог' if directory else 'файл'} {relative}")
    return path


def load_workspace(workspace: Path) -> dict:
    marker = owned_path(workspace, MARKER)
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        valid_name(data["name"])
        valid_port(str(data["port"]))
        source = portal_source_path(data["portal_source"])
        workspace_path(str(workspace), source)
        expected = compose_name(workspace, data["name"])
        if (data["version"] != 1 or data["workspace"] != str(workspace)
                or data["project_name"] != expected or data["portal_source"] != str(source)):
            raise ValueError
    except (ValueError, KeyError, TypeError, argparse.ArgumentTypeError):
        raise LocalError("некорректный marker workspace; управление установкой остановлено") from None
    for relative in (".env", "projects.yaml"):
        owned_path(workspace, relative)
    for relative in ("data", "data/postgres", "data/artifacts", "results"):
        owned_path(workspace, relative, directory=True)
    # Read only the required generated secret; never print the environment file.
    password = env_password(workspace)
    if not password:
        raise LocalError("в .env workspace отсутствует POSTGRES_PASSWORD")
    return data


def compose_name(workspace: Path, name: str) -> str:
    identity = os.path.normcase(str(workspace)).encode("utf-8")
    return f"{name}-{hashlib.sha256(identity).hexdigest()[:12]}"


def env_password(workspace: Path) -> str:
    for line in (workspace / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("POSTGRES_PASSWORD="):
            return line.split("=", 1)[1].strip().strip("\"'")
    return ""


def init_workspace(workspace: Path, name: str | None, port: int | None, source: Path) -> dict:
    if workspace.exists():
        if not workspace.is_dir():
            raise LocalError("workspace должен быть каталогом")
        if (workspace / MARKER).exists():
            data = load_workspace(workspace)
            if data["portal_source"] != str(source):
                raise LocalError("workspace привязан к другому portal source; повтор init сохраняет настройки")
            if name is not None and name != data["name"] or port is not None and port != data["port"]:
                raise LocalError("workspace уже настроен; повтор init сохраняет имя и порт")
            agent_local.ensure_files(workspace, data)
            return data
        if any(workspace.iterdir()):
            raise LocalError("workspace не пуст и не принадлежит launcher; выберите новый каталог")
    name, port = name or "portal-local", port or 8080
    data = {"version": 1, "workspace": str(workspace), "name": name, "portal_source": str(source),
            "project_name": compose_name(workspace, name), "port": port}
    workspace.mkdir(parents=True, exist_ok=True)
    for relative in ("data/postgres", "data/artifacts", "results"):
        (workspace / relative).mkdir(parents=True)
    with (workspace / "projects.yaml").open("x", encoding="utf-8") as stream:
        stream.write("# Проекты этой локальной установки\nprojects: {}\n")
    variables = {"POSTGRES_PASSWORD": secrets.token_hex(32),
                 "PORTAL_BIND": f"127.0.0.1:{port}",
                 "PROJECTS_FILE": (workspace / "projects.yaml").as_posix(),
                 "PORTAL_DATA": (workspace / "data").as_posix(),
                 "RESULTS_ROOT": (workspace / "results").as_posix(),
                 "PORTAL_COOKIE_SECURE": "false"}
    env_path = workspace / ".env"
    with env_path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write("".join(f"{key}='{value}'\n" for key, value in variables.items()))
    if os.name != "nt":
        env_path.chmod(0o600)
    # Marker is written last: incomplete installations cannot be managed.
    with (workspace / MARKER).open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    agent_local.ensure_files(workspace, data)
    return data


def docker_environment(workspace: Path, data: dict) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("COMPOSE_") and key not in ENV_KEYS}
    # The process environment wins over dotenv: edited or inherited bind/path
    # settings cannot redirect this installation to another address or data root.
    env.update(POSTGRES_PASSWORD=env_password(workspace),
               PORTAL_BIND=f"127.0.0.1:{data['port']}",
               PROJECTS_FILE=(workspace / "projects.yaml").as_posix(),
               PORTAL_DATA=(workspace / "data").as_posix(),
               RESULTS_ROOT=(workspace / "results").as_posix(),
               PORTAL_COOKIE_SECURE="false")
    return env


def check_docker(env: dict[str, str]) -> str:
    docker = shutil.which("docker") or shutil.which("docker.exe")
    if not docker:
        raise LocalError("Docker не найден в PATH. Установите Docker с Compose v2 и запустите Docker Engine/Desktop.")

    def probe(args: list[str], message: str) -> str:
        try:
            result = subprocess.run([docker, *args], env=env, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            raise LocalError(message) from None
        if result.returncode:
            raise LocalError(message)
        return (result.stdout or "").strip()

    probe(["compose", "version"], "недоступен Docker Compose v2")
    # Inspect local metadata before talking to a daemon. DOCKER_CONTEXT takes
    # precedence over DOCKER_HOST, just as in the Docker CLI itself.
    endpoint = env.get("DOCKER_HOST") if not env.get("DOCKER_CONTEXT") else None
    if not endpoint:
        context = [env["DOCKER_CONTEXT"]] if env.get("DOCKER_CONTEXT") else []
        endpoint = probe(["context", "inspect", *context, "--format", "{{.Endpoints.docker.Host}}"],
                         "не удалось проверить локальный Docker context")
    if not endpoint.startswith(("unix:///", "npipe:////./pipe/")):
        raise LocalError("нужен локальный Docker context (Unix socket/Windows pipe); удалённый daemon для localhost установки не подходит")
    if probe(["info", "--format", "{{.OSType}}"],
             "Docker daemon недоступен; запустите Docker Engine/Desktop") != "linux":
        raise LocalError("нужен Docker daemon с Linux containers; переключите Docker Desktop в Linux containers")
    return docker


def compose_command(docker: str, workspace: Path, data: dict, args: argparse.Namespace) -> list[str]:
    source = Path(data["portal_source"])
    command = [docker, "compose", "--project-name", data["project_name"],
               "--project-directory", str(source), "--env-file", str(workspace / ".env"),
               "--file", str(source / "docker-compose.yaml")]
    if (workspace / "agent.compose.json").exists():
        command.extend(["--file", str(owned_path(workspace, "agent.compose.json"))])
    actions = {"up": ["up", "-d", "--build"], "down": ["down"],
               "status": ["ps", "--all"], "logs": ["logs", "--tail", str(getattr(args, "tail", 100))]}
    if args.command in actions:
        command.extend(actions[args.command])
        if args.command == "logs" and args.follow:
            command.append("--follow")
    else:
        command.extend(["exec"])
        if args.command in ("sync", "admin-init", "agent-init"):
            command.append("-T")
        command.extend(["portal", "python", "-m", "portal.cli"])
        if args.command == "user-add":
            command.extend(["user", "add", args.login, "--role", args.role, f"--name={args.display_name}"])
        elif args.command == "admin-init":
            command.extend(["user", "ensure-default-admin"])
        elif args.command == "agent-init":
            config = agent_local.configuration(workspace)
            command.extend(["stand-agent", "provision", "--name", data["project_name"] + "-agent",
                            "--host", config["host_id"], "--token-file", "/config/local-agent/token"])
        else:
            command.append("sync")
            if args.project:
                command.append(f"--project={args.project}")
    return command


def redact_prefix(pending: bytes, secret_values: list[bytes], *, final: bool = False) -> tuple[bytes, bytes]:
    """Keep incomplete secret prefixes, including prefixes of a longer secret."""
    output = bytearray()
    index = 0
    while index < len(pending):
        remaining_size = len(pending) - index
        if not final and any(remaining_size < len(secret) and secret.startswith(pending[index:])
                             for secret in secret_values):
            break
        match = next((secret for secret in secret_values if pending.startswith(secret, index)), None)
        if match is not None:
            output.extend(b"[REDACTED]")
            index += len(match)
        else:
            output.append(pending[index])
            index += 1
    return bytes(output), pending[index:]


def run_compose(command: list[str], env: dict[str, str], password: str, *, noninteractive: bool = False,
                redactions: tuple[str, ...] = ()) -> int:
    # Stream output while retaining a tail so a secret split across reads is redacted.
    try:
        with subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL if noninteractive else None, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT) as process:
            pending = b""
            secret_values = sorted({value.encode("utf-8") for value in (password, *redactions) if value}, key=len, reverse=True)
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            while chunk := process.stdout.read1(4096):
                pending += chunk
                forwarded, pending = redact_prefix(pending, secret_values)
                sys.stdout.write(decoder.decode(forwarded))
                sys.stdout.flush()
            forwarded, _ = redact_prefix(pending, secret_values, final=True)
            sys.stdout.write(decoder.decode(forwarded, final=True))
            return process.wait()
    except OSError:
        raise LocalError("не удалось запустить Docker; проверьте установку и PATH") from None


def run_interactive(command: list[str], env: dict[str, str]) -> int:
    # Compose allocates the container TTY for getpass. Keep all three standard
    # streams attached to the user's terminal; a pipe would change TTY detection.
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise LocalError("user-add требует интерактивный терминал для безопасного ввода пароля")
    try:
        return subprocess.run(command, env=env).returncode
    except OSError:
        raise LocalError("не удалось запустить Docker; проверьте установку и PATH") from None


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--workspace", required=True, help="каталог установки вне исходников")
    result.add_argument("--portal-source", help="путь отдельного клона портала; обязателен для первого init")
    commands = result.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="создать пустую установку без Docker")
    init.add_argument("--name", type=valid_name, help="основа имени Compose (по умолчанию portal-local)")
    init.add_argument("--port", type=valid_port, help="localhost порт (по умолчанию 8080)")
    for name, help_text in (("doctor", "проверить workspace, Compose и Docker daemon"),
                            ("up", "собрать и запустить портал"), ("down", "остановить, сохранив данные"),
                            ("status", "состояние контейнеров")):
        commands.add_parser(name, help=help_text)
    commands.add_parser("admin-init", help="создать admin/admin только в пустой базе пользователей")
    commands.add_parser("agent-init", help="зарегистрировать токен host-агента, сохранив существующий")
    commands.add_parser("agent-status", help="показать состояние host-агента")
    for action, default in (("agent-start", 15), ("agent-stop", 30)):
        agent = commands.add_parser(action, help="запустить/остановить host-агент без системной службы")
        agent.add_argument("--timeout", type=float, default=default, help="срок подтверждения в секундах")
    logs = commands.add_parser("logs", help="журнал контейнеров (секрет БД скрывается)")
    logs.add_argument("--follow", action="store_true")
    logs.add_argument("--tail", type=int, choices=range(1, 10001), default=100, metavar="1..10000")
    user = commands.add_parser("user-add", help="интерактивно добавить пользователя")
    user.add_argument("login")
    user.add_argument("--role", choices=("admin", "tester", "developer", "viewer"), default="admin")
    user.add_argument("--display-name", default="")
    sync = commands.add_parser("sync", help="индексировать результаты существующим CLI")
    sync.add_argument("--project")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        workspace = workspace_path(args.workspace)
        if args.command == "init":
            existing = load_workspace(workspace) if (workspace / MARKER).is_file() else None
            source_value = args.portal_source or (existing["portal_source"] if existing else None)
            if not source_value:
                raise LocalError("для первого init требуется --portal-source PATH к отдельному клону портала")
            source = portal_source_path(source_value)
            workspace = workspace_path(args.workspace, source)
            data = init_workspace(workspace, args.name, args.port, source)
            print(f"Workspace готов: {workspace}\nCompose: {data['project_name']}\nПортал: http://127.0.0.1:{data['port']}")
            return 0
        data = load_workspace(workspace)
        if args.portal_source and str(portal_source_path(args.portal_source)) != data["portal_source"]:
            raise LocalError("--portal-source не совпадает с исходниками, записанными в marker workspace")
        if args.command in ("agent-start", "agent-stop"):
            if not 0 < args.timeout <= 900:
                raise LocalError("timeout агента должен быть больше 0 и не больше 900 секунд")
            if args.command == "agent-start":
                agent_local.ensure_files(workspace, data)
                print(agent_local.start(workspace, timeout=args.timeout))
            else:
                print(agent_local.stop(workspace, timeout=args.timeout))
            return 0
        if args.command == "agent-status":
            print(agent_local.describe(workspace))
            return 0
        if args.command in ("up", "agent-init"):
            agent_local.ensure_files(workspace, data)
        if args.command == "down":
            print(agent_local.stop(workspace))
        if args.command == "status":
            print(agent_local.describe(workspace))
        env = docker_environment(workspace, data)
        docker = check_docker(env)
        if args.command == "doctor":
            print(f"Workspace, Docker Compose и daemon доступны. Compose: {data['project_name']}")
            return 0
        command = compose_command(docker, workspace, data, args)
        if args.command == "user-add":
            return run_interactive(command, env)
        redactions = (agent_local.token(workspace),) if (workspace / "agent/token").exists() else ()
        return run_compose(command, env, env_password(workspace),
                           noninteractive=args.command in ("admin-init", "agent-init"), redactions=redactions)
    except (LocalError, agent_local.AgentError, OSError, UnicodeError) as error:
        # OS error filenames may contain installation details; never dump .env.
        message = str(error) if isinstance(error, (LocalError, agent_local.AgentError)) else "не удалось прочитать или создать файлы workspace; проверьте путь и права"
        print(f"Ошибка: {message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
