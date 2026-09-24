# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import errno
import io
import json
import shutil
import sqlite3
import threading
import time
import zipfile
from pathlib import Path

import faiss
import numpy as np
import pytest

import pave.metadb as metadb
from pave.config import get_cfg
from pave.data_dir_lease import (
    DATA_DIR_LOCK_NAME,
    DataDirInUseError,
    DataDirLease,
)
from pave.metadb import CatalogDB, COLLECTION_CONFIG_FIELDS
from pave.service import (
    create_collection,
    restore_archive,
)
from pave.stores.local import LocalStore
from utils import FakeEmbedder


def _archive_bytes(store: LocalStore) -> bytes:
    archive_path, tmp_dir = store.dump_archive()
    try:
        return Path(archive_path).read_bytes()
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _zip_tree(source_dir: Path, target_path: Path) -> bytes:
    with zipfile.ZipFile(target_path, "w") as archive:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file() and not path.name.endswith(("-wal", "-shm")):
                archive.write(path, path.relative_to(source_dir).as_posix())
    return target_path.read_bytes()


def _archive_tree(archive_bytes: bytes, root: Path) -> Path:
    root.mkdir()
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        archive.extractall(root)
    return root


def _set_openai_instance(key: str, *, dim: int) -> None:
    cfg = get_cfg()
    cfg.set(f"embedder.instances.{key}.type", "openai")
    cfg.set(f"embedder.instances.{key}.model", "bge-m3")
    cfg.set(f"embedder.instances.{key}.config.dim", dim)
    cfg.set(f"embedder.instances.{key}.config.base_url", "https://runtime")
    cfg.set(f"embedder.instances.{key}.api_key", "runtime-secret")


def _native_instance() -> dict:
    return {
        "type": "sbert",
        "model": "fake",
        "config": {"runtime": "auto"},
    }


def _openai_archive(tmp_path: Path) -> bytes:
    _set_openai_instance("archive-text", dim=4)
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder(dim=8))
    source.create_collection(
        "acme",
        "text",
        embedder_type="openai",
        embed_model="bge-m3",
        embedder_config={"dim": 4},
    )
    source.index_records(
        "acme",
        "text",
        "doc",
        [("0", "archive text", {})],
    )
    return _archive_bytes(source)


def _sentinel_store(path: Path) -> LocalStore:
    store = LocalStore(str(path), FakeEmbedder(dim=8))
    store.index_records(
        "live",
        "sentinel",
        "doc",
        [("0", "keep this", {})],
    )
    return store


def test_dump_archive_returns_zip(tmp_path):
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.create_collection("acme", "documents")
    store.index_records(
        "acme",
        "documents",
        "doc",
        [("0", "hello endpoint", {})],
    )

    with zipfile.ZipFile(io.BytesIO(_archive_bytes(store))) as zf:
        names = set(zf.namelist())
        assert "catalog.db" in names
        assert "t_acme/c_documents/collection.json" in names


def test_archive_omits_rejects_and_preserves_data_dir_lease(tmp_path):
    data_dir = tmp_path / "data"
    lease = DataDirLease(data_dir).acquire()
    store = None
    try:
        store = LocalStore(str(data_dir), FakeEmbedder())
        store.create_collection("acme", "documents")
        archive_bytes = _archive_bytes(store)

        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            assert DATA_DIR_LOCK_NAME not in archive.namelist()

        store.restore_archive(archive_bytes)
        with pytest.raises(DataDirInUseError):
            DataDirLease(data_dir).acquire()

        supplied_lock = io.BytesIO(archive_bytes)
        with zipfile.ZipFile(supplied_lock, "a") as archive:
            archive.writestr(DATA_DIR_LOCK_NAME, b"not an ownership grant")
        with pytest.raises(ValueError, match="ownership lock"):
            store.restore_archive(supplied_lock.getvalue())
    finally:
        try:
            if store is not None:
                store.close()
        finally:
            lease.close()


def test_dump_archive_allows_unroutable_vector_space(tmp_path):
    _set_openai_instance("archive-text", dim=4)
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder(dim=8))
    source.create_collection(
        "acme",
        "text",
        embedder_type="openai",
        embed_model="bge-m3",
        embedder_config={"dim": 4},
    )
    get_cfg().set("embedder.instances", {"native": _native_instance()})

    with zipfile.ZipFile(io.BytesIO(_archive_bytes(source))) as archive:
        assert "catalog.db" in archive.namelist()


def test_create_collection_uses_collection_lock(monkeypatch, temp_data_dir):
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    events: list[tuple[str, str, str]] = []

    class _SpyLock:
        def __init__(self, tenant: str, collection: str):
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
        "_collection_write_lock",
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

    store._flush_caches()

    assert key not in store._dbs
    assert key not in store._emb
    assert col_db._rconn is None
    assert col_db._wconn is None


