# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
import stat
from pathlib import Path

import pytest
from utils import FakeEmbedder

import pave.cli as cli_mod
import pave.main as main_mod
from pave.data_dir_lease import DataDirLease
from pave.localclient import LocalClient
from pave.metadb import CatalogDB, CollectionDB
from pave.stores.local import LocalStore

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")


@pytest.fixture(params=[0o000, 0o002, 0o022, 0o077])
def creation_umask(request):
    previous = os.umask(request.param)
    try:
        yield request.param
    finally:
        os.umask(previous)


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.parametrize("database", [CatalogDB, CollectionDB])
def test_sqlite_creation_is_private(tmp_path, creation_umask, database):
    # A traversable ancestor must not expose any new database or its side files.
    tmp_path.chmod(0o755)
    path = tmp_path / "instance" / "data" / "metadata.db"
    db = database()
    try:
        db.open(path)
        assert _mode(path.parent.parent) == 0o700
        assert _mode(path.parent) == 0o700
        for suffix in ("", "-wal", "-shm"):
            assert _mode(Path(f"{path}{suffix}")) == 0o600
        # No process-global umask change, even after SQLite creates side files.
        assert os.umask(creation_umask) == creation_umask
    finally:
        db.close()


def test_init_credentials_and_directories_are_private(
    tmp_path, creation_umask, capsys,
):
    tmp_path.chmod(0o755)
    home = tmp_path / "instance"
    tenants = tmp_path / "secrets" / "tenants.yml"
    assert cli_mod.main_cli([
        "--tenants", str(tenants), "init", str(home),
    ]) == 0
    capsys.readouterr()
    for directory in (home, home / "data", tenants.parent):
        assert _mode(directory) == 0o700
    for credential in (home / "config.yml", tenants):
        assert _mode(credential) == 0o600


@pytest.mark.parametrize("entrypoint", ["server", "cli", "embedded"])
def test_runtime_creates_private_data_root(
    tmp_path, creation_umask, entrypoint, monkeypatch, capsys,
):
    tmp_path.chmod(0o755)
    home = tmp_path / "instance"
    data_dir = home / "data"
    if entrypoint == "server":
        monkeypatch.setattr(main_mod, "_run_pave_server", lambda *_a, **_kw: None)
        monkeypatch.setattr(main_mod, "enforce_policy", lambda _cfg: None)
        monkeypatch.setattr(
            main_mod, "resolve_bind", lambda _cfg: ("127.0.0.1", 8086),
        )
        main_mod.main_srv(["--home", str(home)])
    elif entrypoint == "cli":
        try:
            assert cli_mod.main_cli([
                "--data-dir", str(data_dir), "list-tenants",
            ]) == 0
        finally:
            cli_mod._close_owned_store()
    else:
        with LocalClient(data_dir) as client:
            client.create_collection("books")
    capsys.readouterr()
    assert _mode(home) == 0o700
    assert _mode(data_dir) == 0o700
    assert _mode(data_dir / ".pavedb.lock") == 0o600
    if entrypoint != "server":
        assert _mode(data_dir / "catalog.db") == 0o600


def test_existing_installation_permissions_are_preserved(tmp_path, caplog):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    data_dir.chmod(0o750)
    db_path = data_dir / "catalog.db"
    db_path.touch()
    db_path.chmod(0o640)
    with DataDirLease(data_dir):
        db = CatalogDB()
        try:
            db.open(db_path)
            assert _mode(data_dir) == 0o750
            assert _mode(db_path) == 0o640
        finally:
            db.close()
    assert "existing permissions are unchanged" in caplog.text


def test_restore_keeps_private_data_boundary(tmp_path, creation_umask):
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    source.index_records("acme", "books", "doc", [("0", "private text", {})])
    archive, _ = source.dump_archive(tmp_path / "backup.zip")
    target = tmp_path / "target"
    store = LocalStore(str(target), FakeEmbedder())
    store.restore_archive(Path(archive).read_bytes())
    assert _mode(target) == 0o700
    assert store.search("acme", "books", "private", k=1)[0].id == "doc::0"


@pytest.mark.parametrize("archive_kind", ["instance", "collection"])
def test_restore_privatises_new_content_under_existing_data_root(
    tmp_path, creation_umask, archive_kind,
):
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    source.index_records("acme", "books", "doc", [("0", "private text", {})])
    if archive_kind == "instance":
        archive, _ = source.dump_archive(tmp_path / "backup.zip")
    else:
        archive, _ = source.dump_collection_archive(
            "acme", "books", tmp_path / "backup.zip",
        )
    target = tmp_path / "target"
    store = LocalStore(str(target), FakeEmbedder())
    target.chmod(0o755)
    if archive_kind == "instance":
        store.restore_archive(Path(archive).read_bytes())
    else:
        store.restore_collection_archive(
            "acme", "books", Path(archive).read_bytes(),
        )
    assert _mode(target) == 0o755
    for path in target.rglob("*"):
        assert _mode(path) == (0o700 if path.is_dir() else 0o600), path
    assert store.search("acme", "books", "private", k=1)[0].id == "doc::0"
