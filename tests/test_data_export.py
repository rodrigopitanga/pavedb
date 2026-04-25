# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import errno
import io
import shutil
import threading
import time
import zipfile
from pathlib import Path

from pave.service import (
    create_collection,
    dump_archive,
    restore_archive,
)
from pave.stores.local import LocalStore
from utils import FakeEmbedder


def test_dump_archive_returns_zip(temp_data_dir):
    sample = Path(temp_data_dir) / "tenant" / "collection" / "doc.txt"
    sample.parent.mkdir(parents=True, exist_ok=True)
    sample.write_text("hello endpoint", encoding="utf-8")

    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    archive_path, tmp_dir = dump_archive(store)
    try:
        response_bytes = Path(archive_path).read_bytes()
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    buffer = io.BytesIO(response_bytes)
    with zipfile.ZipFile(buffer) as zf:
        names = set(zf.namelist())
        assert "tenant/collection/doc.txt" in names
        with zf.open("tenant/collection/doc.txt") as f:
            assert f.read().decode("utf-8") == "hello endpoint"


def test_lock_indexes_blocks_new_collection_lock(temp_data_dir):
    tenant_dir = Path(temp_data_dir) / "t_acme"
    collection_dir = tenant_dir / "c_invoices"
    collection_dir.mkdir(parents=True, exist_ok=True)

    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    start = threading.Event()
    release = threading.Event()
    acquired = threading.Event()

    def hold_all_locks() -> None:
        with store._lock_all():
            start.set()
            release.wait(timeout=2.0)

    def try_new_lock() -> None:
        with store._collection_lock("acme", "new_collection"):
            acquired.set()

    holder = threading.Thread(target=hold_all_locks, daemon=True)
    holder.start()
    assert start.wait(timeout=1.0)

    contender = threading.Thread(target=try_new_lock, daemon=True)
    contender.start()
    time.sleep(0.1)
    assert not acquired.is_set()

    release.set()
    holder.join(timeout=2.0)
    contender.join(timeout=2.0)
    assert acquired.is_set()


def test_create_collection_uses_collection_lock(monkeypatch, temp_data_dir):
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    events: list[tuple[str, str, str]] = []

    class _SpyLock:
        def __init__(self, tenant: str, collection: str) -> None:
            self.tenant = tenant
            self.collection = collection

        def __enter__(self):
            events.append(("enter", self.tenant, self.collection))
            return self

        def __exit__(self, exc_type, exc, tb):
            events.append(("exit", self.tenant, self.collection))
            return False

    def fake_collection_lock(tenant: str, collection: str):
        return _SpyLock(tenant, collection)

    monkeypatch.setattr(
        store,
        "_collection_lock",
        fake_collection_lock,
    )

    out = create_collection(store, "acme", "locked")
    assert out["ok"] is True
    assert ("enter", "acme", "locked") in events
    assert ("exit", "acme", "locked") in events


def test_flush_store_caches_closes_old_dbs_sync(temp_data_dir):
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    store.index_records(
        "acme",
        "flush_test",
        "doc1",
        [("0", "flush probe", {"lang": "en"})],
    )
    key = ("acme", "flush_test")
    col_db = store._dbs[key]
    assert col_db._rconn is not None

    store._flush_caches(async_close=False)

    assert key not in store._dbs
    assert key not in store._emb
    assert col_db._rconn is None
    assert col_db._wconn is None


def test_remove_path_retries_transient_errors(monkeypatch, tmp_path):
    target = tmp_path / "to_remove"
    target.mkdir()
    (target / "f.txt").write_text("x", encoding="utf-8")

    calls = {"n": 0}
    real_rmtree = shutil.rmtree

    def flaky_rmtree(path, *args, **kwargs):
        if Path(path) == target and calls["n"] < 2:
            calls["n"] += 1
            raise OSError(errno.ENOTEMPTY, "directory not empty")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", flaky_rmtree)

    LocalStore._remove_path(target)
    assert calls["n"] == 2
    assert not target.exists()


def test_restore_archive_replaces_data_dir(temp_data_dir):
    sample = Path(temp_data_dir) / "tenant" / "collection" / "doc.txt"
    sample.parent.mkdir(parents=True, exist_ok=True)
    sample.write_text("restore me", encoding="utf-8")

    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    archive_path, tmp_dir = dump_archive(store)
    try:
        archive_bytes = Path(archive_path).read_bytes()
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    shutil.rmtree(temp_data_dir)
    Path(temp_data_dir).mkdir(parents=True, exist_ok=True)
    other = Path(temp_data_dir) / "other.txt"
    other.write_text("doomed", encoding="utf-8")

    out = restore_archive(store, archive_bytes)
    assert out["ok"] is True
    assert sample.exists()
    assert sample.read_text(encoding="utf-8") == "restore me"
    assert not other.exists()