def test_index_records_replaces_existing_doc_chunks(temp_data_dir):
    store = LocalStore(str(temp_data_dir), FakeEmbedder())

    store.index_records(
        "acme",
        "replace_me",
        "doc1",
        [
            ("0", "alpha one", {"kind": "old"}),
            ("1", "beta two", {"kind": "old"}),
            ("2", "gamma three", {"kind": "old"}),
        ],
    )
    store.index_records(
        "acme",
        "replace_me",
        "doc1",
        [
            ("0", "beta two", {"kind": "new"}),
        ],
    )

    assert len(store.list_chunks("acme", "replace_me", "doc1")) == 1
    backend = store._emb[("acme", "replace_me")]
    assert int(backend._index.ntotal) == 1
    hits = store.search("acme", "replace_me", "beta", k=5)
    assert len(hits) >= 1
    assert hits[0].id == "doc1::0"


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


def test_restore_archive_replaces_data_dir(tmp_path):
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    source.create_collection("acme", "restored")
    source.index_records(
        "acme",
        "restored",
        "doc",
        [("0", "restore me", {})],
    )
    archive_bytes = _archive_bytes(source)

    target = tmp_path / "target"
    target.mkdir()
    other = target / "other.txt"
    other.write_text("doomed", encoding="utf-8")
    store = LocalStore(str(target), FakeEmbedder())
    metrics_generation = store.metrics_generation()

    out = restore_archive(store, archive_bytes)
    assert out["ok"] is True
    assert store.search("acme", "restored", "restore", k=1)[0].id == "doc::0"
    assert not other.exists()
    assert store.metrics_generation() != metrics_generation


def test_restore_archive_migrates_preview_baseline(monkeypatch, tmp_path):
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    source.create_collection("acme", "restored")
    source.index_records(
        "acme",
        "restored",
        "doc",
        [("0", "restore me", {})],
    )
    archive_bytes = _archive_bytes(source)
    source.close()

    # Preview targets one past whatever is current, so this test keeps
    # working across real schema bumps instead of pinning stale numbers.
    preview_catalog = metadb._CATALOG_SCHEMA_VERSION + 1
    preview_collection = metadb._COLLECTION_SCHEMA_VERSION + 1
    monkeypatch.setattr(metadb, "_CATALOG_SCHEMA_VERSION", preview_catalog)
    monkeypatch.setattr(
        metadb,
        "_CATALOG_MIGRATIONS",
        {
            **metadb._CATALOG_MIGRATIONS,
            preview_catalog: [
                "CREATE TABLE catalog_preview_probe (value TEXT)"
            ],
        },
    )
    monkeypatch.setattr(
        metadb, "_COLLECTION_SCHEMA_VERSION", preview_collection
    )
    monkeypatch.setattr(
        metadb,
        "_COLLECTION_MIGRATIONS",
        {
            **metadb._COLLECTION_MIGRATIONS,
            preview_collection: [
                "CREATE TABLE collection_preview_probe (value TEXT)"
            ],
        },
    )

    target = tmp_path / "target"
    store = LocalStore(str(target), FakeEmbedder())
    store.restore_archive(archive_bytes)

    assert store.search("acme", "restored", "restore", k=1)[0].id == "doc::0"
    with sqlite3.connect(target / "catalog.db") as catalog:
        assert CatalogDB._schema_version(catalog) == preview_catalog
    with sqlite3.connect(
        target / "t_acme" / "c_restored" / "meta.db"
    ) as metadata:
        assert (
            metadb.CollectionDB._schema_version(metadata)
            == preview_collection
        )


@pytest.mark.parametrize(
    "member",
    ("catalog.db-wal", "t_acme/c_documents/meta.db-shm"),
)
def test_restore_archive_rejects_sqlite_sidecars_before_touch(tmp_path, member):
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    source.create_collection("acme", "documents")
    archive_buffer = io.BytesIO(_archive_bytes(source))
    with zipfile.ZipFile(archive_buffer, "a") as archive:
        archive.writestr(member, b"stale SQLite sidecar")
    destination = _sentinel_store(tmp_path / "destination")

    with pytest.raises(ValueError, match="SQLite sidecar"):
        destination.restore_archive(archive_buffer.getvalue())

    assert destination.search("live", "sentinel", "keep", k=1)[0].id == "doc::0"


def test_restore_archive_rejects_malformed_zip_before_touch(tmp_path):
    destination = _sentinel_store(tmp_path / "destination")

    with pytest.raises(ValueError, match="invalid archive"):
        destination.restore_archive(b"not a zip")

    assert destination.search("live", "sentinel", "keep", k=1)[0].id == "doc::0"


