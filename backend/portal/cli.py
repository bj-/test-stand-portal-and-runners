"""Команды обслуживания: sync, user add/reset/ensure-default-admin, token add."""
import argparse
import getpass
import sys
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from . import auth
from .db import SessionLocal
from .importer import sync_project
from .models import ApiToken, User
from .registry import RegistryError, load_registry


def ask_password() -> str:
    password = getpass.getpass("Пароль: ")
    if len(password) < auth.MIN_PASSWORD:
        raise SystemExit(f"пароль короче {auth.MIN_PASSWORD} символов")
    if password != getpass.getpass("Ещё раз: "):
        raise SystemExit("пароли не совпали")
    return password


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync", help="обновить индекс из репозиториев результатов")
    sync.add_argument("--project", help="код проекта; по умолчанию все")
    user = commands.add_parser("user", help="учётки").add_subparsers(dest="action", required=True)
    add = user.add_parser("add")
    add.add_argument("login")
    add.add_argument("--role", choices=auth.USER_ROLES, required=True)
    add.add_argument("--name", default="")
    user.add_parser("reset").add_argument("login")
    user.add_parser("ensure-default-admin", help="создать admin со случайным паролем только в пустой БД")
    token = commands.add_parser("token", help="токены API").add_subparsers(dest="action", required=True)
    add_token = token.add_parser("add")
    add_token.add_argument("name")
    add_token.add_argument("--role", choices=auth.ALL_ROLES, required=True)
    provision = commands.add_parser("stand-agent").add_subparsers(dest="action", required=True).add_parser("provision")
    provision.add_argument("--name", required=True)
    provision.add_argument("--host", required=True)
    provision.add_argument("--token-file", required=True)
    args = parser.parse_args()

    if args.command == "stand-agent":
        from .stand_contract import identifier, StandError
        try:
            identifier(args.host, "host")
            if not args.name.strip() or len(args.name) > 120 or not args.name.isprintable():
                raise ValueError()
            secret = Path(args.token_file).read_text(encoding="utf-8").strip()
            if len(secret) < 32 or len(secret) > 4096 or not secret.isascii() or not secret.isprintable():
                raise ValueError()
            digest = auth.token_hash(secret)
            with SessionLocal() as session:
                existing = session.scalar(select(ApiToken).where(ApiToken.name == args.name))
                same_hash = session.get(ApiToken, digest)
                if existing is not None:
                    if (existing.token_hash != digest or existing.role != auth.AGENT
                            or existing.stand_host != args.host or not existing.active):
                        raise ValueError()
                elif same_hash is not None:
                    raise ValueError()
                else:
                    session.add(ApiToken(name=args.name, token_hash=digest, role=auth.AGENT,
                                         active=True, stand_host=args.host))
                    session.commit()
        except (OSError, UnicodeError, ValueError, StandError, SQLAlchemyError):
            print("Не удалось зарегистрировать агента: проверьте файл токена и существующую привязку", file=sys.stderr)
            return 1
        print("Агент зарегистрирован")
        return 0

    if args.command == "user":
        if args.action == "ensure-default-admin":
            try:
                with SessionLocal() as session:
                    password = auth.ensure_default_admin(session)
            except (SQLAlchemyError, RuntimeError):
                print("Не удалось создать начального администратора: проверьте доступность и состояние БД.", file=sys.stderr)
                return 1
            if password is not None:
                print(f"Создан администратор портала. Логин: admin; пароль: {password}.")
                print("При входе можно сменить пароль или оставить его как есть.")
            else:
                print("В портале уже есть пользователи; существующие учётные записи не изменены.")
            return 0
        password = ask_password()
        with SessionLocal() as session:
            if args.action == "add":
                auth.create_user(session, args.login, args.role, password, args.name)
            else:
                existing = session.get(User, args.login)
                if existing is None:
                    raise SystemExit("пользователь не найден")
                existing.password_hash = auth.hash_password(password)
                existing.password_change_suggested = False
                existing.locked_until = None
                session.commit()
        print("готово")
        return 0
    if args.command == "token":
        with SessionLocal() as session:
            print(auth.create_token(session, args.name, args.role))
        return 0

    with SessionLocal() as session:
        try:
            registry = load_registry(session)
            projects = [registry.get(args.project)] if args.project else list(registry.projects.values())
        except KeyError:
            print("проект не найден", file=sys.stderr)
            return 1
        except RegistryError as error:
            print(str(error), file=sys.stderr)
            return 1
        failed = bool(registry.errors) if not args.project else False
        if not args.project:
            for code, error in registry.errors.items():
                print(f"{code}: {error}", file=sys.stderr)
        for project in projects:
            if project.results is None:
                print(f"{project.code}: пропущен — репозиторий результатов не настроен")
                continue
            print(project.code, sync_project(session, project))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
