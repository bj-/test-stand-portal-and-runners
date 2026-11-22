"""Canonical HTTPS Git sources for the stand catalogue."""

import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

from .stand_contract import ID


class GitSourceError(ValueError):
    """A neutral, credential-free error suitable for the API response."""


_INVALID_URL = "Некорректный HTTPS URL Git-репозитория"


def parse_git_url(url: str) -> tuple[str, str, str]:
    """Return canonical HTTPS URL, display slug, and a portable contract ID.

    Paths deliberately use literal ASCII segments: percent encodings, URL
    credentials and ambiguous traversal spellings are not accepted.
    Internal DNS names and explicit HTTPS ports are supported.
    """
    try:
        if (not isinstance(url, str) or not url or len(url) > 2048
                or not url.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in url)
                or any(c in url for c in "\\%?#")):
            raise ValueError
        parsed = urlsplit(url)
        if parsed.scheme.lower() != "https" or not parsed.netloc or "@" in parsed.netloc:
            raise ValueError
        host = parsed.hostname
        port = parsed.port
        if not host or (port is not None and not 1 <= port <= 65535):
            raise ValueError
        host = host.lower().rstrip(".")
        if ":" in host:
            host = "[" + str(ipaddress.IPv6Address(host)) + "]"
        else:
            if (len(host) > 253 or not all(re.fullmatch(
                    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                    for label in host.split("."))):
                raise ValueError
        # Reject an empty port (urlsplit otherwise treats it like no port).
        if parsed.netloc.endswith(":"):
            raise ValueError
        authority = host + (f":{port}" if port is not None and port != 443 else "")
        path = parsed.path.removesuffix("/")
        parts = path.split("/")
        if (not path.startswith("/") or len(parts) < 2
                or any(not re.fullmatch(r"[A-Za-z0-9_.~-]+", part)
                       or part in (".", "..") for part in parts[1:])):
            raise ValueError
        slug = parts[-1]
        if slug.lower().endswith(".git"):
            slug = slug[:-4]
        if not slug or slug in (".", ".."):
            raise ValueError
        key = re.sub(r"[^a-z0-9_.-]+", "-", slug.lower())
        if not key or not key[0].isalpha():
            key = "repo-" + key
        key = key[:128]
        if not ID.fullmatch(key):
            raise ValueError
        return urlunsplit(("https", authority, path, "", "")), slug, key
    except (ValueError, TypeError, UnicodeError):
        raise GitSourceError(_INVALID_URL) from None