def test_archive_round_trip_preserves_mixed_vector_spaces(tmp_path, monkeypatch):
    _set_openai_instance("archive-text", dim=4)
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder(dim=8))
    source.create_collection(
        "acme",
        "raw",
        embedder_type="identity",
        embedder_config={"dim": 4},
    )
    source.index_records(
        "acme",
        "raw",
        "raw",
        [("0", "", {})],
        vectors=[[1.0, 0.0, 0.0, 0.0]],
    )
    source.create_collection(
        "acme",
        "text",
        embedder_type="openai",
        embed_model="bge-m3",
        embedder_config={"dim": 4},
    )
    source.index_records(
        "acme",
        "text",
        "text",
        [("0", "archive text", {})],
    )
    archive_bytes = _archive_bytes(source)

    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        names = set(archive.namelist())
        assert {
            "catalog.db",
            "t_acme/c_raw/collection.json",
            "t_acme/c_text/collection.json",
        } <= names
        for name in names:
            if not name.endswith("collection.json"):
                continue
            config = json.loads(archive.read(name))
            rendered = json.dumps(config)
            assert "runtime-secret" not in rendered
            assert "https://runtime" not in rendered
            assert "base_url" not in config["embedder_config"]

    destination = LocalStore(str(tmp_path / "destination"), FakeEmbedder(dim=8))
    factory = destination._embedder_factory
    monkeypatch.setattr(
        destination,
        "_embedder_factory",
        lambda *_args: pytest.fail("restore constructed an embedder"),
    )
    destination.restore_archive(archive_bytes)
    destination._embedder_factory = factory

    assert destination.search(
        "acme",
        "raw",
        "",
        query_vector=[1.0, 0.0, 0.0, 0.0],
    )[0].id == "raw::0"
    assert destination.search("acme", "text", "archive", k=1)[0].id == "text::0"


@pytest.mark.parametrize("routing", ("missing", "ambiguous"))
def test_restore_archive_validates_destination_before_touch(tmp_path, routing):
    archive_bytes = _openai_archive(tmp_path)
    cfg = get_cfg()
    if routing == "missing":
        cfg.set("embedder.instances", {"native": _native_instance()})
    else:
        cfg.set(
            "embedder.instances",
            {
                "native": _native_instance(),
                "openai-a": {
                    "type": "openai",
                    "model": "bge-m3",
                    "config": {"dim": 8},
                },
                "openai-b": {
                    "type": "openai",
                    "model": "bge-m3",
                    "config": {"dim": 16},
                },
            },
        )
    destination = _sentinel_store(tmp_path / "destination")

    with pytest.raises(ValueError) as exc_info:
        destination.restore_archive(archive_bytes)

    assert "openai:bge-m3:dim=4" in str(exc_info.value)
    assert "runtime-secret" not in str(exc_info.value)
    assert destination.search("live", "sentinel", "keep", k=1)[0].id == "doc::0"


def test_restore_archive_uses_compatible_destination_instance(tmp_path):
    archive_bytes = _openai_archive(tmp_path)
    cfg = get_cfg()
    cfg.set(
        "embedder.instances",
        {
            "native": _native_instance(),
            "destination-text": {
                "type": "openai",
                "model": "bge-m3",
                "config": {
                    "dim": 4,
                    "base_url": "https://destination-runtime",
                },
                "api_key": "destination-secret",
            },
        },
    )
    destination = _sentinel_store(tmp_path / "destination")

    destination.restore_archive(archive_bytes)

    restored = destination.get_collection_config("acme", "text")
    assert restored is not None
    assert restored["embedder_type"] == "openai"
    assert restored["embed_model"] == "bge-m3"
    assert restored["embedder_config"] == {"dim": 4}


def test_restore_archive_rejects_conflicting_sidecar_before_touch(tmp_path):
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    source.create_collection("acme", "documents")
    archive_root = _archive_tree(
        _archive_bytes(source),
        tmp_path / "archive",
    )
    sidecar_path = archive_root / "t_acme" / "c_documents" / "collection.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar["embed_model"] = "conflicting-model"
    sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
    archive_bytes = _zip_tree(archive_root, tmp_path / "conflict.zip")
    destination = _sentinel_store(tmp_path / "destination")

    with pytest.raises(ValueError, match="catalog and sidecar conflict"):
        destination.restore_archive(archive_bytes)

    assert destination.search("live", "sentinel", "keep", k=1)[0].id == "doc::0"


