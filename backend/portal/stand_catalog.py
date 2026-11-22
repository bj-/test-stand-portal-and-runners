"""Trusted installation host mappings and read-only observations of local Git clones.

Host IDs name execution targets; this catalogue does not assert agent availability.
Repository registration and project permissions are checked separately by the API.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
import json
import os
from pathlib import Path
import subprocess

from .registry import _valid_code
from .stand_contract import SHA, StandError, identifier, obj, ref_name, string, version


class CatalogError(ValueError):
    """Unavailable or invalid installation data, safe to expose to an API client."""


@dataclass(frozen=True)
class Host:
    id: str
    title: str
    port_min: int
    port_max: int
    repositories: dict[str, dict[str, Path]]
    ref_source: str = "local"
    publish_ip: str = "127.0.0.1"


PUBLISH_NETWORKS = tuple(map(IPv4Network, ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))


def publish_address(value: str) -> str:
    address = IPv4Address(string(value, "publish_ip"))
    if not any(address in network for network in PUBLISH_NETWORKS):
        raise CatalogError("Некорректный адрес публикации стендов")
    return str(address)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError("Некорректный каталог хостов стендов")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise CatalogError("Некорректный каталог хостов стендов")


def load_hosts() -> dict[str, Host]:
    """Load PORTAL_STAND_HOSTS without probing clones or exposing installation paths."""
    filename = os.environ.get("PORTAL_STAND_HOSTS")
    if filename is None:
        return {}
    try:
        with Path(filename).open(encoding="utf-8") as stream:
            data = json.load(stream, object_pairs_hook=_unique_object,
                             parse_constant=_invalid_constant)
        obj(data, ("schema_version", "hosts"))
        version(data["schema_version"], "catalogue")
        if not isinstance(data["hosts"], list):
            raise CatalogError("Некорректный каталог хостов стендов")
        hosts = {}
        for entry in data["hosts"]:
            obj(entry, ("id", "title", "port_range", "repositories"),
                ("ref_source", "publish_ip"))
            host_id = identifier(entry["id"], "host")
            title = string(entry["title"], "host title")
            ports = entry["port_range"]
            if (not isinstance(ports, list) or len(ports) != 2
                    or any(type(p) is not int or not 1 <= p <= 65535 for p in ports)
                    or ports[0] > ports[1] or host_id in hosts):
                raise CatalogError("Некорректный каталог хостов стендов")
            if not isinstance(entry["repositories"], dict):
                raise CatalogError("Некорректный каталог хостов стендов")
            repositories = {}
            for project, mapping in entry["repositories"].items():
                if not _valid_code(project):
                    raise CatalogError("Некорректный каталог хостов стендов")
                if not isinstance(mapping, dict):
                    raise CatalogError("Некорректный каталог хостов стендов")
                repositories[project] = {}
                for repo_id, raw_path in mapping.items():
                    identifier(repo_id, "repository")
                    path = Path(string(raw_path, "repository path"))
                    if not path.is_absolute():
                        raise CatalogError("Некорректный каталог хостов стендов")
                    repositories[project][repo_id] = path
            ref_source = entry.get("ref_source", "local")
            if ref_source not in ("local", "origin"):
                raise CatalogError("Некорректный источник ревизий стендов")
            publish_ip = publish_address(entry.get("publish_ip", "127.0.0.1"))
            hosts[host_id] = Host(host_id, title, ports[0], ports[1], repositories,
                                  ref_source, publish_ip)
        return hosts
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        raise CatalogError("Некорректный каталог хостов стендов") from None


def public_host(host: Host) -> dict:
    """The only host fields intended for API responses."""
    return {"id": host.id, "title": host.title,
            "port_range": [host.port_min, host.port_max]}


def _git(path: Path, *args: str) -> str:
    # Neither inherited Git routing/config variables nor optional hooks/locks apply.
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_SYSTEM=os.devnull,
               GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0",
               GIT_NO_REPLACE_OBJECTS="1", GIT_TERMINAL_PROMPT="0")
    try:
        done = subprocess.run(
            ["git", "--no-pager", "-c", "core.fsmonitor=false", "-c", f"safe.directory={path}",
             "-C", str(path), *args],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, shell=False, timeout=20,
            text=True, encoding="utf-8",
        )
        if done.returncode:
            raise CatalogError("Репозиторий стенда недоступен")
        return done.stdout.strip()
    except (OSError, UnicodeError, subprocess.SubprocessError):
        raise CatalogError("Репозиторий стенда недоступен") from None


def _clone(host: Host, project: str, repository: str) -> Path:
    try:
        if not _valid_code(project):
            raise CatalogError("Репозиторий стенда недоступен")
        identifier(repository, "repository")
        path = host.repositories[project][repository]
        if not path.is_absolute() or not path.is_dir():
            raise CatalogError("Репозиторий стенда недоступен")
        path = path.resolve()
        if (_git(path, "rev-parse", "--is-bare-repository") != "false"
                or Path(_git(path, "rev-parse", "--show-toplevel")).resolve() != path):
            raise CatalogError("Репозиторий стенда недоступен")
        return path
    except (KeyError, OSError, ValueError, TypeError):
        raise CatalogError("Репозиторий стенда недоступен") from None


def _ref_prefixes(host: Host) -> tuple[str, str]:
    if host.ref_source == "origin":
        return "refs/remotes/origin/", "refs/remotes/origin-tags/"
    return "refs/heads/", "refs/tags/"


def _direct_refs(path: Path, *patterns: str):
    for line in _git(path, "for-each-ref", "--format=%(refname)%09%(symref)", *patterns).splitlines():
        name, _, symbolic_target = line.partition("\t")
        if not symbolic_target:
            yield name


def list_refs(host: Host, project: str, repository: str) -> list[dict[str, str]]:
    """List branches and tags from the installation's configured local Git refs."""
    path = _clone(host, project, repository)
    prefixes = _ref_prefixes(host)
    result = []
    for name in _direct_refs(path, *prefixes):
        for prefix, kind in zip(prefixes, ("branch", "tag")):
            if name.startswith(prefix):
                result.append({"name": name[len(prefix):], "kind": kind})
                break
    return sorted(result, key=lambda entry: (entry["kind"], entry["name"]))


