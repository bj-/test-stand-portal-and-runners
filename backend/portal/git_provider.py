"""Bounded, credential-transient repository discovery for configured Git origins."""

from __future__ import annotations

import json
from http.client import HTTPException
import re
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from .stand_git import GitSourceError, parse_git_url


class GitProviderError(ValueError):
    """Neutral public error; upstream messages and credentials must never escape."""


_INVALID_SERVER = "Некорректный HTTPS адрес Git-сервера: укажите только сервер и порт"
_INVALID_REPOSITORY = "Некорректный HTTPS URL репозитория или другой Git-сервер"
_UNAVAILABLE = "Не удалось получить список репозиториев; можно указать HTTPS URL вручную"
_DENIED = "Нет доступа к API списка репозиториев; проверьте права токена или укажите HTTPS URL вручную"
PAGE_SIZE = 30
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT = 20


def normalize_server_url(url: str) -> str:
    """Return a canonical HTTPS origin, rejecting paths, credentials and queries."""
    try:
        if not isinstance(url, str) or len(url) > 2048:
            raise ValueError
        parts = urlsplit(url)
        if parts.path not in ("", "/"):
            raise ValueError
        # Reuse the clone parser's strict host, port and ambiguity validation.
        origin = url.removesuffix("/")
        canonical, _, _ = parse_git_url(origin + "/portal-origin-check")
        return canonical.rsplit("/", 1)[0]
    except (TypeError, ValueError, UnicodeError):
        raise GitProviderError(_INVALID_SERVER) from None


def repository_on_server(base_url: str, git_url: str) -> str:
    """Validate a clone URL against the configured exact HTTPS origin."""
    origin = normalize_server_url(base_url)
    try:
        canonical, _, _ = parse_git_url(git_url)
        parts = urlsplit(canonical)
        if "https://" + parts.netloc != origin:
            raise ValueError
        return canonical
    except (GitSourceError, ValueError, TypeError):
        raise GitProviderError(_INVALID_REPOSITORY) from None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def list_repositories(provider: str, base_url: str, token: str, page: int = 1) -> dict:
    """Fetch one page only; never follow provider URLs, redirects or env proxies.

    Listing needs API permissions that are distinct from cloning permissions.
    A denied API therefore leaves manual HTTPS binding available to the caller.
    """
    origin = normalize_server_url(base_url)
    if provider not in ("gitlab", "github"):
        raise GitProviderError("Этот Git-сервер поддерживает указание HTTPS URL вручную")
    if type(page) is not int or not 1 <= page <= 10000:
        raise GitProviderError("Некорректная страница списка репозиториев")
    if (not isinstance(token, str) or not token or len(token) > 16384
            or not token.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in token)):
        raise GitProviderError(_DENIED)
    headers = {"Accept": "application/json", "User-Agent": "test-portal"}
    query = {"per_page": PAGE_SIZE, "page": page}
    if provider == "gitlab":
        endpoint = origin + "/api/v4/projects"
        query.update(membership="true", simple="true")
        headers["PRIVATE-TOKEN"] = token
    else:
        endpoint = ("https://api.github.com/user/repos" if origin == "https://github.com"
                    else origin + "/api/v3/user/repos")
        headers.update({"Authorization": "Bearer " + token,
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28"})
    try:
        opener = build_opener(ProxyHandler({}), _NoRedirect(),
                              HTTPSHandler(context=ssl.create_default_context()))
        request = Request(endpoint + "?" + urlencode(query), headers=headers)
        with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
            if response.status != 200:
                raise GitProviderError(_UNAVAILABLE)
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise GitProviderError(_UNAVAILABLE)
            rows = json.loads(raw)
            if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
                raise GitProviderError(_UNAVAILABLE)
            # Pagination headers are only hints. Their URLs are never requested.
            if provider == "gitlab":
                next_page = response.headers.get("X-Next-Page")
                has_next = bool(next_page) if next_page is not None else len(rows) == PAGE_SIZE
            else:
                link = response.headers.get("Link")
                has_next = bool(re.search(r';\s*rel="next"', link)) if link is not None else len(rows) == PAGE_SIZE
        items = []
        for row in rows:
            if not isinstance(row, dict):
                raise GitProviderError(_UNAVAILABLE)
            name = row.get("name")
            path = row.get("path_with_namespace" if provider == "gitlab" else "full_name")
            url = row.get("http_url_to_repo" if provider == "gitlab" else "clone_url")
            if (not isinstance(name, str) or not name or len(name) > 512
                    or not isinstance(path, str) or not path or len(path) > 2048
                    or any(ord(c) < 32 or ord(c) == 127 for c in name + path)):
                raise GitProviderError(_UNAVAILABLE)
            items.append({"name": name, "path": path,
                          "git_url": repository_on_server(origin, url)})
        return {"items": items, "page": page, "has_next": has_next}
    except HTTPError as error:
        status = error.code
        error.close()
        raise GitProviderError(_DENIED if status in (401, 403) else _UNAVAILABLE) from None
    except (URLError, OSError, HTTPException, ValueError, TypeError, RecursionError):
        raise GitProviderError(_UNAVAILABLE) from None