def test_restore_archive_rejects_orphan_collection_dir_before_touch(tmp_path):
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    source.create_collection("acme", "documents")
    archive_root = _archive_tree(
        _archive_bytes(source),
        tmp_path / "archive",
    )
    orphan = archive_root / "t_acme" / "c_orphan"
    orphan.mkdir()
    (orphan / "orphan.txt").write_text("orphan", encoding="utf-8")
    archive_bytes = _zip_tree(archive_root, tmp_path / "orphan.zip")
    destination = _sentinel_store(tmp_path / "destination")

    with pytest.raises(
        ValueError,
        match="missing metadata database: acme/orphan",
    ):
        destination.restore_archive(archive_bytes)

    assert destination.search("live", "sentinel", "keep", k=1)[0].id == "doc::0"


def test_restore_archive_rejects_invalid_collection_database_before_touch(tmp_path):
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    source.create_collection("acme", "documents")
    archive_root = _archive_tree(
        _archive_bytes(source),
        tmp_path / "archive",
    )
    (archive_root / "t_acme" / "c_documents" / "meta.db").write_bytes(
        b"not a database"
    )
    archive_bytes = _zip_tree(archive_root, tmp_path / "corrupt.zip")
    destination = _sentinel_store(tmp_path / "destination")

    with pytest.raises(
        ValueError,
        match="invalid archive collection metadata database: acme/documents",
    ):
        destination.restore_archive(archive_bytes)

    assert destination.search("live", "sentinel", "keep", k=1)[0].id == "doc::0"


@pytest.mark.parametrize(
    "name",
    (
        "faiss.index",
        "id_map.json",
        "mismatched-id-map",
        "wrong-rid",
        "wrong-metric",
    ),
)
def test_restore_archive_rejects_invalid_collection_index_before_touch(
    tmp_path,
    name,
):
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    source.create_collection("acme", "documents")
    source.index_records(
        "acme",
        "documents",
        "doc",
        [("0", "archive index", {})],
    )
    archive_root = _archive_tree(
        _archive_bytes(source),
        tmp_path / "archive",
    )
    index_dir = archive_root / "t_acme" / "c_documents" / "index"
    if name == "mismatched-id-map":
        id_map_path = index_dir / "id_map.json"
        id_map = json.loads(id_map_path.read_text(encoding="utf-8"))
        id_map["id_to_rid"]["0"] = "wrong"
        id_map_path.write_text(json.dumps(id_map), encoding="utf-8")
    elif name == "wrong-rid":
        id_map_path = index_dir / "id_map.json"
        id_map = json.loads(id_map_path.read_text(encoding="utf-8"))
        id_map["rid_to_id"] = {"wrong": 0}
        id_map["id_to_rid"] = {"0": "wrong"}
        id_map_path.write_text(json.dumps(id_map), encoding="utf-8")
    elif name == "wrong-metric":
        faiss_index_path = index_dir / "faiss.index"
        original = faiss.read_index(str(faiss_index_path))
        ids = faiss.vector_to_array(original.id_map)
        tampered = faiss.IndexIDMap2(faiss.IndexFlatL2(original.d))
        tampered.add_with_ids(
            np.zeros((len(ids), original.d), dtype="float32"), ids
        )
        faiss.write_index(tampered, str(faiss_index_path))
    else:
        (index_dir / name).write_bytes(b"not a valid index")
    archive_bytes = _zip_tree(archive_root, tmp_path / "corrupt.zip")
    destination = _sentinel_store(tmp_path / "destination")

    with pytest.raises(
        ValueError,
        match="invalid archive collection index: acme/documents",
    ):
        destination.restore_archive(archive_bytes)

    assert destination.search("live", "sentinel", "keep", k=1)[0].id == "doc::0"


def test_restore_archive_rejects_wrong_dimension_identity_index_before_touch(
    tmp_path,
):
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder(dim=8))
    source.create_collection(
        "acme",
        "raw",
        embedder_type="identity",
        embedder_config={"dim": 4},
    )
    source.index_records(
        "acme",
        "raw",
        "raw",
        [("0", "", {})],
        vectors=[[1.0, 0.0, 0.0, 0.0]],
    )
    archive_root = _archive_tree(
        _archive_bytes(source),
        tmp_path / "archive",
    )
    index_dir = archive_root / "t_acme" / "c_raw" / "index"
    faiss_index_path = index_dir / "faiss.index"
    original = faiss.read_index(str(faiss_index_path))
    ids = faiss.vector_to_array(original.id_map)
    tampered = faiss.IndexIDMap2(faiss.IndexFlatIP(original.d * 2))
    tampered.add_with_ids(
        np.zeros((len(ids), original.d * 2), dtype="float32"), ids
    )
    faiss.write_index(tampered, str(faiss_index_path))
    archive_bytes = _zip_tree(archive_root, tmp_path / "wrong-dim.zip")
    destination = _sentinel_store(tmp_path / "destination")

    with pytest.raises(
        ValueError,
        match="invalid archive collection index: acme/raw",
    ):
        destination.restore_archive(archive_bytes)

    assert destination.search("live", "sentinel", "keep", k=1)[0].id == "doc::0"