def resolve_ref(host: Host, project: str, repository: str, ref: str) -> str:
    """Observe a full commit SHA now; this is not an execution host reservation."""
    try:
        requested = ref_name(ref)
    except StandError:
        raise CatalogError("Некорректная ссылка на ветку, тег или коммит репозитория стенда") from None
    path = _clone(host, project, repository)
    if SHA.fullmatch(requested):
        resolved = _git(path, "rev-parse", "--verify", "--end-of-options", requested + "^{commit}")
        if resolved.lower() != requested.lower():
            raise CatalogError("Ссылка на ветку, тег или коммит репозитория стенда недоступна или неоднозначна")
    else:
        branches, tags = _ref_prefixes(host)
        if requested.startswith("refs/heads/"):
            candidates = [branches + requested.removeprefix("refs/heads/")]
        elif requested.startswith("refs/tags/"):
            candidates = [tags + requested.removeprefix("refs/tags/")]
        else:
            candidates = [branches + requested, tags + requested]
        exact = [name for name in _direct_refs(path, *candidates) if name in candidates]
        if len(exact) != 1:
            raise CatalogError("Ссылка на ветку, тег или коммит репозитория стенда недоступна или неоднозначна")
        resolved = _git(path, "rev-parse", "--verify", "--end-of-options", exact[0] + "^{commit}")
    if not SHA.fullmatch(resolved):
        raise CatalogError("Ссылка на ветку, тег или коммит репозитория стенда недоступна или неоднозначна")
    return resolved.lower()
