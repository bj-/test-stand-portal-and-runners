"""Credential persistence uses synthetic values and isolated temporary stores."""

import os
import stat
from uuid import uuid4

import pytest

from portal import git_credentials as store


def test_persist_rotate_and_remove_secret_without_removing_metadata(tmp_path):
    root = tmp_path / "credentials"
    connection = str(uuid4())
    assert store.read_credential(root, connection) is None
    assert not root.exists()
    first = store.Credential("synthetic-one", "oauth2", "git")
    assert first.token not in repr(first)
    store.write_credential(root, connection, first)
    assert store.read_credential(root, connection) == first
    second = store.Credential("synthetic-two", "reader", "api")
    store.write_credential(root, connection, second)
    assert store.read_credential(root, connection) == second
    removed = store.Credential(None, "reader", "api")
    store.write_credential(root, connection, removed)
    assert store.read_credential(root, connection) == removed
    assert "synthetic-" not in (root / f"{connection}.json").read_text()
    assert len(list(root.iterdir())) == 1


@pytest.mark.parametrize("connection", ["../escaped", "", "x", uuid4().hex, None])
def test_reject_invalid_id_before_creating_store(tmp_path, connection):
    root = tmp_path / "credentials"
    with pytest.raises(store.CredentialError):
        store.write_credential(root, connection, store.Credential("synthetic", "oauth2", "git"))
    assert not root.exists()


@pytest.mark.parametrize("token,username,access", [
    ("", "oauth2", "git"), ("a b", "oauth2", "git"), ("a\r\nHeader:evil", "oauth2", "api"),
    ("non-ascii-\u0430", "oauth2", "git"), ("x" * 4097, "oauth2", "git"),
    (42, "oauth2", "git"), ("synthetic", "", "git"), ("synthetic", "a:b", "git"),
    ("synthetic", "a\n", "git"), ("synthetic", "a\u0085", "git"),
    ("synthetic", "u" * 1025, "git"),
    ("synthetic", "oauth2", "shell"),
])
def test_invalid_credential_never_enters_store_or_error(tmp_path, token, username, access):
    with pytest.raises(store.CredentialError) as error:
        store.write_credential(tmp_path / "credentials", str(uuid4()),
                               store.Credential(token, username, access))
    assert "synthetic" not in str(error.value)
    assert not (tmp_path / "credentials").exists()


def test_failed_replace_preserves_previous_record_and_cleans_temp(tmp_path, monkeypatch):
    connection = str(uuid4())
    original = store.Credential("synthetic-one", "oauth2", "git")
    store.write_credential(tmp_path, connection, original)

    def fail(*args):
        raise OSError("synthetic-two must not escape")

    monkeypatch.setattr(store.os, "replace", fail)
    with pytest.raises(store.CredentialError) as error:
        store.write_credential(tmp_path, connection, store.Credential("synthetic-two", "oauth2", "git"))
    assert "synthetic-two" not in str(error.value)
    assert store.read_credential(tmp_path, connection) == original
    assert [p.name for p in tmp_path.iterdir()] == [f"{connection}.json"]


def test_malformed_record_has_sanitized_error(tmp_path):
    connection = str(uuid4())
    (tmp_path / f"{connection}.json").write_text('{"token":"synthetic-secret",BROKEN')
    with pytest.raises(store.CredentialError) as error:
        store.read_credential(tmp_path, connection)
    assert "synthetic-secret" not in str(error.value)


def test_reject_nonregular_credential_and_root(tmp_path):
    connection = str(uuid4())
    (tmp_path / f"{connection}.json").mkdir()
    for operation in (lambda: store.read_credential(tmp_path, connection),
                      lambda: store.write_credential(tmp_path, connection,
                                                     store.Credential("synthetic", "oauth2", "git"))):
        with pytest.raises(store.CredentialError):
            operation()
    bad_root = tmp_path / "file"
    bad_root.write_text("unrelated")
    with pytest.raises(store.CredentialError):
        store.read_credential(bad_root, connection)


@pytest.mark.parametrize("link_root", [False, True])
def test_reject_symlink_without_accessing_destination(tmp_path, link_root):
    connection = str(uuid4())
    destination = tmp_path / "outside"
    destination.mkdir()
    protected = destination / f"{connection}.json"
    protected.write_text("untouched")
    root = tmp_path / "credentials"
    try:
        if link_root:
            root.symlink_to(destination, target_is_directory=True)
        else:
            root.mkdir()
            (root / f"{connection}.json").symlink_to(protected)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    with pytest.raises(store.CredentialError):
        store.read_credential(root, connection)
    with pytest.raises(store.CredentialError):
        store.write_credential(root, connection, store.Credential("synthetic", "oauth2", "git"))
    assert protected.read_text() == "untouched"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_posix_permissions(tmp_path):
    root = tmp_path / "credentials"
    connection = str(uuid4())
    store.write_credential(root, connection, store.Credential("synthetic", "oauth2", "git"))
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / f"{connection}.json").stat().st_mode) == 0o600