def test_restore_archive_rejects_wrong_dimension_openai_index_before_touch(
    tmp_path,
):
    _set_openai_instance("archive-text", dim=4)
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder(dim=8))
    source.create_collection(
        "acme",
        "text",
        embedder_type="openai",
        embed_model="bge-m3",
        embedder_config={"dim": 4},
    )
    source.index_records(
        "acme",
        "text",
        "text",
        [("0", "archive text", {})],
    )
    archive_root = _archive_tree(
        _archive_bytes(source),
        tmp_path / "archive",
    )
    index_dir = archive_root / "t_acme" / "c_text" / "index"
    faiss_index_path = index_dir / "faiss.index"
    original = faiss.read_index(str(faiss_index_path))
    ids = faiss.vector_to_array(original.id_map)
    tampered = faiss.IndexIDMap2(faiss.IndexFlatIP(original.d * 2))
    tampered.add_with_ids(
        np.zeros((len(ids), original.d * 2), dtype="float32"), ids
    )
    faiss.write_index(tampered, str(faiss_index_path))
    archive_bytes = _zip_tree(archive_root, tmp_path / "wrong-dim.zip")
    destination = _sentinel_store(tmp_path / "destination")

    with pytest.raises(
        ValueError,
        match="invalid archive collection index: acme/text",
    ):
        destination.restore_archive(archive_bytes)

    assert destination.search("live", "sentinel", "keep", k=1)[0].id == "doc::0"


def test_restore_archive_rejects_incomplete_collection_database_before_touch(
    tmp_path,
):
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    source.create_collection("acme", "documents")
    archive_root = _archive_tree(
        _archive_bytes(source),
        tmp_path / "archive",
    )
    metadata_path = archive_root / "t_acme" / "c_documents" / "meta.db"
    metadata_path.unlink()
    with sqlite3.connect(metadata_path) as metadata:
        metadata.execute(
            "CREATE TABLE schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        metadata.execute(
            "INSERT INTO schema_migrations (version, applied_at) "
            "VALUES (6, 'current')"
        )
    archive_bytes = _zip_tree(archive_root, tmp_path / "metadata.zip")
    destination = _sentinel_store(tmp_path / "destination")

    with pytest.raises(
        ValueError,
        match="invalid archive collection metadata database: acme/documents",
    ):
        destination.restore_archive(archive_bytes)

    assert destination.search("live", "sentinel", "keep", k=1)[0].id == "doc::0"


def test_restore_archive_rejects_incomplete_collection_schema_before_touch(
    tmp_path,
):
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    source.create_collection("acme", "documents")
    archive_root = _archive_tree(
        _archive_bytes(source),
        tmp_path / "archive",
    )
    metadata_path = archive_root / "t_acme" / "c_documents" / "meta.db"
    with sqlite3.connect(metadata_path) as metadata:
        metadata.execute("PRAGMA journal_mode=DELETE")
        metadata.execute(
            "ALTER TABLE documents RENAME COLUMN version TO missing_version"
        )
    archive_bytes = _zip_tree(archive_root, tmp_path / "schema.zip")
    destination = _sentinel_store(tmp_path / "destination")

    with pytest.raises(
        ValueError,
        match="invalid archive collection metadata database: acme/documents",
    ):
        destination.restore_archive(archive_bytes)

    assert destination.search("live", "sentinel", "keep", k=1)[0].id == "doc::0"


def test_restore_archive_rejects_uninitialized_catalog_before_touch(tmp_path):
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    source.create_collection("acme", "documents")
    archive_root = _archive_tree(
        _archive_bytes(source),
        tmp_path / "archive",
    )
    catalog_path = archive_root / "catalog.db"
    catalog_path.unlink()
    with sqlite3.connect(catalog_path) as catalog:
        catalog.execute("CREATE TABLE unrelated (value TEXT)")
    archive_bytes = _zip_tree(archive_root, tmp_path / "catalog.zip")
    destination = _sentinel_store(tmp_path / "destination")

    with pytest.raises(ValueError, match="invalid archive catalog"):
        destination.restore_archive(archive_bytes)

    assert destination.search("live", "sentinel", "keep", k=1)[0].id == "doc::0"


def test_restore_archive_rejects_incomplete_catalog_schema_before_touch(tmp_path):
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    source.create_collection("acme", "documents")
    archive_root = _archive_tree(
        _archive_bytes(source),
        tmp_path / "archive",
    )
    catalog_path = archive_root / "catalog.db"
    with sqlite3.connect(catalog_path) as catalog:
        catalog.execute("PRAGMA journal_mode=DELETE")
        catalog.execute("DROP TABLE query_home")
    archive_bytes = _zip_tree(archive_root, tmp_path / "catalog-schema.zip")
    destination = _sentinel_store(tmp_path / "destination")

    with pytest.raises(ValueError, match="invalid archive catalog"):
        destination.restore_archive(archive_bytes)

    assert destination.search("live", "sentinel", "keep", k=1)[0].id == "doc::0"


