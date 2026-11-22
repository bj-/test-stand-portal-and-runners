"""Provider discovery uses mocked HTTP only, with synthetic credentials."""

from io import BytesIO
from http.client import IncompleteRead
import json
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPSHandler, ProxyHandler

import pytest

from portal import git_provider as provider


@pytest.mark.parametrize("url,expected", [
    ("https://GIT.example:443/", "https://git.example"),
    ("https://git.example:8443", "https://git.example:8443"),
    ("https://[2001:db8::1]:443/", "https://[2001:db8::1]"),
])
def test_server_origin_normalization(url, expected):
    assert provider.normalize_server_url(url) == expected


@pytest.mark.parametrize("url", [
    "http://git.example", "https://user:secret@git.example", "https://git.example/group",
    "https://git.example//", "https://git.example?", "https://git.example/#",
    "https://git.example\\evil", "https://git.example:/", "https://git.example:65536",
    "https://git.example/%2f", "https://git.example\n", None,
])
def test_server_rejects_ambiguous_or_non_origin_input(url):
    with pytest.raises(provider.GitProviderError):
        provider.normalize_server_url(url)


def test_repository_requires_exact_origin():
    assert provider.repository_on_server("https://git.example", "https://GIT.example:443/team/repo.git/") == "https://git.example/team/repo.git"
    for url in ("https://sub.git.example/team/repo", "https://git.example:8443/team/repo",
                "https://git.example.evil/team/repo", "http://git.example/team/repo",
                "https://secret@git.example/team/repo", "https://git.example/team/../repo"):
        with pytest.raises(provider.GitProviderError):
            provider.repository_on_server("https://git.example", url)


class Response(BytesIO):
    status = 200

    def __init__(self, rows, headers=None, raw=None):
        super().__init__(json.dumps(rows).encode() if raw is None else raw)
        self.headers = headers or {}


def mock_http(monkeypatch, response):
    observed = {}

    class Opener:
        def open(self, request, timeout):
            observed.update(request=request, timeout=timeout)
            if isinstance(response, Exception):
                raise response
            return response

    def build(*handlers):
        observed["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(provider, "build_opener", build)
    return observed


def test_gitlab_request_and_safe_projection(monkeypatch):
    observed = mock_http(monkeypatch, Response([{
        "name": "Repo", "path_with_namespace": "team/repo",
        "http_url_to_repo": "https://git.example/team/repo.git", "secret_extra": "discard",
    }], {"X-Next-Page": "3"}))
    result = provider.list_repositories("gitlab", "https://git.example", "synthetic-token", 2)
    assert result == {"items": [{"name": "Repo", "path": "team/repo", "git_url": "https://git.example/team/repo.git"}], "page": 2, "has_next": True}
    request = observed["request"]
    assert urlsplit(request.full_url).path == "/api/v4/projects"
    assert parse_qs(urlsplit(request.full_url).query) == {"membership": ["true"], "simple": ["true"], "per_page": ["30"], "page": ["2"]}
    assert request.get_header("Private-token") == "synthetic-token"
    assert "synthetic-token" not in request.full_url
    proxy = next(h for h in observed["handlers"] if isinstance(h, ProxyHandler))
    assert proxy.proxies == {}
    tls = next(h for h in observed["handlers"] if isinstance(h, HTTPSHandler))._context
    assert tls.verify_mode == ssl.CERT_REQUIRED and tls.check_hostname
    redirect = next(h for h in observed["handlers"] if isinstance(h, provider._NoRedirect))
    assert redirect.redirect_request(request, None, 302, "", {}, "https://elsewhere.example") is None


@pytest.mark.parametrize("origin,endpoint", [
    ("https://github.com", "https://api.github.com/user/repos"),
    ("https://gh.example:8443", "https://gh.example:8443/api/v3/user/repos"),
])
def test_github_endpoints_headers_and_untrusted_pagination(monkeypatch, origin, endpoint):
    observed = mock_http(monkeypatch, Response([{"name": "Repo", "full_name": "team/repo", "clone_url": origin + "/team/repo.git"}], {"Link": '<https://untrusted.example/?secret>; rel="next"'}))
    assert provider.list_repositories("github", origin, "synthetic-token")["has_next"]
    request = observed["request"]
    assert request.full_url.split("?")[0] == endpoint
    assert request.get_header("Authorization") == "Bearer synthetic-token"
    assert request.get_header("Accept") == "application/vnd.github+json"
    assert request.get_header("X-github-api-version") == "2022-11-28"


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 403, 500])
def test_errors_never_expose_upstream_details(monkeypatch, status):
    mock_http(monkeypatch, HTTPError("https://secret.example", status, "synthetic-token", {}, BytesIO(b"secret body")))
    with pytest.raises(provider.GitProviderError) as caught:
        provider.list_repositories("gitlab", "https://git.example", "synthetic-token")
    assert "synthetic-token" not in str(caught.value)
    assert "secret" not in str(caught.value)
    assert "HTTPS URL" in str(caught.value)
    assert ("права токена" in str(caught.value)) == (status in (401, 403))


@pytest.mark.parametrize("raw", [b"not-json", b"{}", b"[null]", b"[{}]", b"[" * 2000,
                                       b"x" * (provider.MAX_RESPONSE_BYTES + 1)])
def test_invalid_and_oversized_json_is_neutral(monkeypatch, raw):
    mock_http(monkeypatch, Response(None, raw=raw))
    with pytest.raises(provider.GitProviderError):
        provider.list_repositories("gitlab", "https://git.example", "synthetic-token")


def test_provider_response_cannot_offer_another_origin(monkeypatch):
    mock_http(monkeypatch, Response([{"name": "repo", "path_with_namespace": "repo", "http_url_to_repo": "https://other.example/repo.git"}]))
    with pytest.raises(provider.GitProviderError):
        provider.list_repositories("gitlab", "https://git.example", "synthetic-token")


@pytest.mark.parametrize("kind,token,page", [("other", "synthetic", 1), ("gitlab", "", 1), ("gitlab", "x\r\nInjected: y", 1), ("github", "x", 0), ("github", "x", True)])
def test_invalid_input_never_opens_network(monkeypatch, kind, token, page):
    def unexpected(*args):
        pytest.fail("unexpected network setup")
    monkeypatch.setattr(provider, "build_opener", unexpected)
    with pytest.raises(provider.GitProviderError):
        provider.list_repositories(kind, "https://git.example", token, page)


def test_network_failure_and_final_page(monkeypatch):
    mock_http(monkeypatch, URLError("synthetic-token"))
    with pytest.raises(provider.GitProviderError, match="вручную"):
        provider.list_repositories("gitlab", "https://git.example", "synthetic-token")
    mock_http(monkeypatch, Response([], {"X-Next-Page": ""}))
    assert provider.list_repositories("gitlab", "https://git.example", "synthetic-token") == {"items": [], "page": 1, "has_next": False}


def test_truncated_response_is_neutral(monkeypatch):
    mock_http(monkeypatch, IncompleteRead(b"synthetic-token"))
    with pytest.raises(provider.GitProviderError) as caught:
        provider.list_repositories("gitlab", "https://git.example", "synthetic-token")
    assert "synthetic-token" not in str(caught.value)
