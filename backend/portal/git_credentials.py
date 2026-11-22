"""Installation-local Git credentials; callers expose metadata, never this record.

The directory belongs to the installation and must be mounted outside the checkout
and database. Atomic replacement also works on Docker Desktop bind mounts.
"""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import stat
import tempfile
import unicodedata
from uuid import UUID


class CredentialError(ValueError):
    """Invalid credentials or an unavailable credential store (without secret data)."""


@dataclass(frozen=True)
class Credential:
    token: str | None = field(repr=False)
    username: str
    access: str


def _validate(credential: Credential) -> None:
    if not isinstance(credential, Credential):
        raise CredentialError("Invalid Git credentials")
    if credential.access not in ("git", "api"):
        raise CredentialError("Git access must be git or api")
    username = credential.username
    if (not isinstance(username, str) or not username.strip() or len(username) > 1024
            or ":" in username or any(unicodedata.category(c).startswith("C") for c in username)):
        raise CredentialError("Invalid Git username")
    token = credential.token
    if token is not None and (not isinstance(token, str) or not 1 <= len(token) <= 4096
                              or any(not 33 <= ord(c) <= 126 for c in token)):
        raise CredentialError("Invalid Git token")


def _filename(connection_id: str) -> str:
    try:
        if not isinstance(connection_id, str):
            raise ValueError
        canonical = str(UUID(connection_id))
        if connection_id.lower() != canonical:
            raise ValueError
        return canonical + ".json"
    except (ValueError, AttributeError, TypeError):
        raise CredentialError("Invalid Git server connection ID") from None


def _check_path(path: Path, *, directory: bool = False) -> os.stat_result:
    info = path.lstat()
    # Windows junctions are reparse points even when is_symlink() is false.
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)):
        raise CredentialError("Unsafe Git credential store path")
    return info


def _root(root: Path, *, create: bool) -> bool:
    if not root.is_absolute() or ".." in root.parts:
        raise CredentialError("Unsafe Git credential store path")
    try:
        info = _check_path(root, directory=True)
    except FileNotFoundError:
        if not create:
            return False
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = _check_path(root, directory=True)
    if os.name == "posix":
        if info.st_uid != os.geteuid():
            raise CredentialError("Unsafe Git credential store owner")
        if stat.S_IMODE(info.st_mode) != 0o700:
            if not create:
                raise CredentialError("Unsafe Git credential store permissions")
            os.chmod(root, 0o700)
            if stat.S_IMODE(_check_path(root, directory=True).st_mode) != 0o700:
                raise CredentialError("Unsafe Git credential store permissions")
    return True


def read_credential(root: Path, id: str) -> Credential | None:
    """Return the private record, or None when no record has been configured."""
    filename = _filename(id)
    try:
        root = Path(root)
        if not _root(root, create=False):
            return None
        path = root / filename
        try:
            before = _check_path(path)
        except FileNotFoundError:
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or (os.name != "posix" and (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino))):
                raise CredentialError("Unsafe Git credential store path")
            if os.name == "posix":
                if opened.st_uid != os.geteuid() or stat.S_IMODE(opened.st_mode) != 0o600:
                    raise CredentialError("Unsafe Git credential file permissions")
            # A bounded token and a practical username keep installation records small.
            payload = stream.read(32769)
            if len(payload) > 32768:
                raise CredentialError("Invalid Git credential record")
            data = json.loads(payload)
        if not isinstance(data, dict) or set(data) != {"token", "username", "access"}:
            raise CredentialError("Invalid Git credential record")
        credential = Credential(**data)
        _validate(credential)
        return credential
    except CredentialError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError):
        raise CredentialError("Cannot read Git credentials") from None


def write_credential(root: Path, id: str, credential: Credential) -> None:
    """Atomically create/rotate a record; token=None removes only the secret."""
    filename = _filename(id)
    _validate(credential)
    temporary = None
    try:
        root = Path(root)
        _root(root, create=True)
        target = root / filename
        try:
            _check_path(target)
        except FileNotFoundError:
            pass
        descriptor, temporary = tempfile.mkstemp(prefix=".credential-", dir=root)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            # mkstemp creates mode 0600; chmod is also needed on reused mounts.
            if os.name == "posix":
                os.fchmod(stream.fileno(), 0o600)
                info = os.fstat(stream.fileno())
                if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                    raise CredentialError("Unsafe Git credential file permissions")
            json.dump({"token": credential.token, "username": credential.username,
                       "access": credential.access}, stream, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
    except CredentialError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError):
        raise CredentialError("Cannot save Git credentials") from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