@pytest.mark.parametrize("repair", ("sidecar", "catalog"))
def test_restore_archive_repairs_one_missing_config_surface(tmp_path, repair):
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder(dim=8))
    source.create_collection(
        "acme",
        "vectors",
        embedder_type="identity",
        embedder_config={"dim": 4},
    )
    expected = {
        key: source.get_collection_config("acme", "vectors")[key]
        for key in COLLECTION_CONFIG_FIELDS
    }
    archive_root = _archive_tree(
        _archive_bytes(source),
        tmp_path / "archive",
    )
    if repair == "sidecar":
        (archive_root / "t_acme" / "c_vectors" / "collection.json").unlink()
    else:
        catalog = CatalogDB()
        catalog.open(archive_root / "catalog.db")
        catalog.unregister_collection("acme", "vectors")
        catalog.close()
    archive_bytes = _zip_tree(archive_root, tmp_path / f"{repair}.zip")
    destination = LocalStore(str(tmp_path / "destination"), FakeEmbedder(dim=8))

    destination.restore_archive(archive_bytes)

    restored = destination.get_collection_config("acme", "vectors")
    assert restored is not None
    assert {key: restored[key] for key in COLLECTION_CONFIG_FIELDS} == expected
    sidecar = json.loads(
        (
            tmp_path
            / "destination"
            / "t_acme"
            / "c_vectors"
            / "collection.json"
        ).read_text(encoding="utf-8")
    )
    assert sidecar == expected


def test_restore_archive_rolls_back_staged_install_failure(tmp_path, monkeypatch):
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    source.create_collection("acme", "restored")
    archive_bytes = _archive_bytes(source)
    destination_path = tmp_path / "destination"
    destination = _sentinel_store(destination_path)
    move = shutil.move

    def fail_staged_move(source_path, target_path, *args, **kwargs):
        source = Path(source_path)
        target = Path(target_path)
        if source.parent.name == "extracted" and source.name == "t_acme":
            target.mkdir()
            raise OSError("staged install failed")
        return move(source_path, target_path, *args, **kwargs)

    monkeypatch.setattr(shutil, "move", fail_staged_move)
    with pytest.raises(OSError, match="staged install failed"):
        destination.restore_archive(archive_bytes)

    destination.close()
    recovered = LocalStore(str(destination_path), FakeEmbedder(dim=8))
    assert recovered.search("live", "sentinel", "keep", k=1)[0].id == "doc::0"
    assert not (destination_path / "t_acme" / "c_restored").exists()
    assert not list(destination_path.glob(".pavedb-restore-backup-*"))
    recovered.close()


def test_restore_archive_detects_incomplete_rollback(tmp_path, monkeypatch):
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    source.create_collection("acme", "restored")
    archive_bytes = _archive_bytes(source)
    destination_path = tmp_path / "destination"
    destination = _sentinel_store(destination_path)
    move = shutil.move
    state = {"staged": False, "rollback": False}

    def fail_restore_move(source_path, target_path, *args, **kwargs):
        source = Path(source_path)
        target = Path(target_path)
        if source.parent.name == "extracted" and source.name == "t_acme":
            state["staged"] = True
            target.mkdir()
            raise OSError("staged install failed")
        if (
            state["staged"]
            and not state["rollback"]
            and source.parent.name.startswith(".pavedb-restore-backup-")
        ):
            state["rollback"] = True
            raise OSError("rollback failed")
        return move(source_path, target_path, *args, **kwargs)

    monkeypatch.setattr(shutil, "move", fail_restore_move)
    with pytest.raises(RuntimeError, match="rollback is incomplete"):
        destination.restore_archive(archive_bytes)

    assert state == {"staged": True, "rollback": True}
    assert list(destination_path.glob(".pavedb-restore-backup-*"))
    destination.close()
    with pytest.raises(RuntimeError, match="interrupted archive restore"):
        LocalStore(str(destination_path), FakeEmbedder())


def test_dump_archive_skips_conflicting_collection_config(tmp_path):
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.create_collection("acme", "documents")
    sidecar_path = tmp_path / "t_acme" / "c_documents" / "collection.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar["embed_model"] = "conflicting-model"
    sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")

    skipped: list[str] = []
    path, tmp = store.dump_archive(skipped=skipped)
    shutil.rmtree(tmp)
    assert len(skipped) == 1
    assert skipped[0].startswith("acme/documents: ")
    assert "catalog and sidecar conflict" in skipped[0]


def test_dump_archive_skips_a_collection_failing_with_os_error(
    tmp_path, monkeypatch
):
    """Any per-collection failure is skipped, not only ValueError (B83)."""
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.index_records("acme", "ok", "doc", [("0", "keep", {})])
    store.index_records("acme", "denied", "doc", [("0", "lost", {})])
    original = LocalStore._archive_sidecar_config

    def sidecar(self, collection_dir, tenant, collection):
        if collection == "denied":
            raise PermissionError(13, "Permission denied")
        return original(self, collection_dir, tenant, collection)

    monkeypatch.setattr(LocalStore, "_archive_sidecar_config", sidecar)
    skipped: list[str] = []
    path, tmp = store.dump_archive(skipped=skipped)
    shutil.rmtree(tmp)
    assert [entry.split(":", 1)[0] for entry in skipped] == ["acme/denied"]
    assert "PermissionError" in skipped[0]


def test_dump_archive_skips_an_unreadable_meta_db(tmp_path):
    """One damaged collection must not block the whole backup (B83)."""
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    source.index_records("acme", "healthy", "doc", [("0", "keep me", {})])
    source.index_records("beta", "damaged", "doc", [("0", "lost", {})])
    source.close()
    meta = tmp_path / "source" / "t_beta" / "c_damaged" / "meta.db"
    meta.write_bytes(meta.read_bytes()[:100])

    # Restore of that same tree stays fail-closed.
    live_tree = _zip_tree(tmp_path / "source", tmp_path / "live.zip")
    with pytest.raises(ValueError, match="metadata database: beta/damaged"):
        _sentinel_store(tmp_path / "strict").restore_archive(live_tree)

    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    skipped: list[str] = []
    path, tmp = source.dump_archive(skipped=skipped)
    try:
        archive_bytes = Path(path).read_bytes()
    finally:
        shutil.rmtree(tmp)
    assert [entry.split(":", 1)[0] for entry in skipped] == ["beta/damaged"]
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        names = archive.namelist()
    assert not any(name.startswith("t_beta/c_damaged") for name in names)

    # The partial archive restores: its catalog does not name the gap.
    destination = LocalStore(str(tmp_path / "restored"), FakeEmbedder())
    destination.restore_archive(archive_bytes)
    assert destination.search("acme", "healthy", "keep", k=1)[0].id == "doc::0"
    assert destination.get_collection_config("beta", "damaged") is None


def test_new_collection_persists_an_empty_index_pair(tmp_path):
    """No window where a collection dir exists without its index (B85)."""
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.create_collection("acme", "fresh")
    index_dir = tmp_path / "t_acme" / "c_fresh" / "index"
    assert (index_dir / "faiss.index").is_file()
    assert (index_dir / "id_map.json").is_file()


def test_dump_archive_accepts_an_empty_collection_without_index(tmp_path):
    """Pre-1.0 builds could leave this behind after a hard kill (B85)."""
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.create_collection("acme", "fresh")
    store.close()
    shutil.rmtree(tmp_path / "t_acme" / "c_fresh" / "index")

    store = LocalStore(str(tmp_path), FakeEmbedder())
    assert not (tmp_path / "t_acme" / "c_fresh" / "index").exists()
    skipped: list[str] = []
    path, tmp = store.dump_archive(skipped=skipped)
    shutil.rmtree(tmp)
    assert skipped == []


def test_dump_archive_skips_a_half_index_pair(tmp_path):
    """Only a wholly absent pair is an empty legacy collection (B85)."""
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.create_collection("acme", "half")
    store.close()
    (tmp_path / "t_acme" / "c_half" / "index" / "id_map.json").unlink()

    store = LocalStore(str(tmp_path), FakeEmbedder())
    skipped: list[str] = []
    path, tmp = store.dump_archive(skipped=skipped)
    shutil.rmtree(tmp)
    assert len(skipped) == 1
    assert "missing id_map.json: acme/half" in skipped[0]


def test_dump_archive_preserves_another_catalog_connection_wal(tmp_path):
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.create_collection("acme", "documents")
    other_catalog = CatalogDB()
    other_catalog.open(tmp_path / "catalog.db")
    try:
        other_catalog.put_query_home("other-process-query", "acme", "documents")
        wal_path = tmp_path / "catalog.db-wal"
        shm_path = tmp_path / "catalog.db-shm"
        sidecar_inodes = (wal_path.stat().st_ino, shm_path.stat().st_ino)

        archive_bytes = _archive_bytes(store)

        assert (wal_path.stat().st_ino, shm_path.stat().st_ino) == sidecar_inodes
        archived_catalog = tmp_path / "archived-catalog.db"
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            archived_catalog.write_bytes(archive.read("catalog.db"))
        with sqlite3.connect(archived_catalog) as conn:
            row = conn.execute(
                "SELECT tenant, collection FROM query_home WHERE query_id=?",
                ("other-process-query",),
            ).fetchone()
        assert row == ("acme", "documents")
    finally:
        other_catalog.close()


def test_local_store_rejects_interrupted_restore_backup(tmp_path):
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.close()
    (tmp_path / ".pavedb-restore-backup-leftover").mkdir()

    with pytest.raises(RuntimeError, match="interrupted archive restore"):
        LocalStore(str(tmp_path), FakeEmbedder())


def test_restore_archive_waits_for_active_collection_operation(tmp_path):
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    source.create_collection("acme", "restored")
    archive_bytes = _archive_bytes(source)
    target = tmp_path / "target"
    store = LocalStore(str(target), FakeEmbedder())

    entered = threading.Event()
    release = threading.Event()
    restored = threading.Event()

    def hold_collection():
        with store._collection_write_lock("acme", "busy"):
            entered.set()
            release.wait(timeout=2.0)

    def restore():
        store.restore_archive(archive_bytes)
        restored.set()

    holder = threading.Thread(target=hold_collection, daemon=True)
    holder.start()
    assert entered.wait(timeout=1.0)

    restorer = threading.Thread(target=restore, daemon=True)
    restorer.start()
    time.sleep(0.1)
    assert not restored.is_set()

    release.set()
    holder.join(timeout=2.0)
    restorer.join(timeout=2.0)

    assert restored.is_set()
    assert (target / "t_acme" / "c_restored" / "meta.db").is_file()


def test_restore_archive_waits_for_active_collection_read(tmp_path):
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    source.create_collection("acme", "restored")
    archive_bytes = _archive_bytes(source)
    target = tmp_path / "target"
    store = LocalStore(str(target), FakeEmbedder())

    entered = threading.Event()
    release = threading.Event()
    restored = threading.Event()

    def hold_collection_read():
        with store._collection_read_lock("acme", "busy"):
            entered.set()
            release.wait(timeout=2.0)

    def restore():
        store.restore_archive(archive_bytes)
        restored.set()

    holder = threading.Thread(target=hold_collection_read, daemon=True)
    holder.start()
    assert entered.wait(timeout=1.0)

    restorer = threading.Thread(target=restore, daemon=True)
    restorer.start()
    time.sleep(0.1)
    assert not restored.is_set()

    release.set()
    holder.join(timeout=2.0)
    restorer.join(timeout=2.0)

    assert restored.is_set()
    assert (target / "t_acme" / "c_restored" / "meta.db").is_file()


def test_dump_archive_waits_for_active_collection_operation(tmp_path):
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.index_records(
        "acme",
        "busy",
        "doc1",
        [("0", "archive consistency probe", {"kind": "test"})],
    )

    entered = threading.Event()
    release = threading.Event()
    dumped = threading.Event()
    archive_path: list[str] = []
    tmp_dir: list[str | None] = []

    def hold_collection():
        with store._collection_write_lock("acme", "busy"):
            entered.set()
            release.wait(timeout=2.0)

    def dump():
        path, tmp = store.dump_archive()
        archive_path.append(path)
        tmp_dir.append(tmp)
        dumped.set()

    holder = threading.Thread(target=hold_collection, daemon=True)
    holder.start()
    assert entered.wait(timeout=1.0)

    dumper = threading.Thread(target=dump, daemon=True)
    dumper.start()
    time.sleep(0.1)
    assert not dumped.is_set()

    release.set()
    holder.join(timeout=2.0)
    dumper.join(timeout=2.0)

    try:
        assert dumped.is_set()
        with zipfile.ZipFile(archive_path[0]) as zf:
            assert "t_acme/c_busy/meta.db" in set(zf.namelist())
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir[0], ignore_errors=True)


def _bomb_zip(path: Path) -> bytes:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("catalog.db", b"\0" * (8 << 20))
    return path.read_bytes()


def test_restore_refuses_an_archive_that_outgrows_the_disk(tmp_path, monkeypatch):
    """A high-ratio archive must be refused before extraction (B89)."""
    bomb = _bomb_zip(tmp_path / "bomb.zip")
    assert len(bomb) < 64 << 10
    destination = _sentinel_store(tmp_path / "destination")
    real_usage = shutil.disk_usage

    def tight(path):
        return real_usage(path)._replace(free=4 << 20)

    monkeypatch.setattr(shutil, "disk_usage", tight)
    with pytest.raises(ValueError, match="archive expands to"):
        destination.restore_archive(bomb)
    with pytest.raises(ValueError, match="archive expands to"):
        destination.restore_collection_archive("live", "other", bomb)
    monkeypatch.undo()
    assert destination.search("live", "sentinel", "keep", k=1)[0].id == "doc::0"
