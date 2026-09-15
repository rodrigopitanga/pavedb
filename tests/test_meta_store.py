# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

import pave.metadb as metadb
from pave.metadb import (
    COLLECTION_CONFIG_FILENAME,
    CatalogDB,
    CollectionDB,
    LegacyMetadataError,
    ReindexJobConflictError,
    SchemaMigrationError,
    UnsupportedSchemaVersionError,
)


def _meta_db(tmp_path: Path) -> Path:
    return tmp_path / "t_acme" / "c_demo" / "meta.db"


def _catalog_db(tmp_path: Path) -> Path:
    return tmp_path / "catalog.db"


def test_open_creates_schema(tmp_path):
    db_path = _meta_db(tmp_path)
    db = CollectionDB()
    db.open(db_path)
    conn = db._conn
    assert conn is not None
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN "
        "('schema_migrations', 'documents', 'chunks', "
        "'chunk_meta', 'document_meta', 'query_log')"
    )
    names = {row[0] for row in cur.fetchall()}
    assert {
        "schema_migrations",
        "documents",
        "chunks",
        "chunk_meta",
        "document_meta",
        "query_log",
    } <= names
    version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    assert version == (6,)
    db.close()


def test_catalog_db_open_creates_schema(tmp_path):
    db = CatalogDB()
    db.open(_catalog_db(tmp_path))
    conn = db._conn
    assert conn is not None
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ("
        "'schema_migrations', 'collections', 'query_home', "
        "'metrics_state', 'metrics_counters', 'metrics_latency_samples', "
        "'rate_limit_buckets', 'reindex_jobs'"
        ")"
    )
    names = {row[0] for row in cur.fetchall()}
    assert {
        "schema_migrations",
        "collections",
        "query_home",
        "metrics_state",
        "metrics_counters",
        "metrics_latency_samples",
        "rate_limit_buckets",
        "reindex_jobs",
    } <= names
    version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    assert version == (7,)
    db.close()


def test_catalog_reindex_jobs_are_durable_and_single_flight(tmp_path):
    path = _catalog_db(tmp_path)
    db = CatalogDB()
    db.open(path)
    source = {
        "backend_type": "faiss",
        "backend_config": {},
        "embedder_type": "sbert",
        "embed_model": "old",
        "embedder_config": {},
    }
    target = {**source, "embed_model": "new"}

    created = db.create_reindex_job(
        "job-1",
        "acme",
        "documents",
        source,
        target,
    )
    assert created["status"] == "queued"
    with pytest.raises(ReindexJobConflictError):
        db.create_reindex_job(
            "job-2",
            "acme",
            "documents",
            source,
            target,
        )
    assert db.update_reindex_job(
        "job-1",
        expected_statuses=("queued",),
        status="running",
        total_chunks=3,
        completed_chunks=1,
        set_started=True,
    )
    db.interrupt_reindex_jobs()
    db.close()

    db.open(path)
    interrupted = db.get_reindex_job("job-1")
    assert interrupted is not None
    assert interrupted["status"] == "paused"
    assert interrupted["completed_chunks"] == 1
    assert db.update_reindex_job(
        "job-1",
        expected_statuses=("paused",),
        status="cancelled",
        set_finished=True,
    )
    assert db.create_reindex_job(
        "job-2",
        "acme",
        "documents",
        source,
        target,
    )["status"] == "queued"
    db.close()


def test_catalog_rate_limit_persists_and_smooths_window_boundary(tmp_path):
    path = _catalog_db(tmp_path)
    db = CatalogDB()
    db.open(path)

    first = db.charge_rate_limit("acme", "global", {60: 2}, now=120.0)
    second = db.charge_rate_limit("acme", "global", {60: 2}, now=120.0)
    assert first.allowed is True
    assert first.remaining == 1
    assert second.allowed is True
    assert second.remaining == 0
    db.close()

    db.open(path)
    assert db.charge_rate_limit(
        "acme", "global", {60: 2}, now=120.0
    ).allowed is False
    boundary = db.charge_rate_limit(
        "acme", "global", {60: 2}, now=180.0
    )
    assert boundary.allowed is False
    assert boundary.retry_after == 1
    assert db.charge_rate_limit(
        "acme", "global", {60: 2}, now=181.0
    ).allowed is True
    db.close()


def test_catalog_rate_limit_charges_enabled_windows_atomically(tmp_path):
    db = CatalogDB()
    db.open(_catalog_db(tmp_path))

    assert db.charge_rate_limit(
        "acme", "global", {60: 5, 3600: 1}, now=120.0
    ).allowed is True
    denied = db.charge_rate_limit(
        "acme", "global", {60: 5, 3600: 1}, now=120.0
    )

    assert denied.allowed is False
    assert denied.limit == 1
    assert denied.window_seconds == 3600
    conn = db._conn
    assert conn is not None
    counts = conn.execute(
        "SELECT operation, count FROM rate_limit_buckets "
        "WHERE tenant='acme' ORDER BY operation"
    ).fetchall()
    assert counts == [("global:3600", 1), ("global:60", 1)]
    db.close()


def test_catalog_db_migrates_rate_limit_table_from_version_five(
    monkeypatch,
    tmp_path,
):
    path = _catalog_db(tmp_path)
    monkeypatch.setattr(metadb, "_CATALOG_SCHEMA_VERSION", 5)
    db = CatalogDB()
    db.open(path)
    db.register_collection("acme", "documents")
    db.close()

    monkeypatch.setattr(metadb, "_CATALOG_SCHEMA_VERSION", 6)
    db.open(path)

    assert db.list_collections("acme") == ["documents"]
    assert db._schema_version(db._require_rconn()) == 6
    assert db._require_rconn().execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='rate_limit_buckets'"
    ).fetchone() == ("rate_limit_buckets",)
    db.close()


def test_catalog_db_migrates_reindex_jobs_from_version_six(
    monkeypatch,
    tmp_path,
):
    path = _catalog_db(tmp_path)
    monkeypatch.setattr(metadb, "_CATALOG_SCHEMA_VERSION", 6)
    db = CatalogDB()
    db.open(path)
    db.register_collection("acme", "documents")
    db.close()

    monkeypatch.setattr(metadb, "_CATALOG_SCHEMA_VERSION", 7)
    db.open(path)

    assert db.list_collections("acme") == ["documents"]
    assert db._schema_version(db._require_rconn()) == 7
    assert db._require_rconn().execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='reindex_jobs'"
    ).fetchone() == ("reindex_jobs",)
    db.close()


def test_catalog_metrics_updates_are_atomic_across_connections(tmp_path):
    path = _catalog_db(tmp_path)
    first = CatalogDB()
    second = CatalogDB()
    first.open(path)
    second.open(path)
    generation = first.metrics_generation()
    start = threading.Barrier(2)
    errors: list[Exception] = []

    def increment(db):
        try:
            start.wait(timeout=1)
            db.flush_metrics_delta(
                generation,
                [("search_total", "{}", 1.0)],
                {},
                None,
                1000,
            )
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=increment, args=(db,))
        for db in (first, second)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert first.read_metrics()["metrics"]["search_total"] == [
        {"labels": {}, "value": 2.0}
    ]
    first.close()
    second.close()


def test_catalog_metrics_rejects_stale_generation_after_reset(tmp_path):
    db = CatalogDB()
    db.open(_catalog_db(tmp_path))
    stale_generation = db.metrics_generation()

    current_generation = db.reset_metrics()
    seen_generation, applied = db.flush_metrics_delta(
        stale_generation,
        [("search_total", "{}", 1.0)],
        {},
        None,
        1000,
    )

    assert current_generation != stale_generation
    assert seen_generation == current_generation
    assert applied is False
    assert db.read_metrics()["metrics"] == {}
    db.close()


def test_catalog_metrics_bounds_latency_samples(tmp_path):
    db = CatalogDB()
    db.open(_catalog_db(tmp_path))
    generation = db.metrics_generation()

    db.flush_metrics_delta(
        generation,
        [],
        {"search": [float(value) for value in range(1005)]},
        None,
        1000,
    )

    samples = db.read_metrics()["latencies"]["search"]
    assert len(samples) == 1000
    assert samples[0] == 5.0
    db.close()


@pytest.mark.parametrize(
    ("db_cls", "path_fn"),
    [
        (CollectionDB, _meta_db),
        (CatalogDB, _catalog_db),
    ],
)
def test_close_waits_for_inflight_writer(tmp_path, db_cls, path_fn):
    db = db_cls()
    db.open(path_fn(tmp_path))

    writer_entered = threading.Event()
    release_writer = threading.Event()
    close_done = threading.Event()
    close_errors: list[Exception] = []

    def _writer():
        with db._writer() as conn, conn:
            conn.execute("SELECT 1")
            writer_entered.set()
            assert release_writer.wait(timeout=1.0)

    def _closer():
        try:
            db.close()
        except Exception as exc:  # pragma: no cover - regression capture
            close_errors.append(exc)
        finally:
            close_done.set()

    writer_thread = threading.Thread(target=_writer)
    writer_thread.start()
    assert writer_entered.wait(timeout=1.0)

    closer_thread = threading.Thread(target=_closer)
    closer_thread.start()
    time.sleep(0.05)

    assert close_done.is_set() is False

    release_writer.set()
    writer_thread.join(timeout=1.0)
    closer_thread.join(timeout=1.0)

    assert not close_errors
    assert close_done.is_set() is True
    assert db._conn is None
    assert db._wconn is None
    if isinstance(db, CollectionDB):
        assert db._log_conn is None


@pytest.mark.parametrize(
    ("db_cls", "path_fn"),
    [
        (CollectionDB, _meta_db),
        (CatalogDB, _catalog_db),
    ],
)
def test_close_waits_for_inflight_reader(tmp_path, db_cls, path_fn):
    db = db_cls()
    db.open(path_fn(tmp_path))

    reader_entered = threading.Event()
    release_reader = threading.Event()
    close_done = threading.Event()
    close_errors: list[Exception] = []

    def _reader():
        with db._reader() as conn:
            conn.execute("SELECT 1")
            reader_entered.set()
            assert release_reader.wait(timeout=1.0)

    def _closer():
        try:
            db.close()
        except Exception as exc:  # pragma: no cover - regression capture
            close_errors.append(exc)
        finally:
            close_done.set()

    reader_thread = threading.Thread(target=_reader)
    reader_thread.start()
    assert reader_entered.wait(timeout=1.0)

    closer_thread = threading.Thread(target=_closer)
    closer_thread.start()
    time.sleep(0.05)

    assert close_done.is_set() is False

    release_reader.set()
    reader_thread.join(timeout=1.0)
    closer_thread.join(timeout=1.0)

    assert not close_errors
    assert close_done.is_set() is True
    assert db._conn is None
    assert db._wconn is None
    if isinstance(db, CollectionDB):
        assert db._log_conn is None


@pytest.mark.parametrize("legacy_name", ["catalog.json", "meta.json"])
def test_legacy_json_detection(tmp_path, legacy_name):
    base = _meta_db(tmp_path).parent
    base.mkdir(parents=True, exist_ok=True)
    (base / legacy_name).write_text("{}", encoding="utf-8")
    db = CollectionDB()
    with pytest.raises(LegacyMetadataError):
        db.open(base / "meta.db")


def test_upsert_and_get_meta(tmp_path):
    db = CollectionDB()
    db.open(_meta_db(tmp_path))
    chunks = [
        (
            "doc1::chunk_0",
            "chunks/doc1__chunk_0.txt",
            {"docid": "doc1", "chunk": 0},
        ),
        (
            "doc1::chunk_1",
            "chunks/doc1__chunk_1.txt",
            {"docid": "doc1", "chunk": 1},
        ),
    ]
    db.upsert_chunks("doc1", chunks, doc_meta={"docid": "doc1"})
    assert db.has_doc("doc1") is True
    rids = db.get_rids_for_doc("doc1")
    assert set(rids) == {"doc1::chunk_0", "doc1::chunk_1"}
    meta = db.get_meta_batch(rids)
    assert meta["doc1::chunk_0"]["chunk"] == 0
    assert meta["doc1::chunk_1"]["chunk"] == 1
    assert db.get_doc_version("doc1") == 1
    db.close()


def test_delete_doc(tmp_path):
    db = CollectionDB()
    db.open(_meta_db(tmp_path))
    chunks = [
        ("doc2::chunk_0", "chunks/doc2__chunk_0.txt", {"docid": "doc2"}),
    ]
    db.upsert_chunks("doc2", chunks, doc_meta={"docid": "doc2"})
    deleted = db.delete_doc("doc2")
    assert deleted == ["doc2::chunk_0"]
    assert db.has_doc("doc2") is False
    db.close()


def test_open_read_only_skips_wconn_and_migrations(tmp_path):
    db_path = _meta_db(tmp_path)
    # First open normally to create schema
    db = CollectionDB()
    db.open(db_path)
    db.upsert_chunks(
        "doc1",
        [("doc1::chunk_0", "chunks/doc1__chunk_0.txt", {"docid": "doc1"})],
        doc_meta={"docid": "doc1"},
    )
    db.close()

    # Re-open read-only
    ro = CollectionDB()
    ro.open(db_path, read_only=True)
    assert ro._rconn is not None
    assert ro._wconn is None
    assert ro._log_conn is None
    assert ro.has_doc("doc1") is True
    meta = ro.get_meta_batch(["doc1::chunk_0"])
    assert meta["doc1::chunk_0"]["docid"] == "doc1"
    ro.close()


def test_open_read_only_does_not_create_dirs(tmp_path):
    db_path = tmp_path / "nonexistent" / "sub" / "meta.db"
    db = CollectionDB()
    with pytest.raises(Exception):
        db.open(db_path, read_only=True)
    assert not db_path.parent.exists()


def test_open_read_only_missing_file_does_not_create_db(tmp_path):
    db_path = _meta_db(tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    db = CollectionDB()
    with pytest.raises(sqlite3.OperationalError):
        db.open(db_path, read_only=True)

    assert not db_path.exists()


def test_open_rejects_older_collection_schema_version(tmp_path):
    db_path = _meta_db(tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (5, 'old')"
    )
    conn.commit()
    conn.close()

    db = CollectionDB()
    with pytest.raises(UnsupportedSchemaVersionError):
        db.open(db_path)


def test_collection_db_migrates_from_preview_baseline(monkeypatch, tmp_path):
    db_path = _meta_db(tmp_path)
    db = CollectionDB()
    db.open(db_path)
    db.upsert_chunks("doc", [("doc::0", None, {})])
    db.close()

    monkeypatch.setattr(metadb, "_COLLECTION_SCHEMA_VERSION", 7)
    monkeypatch.setattr(
        metadb,
        "_COLLECTION_MIGRATIONS",
        {7: ["ALTER TABLE documents ADD COLUMN preview_note TEXT"]},
    )

    with pytest.raises(UnsupportedSchemaVersionError, match="requires migration"):
        db.open(db_path, read_only=True)

    db.open(db_path)

    assert db.has_doc("doc") is True
    assert db._schema_version(db._require_rconn()) == 7
    assert "preview_note" in {
        row[1]
        for row in db._require_rconn().execute("PRAGMA table_info(documents)")
    }
    db.close()


def test_catalog_db_open_rejects_older_schema_version(tmp_path):
    db_path = _catalog_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (4, 'old')"
    )
    conn.commit()
    conn.close()

    db = CatalogDB()
    with pytest.raises(UnsupportedSchemaVersionError):
        db.open(db_path)


def test_catalog_db_applies_forward_migration(monkeypatch, tmp_path):
    db_path = _catalog_db(tmp_path)
    db = CatalogDB()
    db.open(db_path)
    db.register_collection("acme", "documents")
    db.close()

    monkeypatch.setattr(metadb, "_CATALOG_SCHEMA_VERSION", 8)
    monkeypatch.setattr(
        metadb,
        "_CATALOG_MIGRATIONS",
        {
            **metadb._CATALOG_MIGRATIONS,
            8: ["CREATE TABLE preview_migration_probe (value TEXT)"],
        },
    )

    db.open(db_path)

    assert db.list_collections("acme") == ["documents"]
    assert db._schema_version(db._require_rconn()) == 8
    assert db._require_rconn().execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='preview_migration_probe'"
    ).fetchone() == ("preview_migration_probe",)
    db.close()


def test_catalog_migration_serializes_across_connections(monkeypatch, tmp_path):
    db_path = _catalog_db(tmp_path)
    db = CatalogDB()
    db.open(db_path)
    db.close()

    monkeypatch.setattr(metadb, "_CATALOG_SCHEMA_VERSION", 8)
    monkeypatch.setattr(
        metadb,
        "_CATALOG_MIGRATIONS",
        {
            **metadb._CATALOG_MIGRATIONS,
            8: ["CREATE TABLE preview_migration_probe (value TEXT)"],
        },
    )
    start = threading.Barrier(2)
    errors: list[Exception] = []

    def migrate():
        migration_db = CatalogDB()
        try:
            start.wait(timeout=1)
            migration_db.open(db_path)
        except Exception as exc:
            errors.append(exc)
        finally:
            migration_db.close()

    threads = [threading.Thread(target=migrate) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    with sqlite3.connect(db_path) as conn:
        assert CatalogDB._schema_version(conn) == 8


def test_catalog_db_rolls_back_failed_migration(monkeypatch, tmp_path):
    db_path = _catalog_db(tmp_path)
    db = CatalogDB()
    db.open(db_path)
    db.close()

    monkeypatch.setattr(metadb, "_CATALOG_SCHEMA_VERSION", 8)
    monkeypatch.setattr(
        metadb,
        "_CATALOG_MIGRATIONS",
        {
            **metadb._CATALOG_MIGRATIONS,
            8: [
                "CREATE TABLE preview_migration_probe (value TEXT)",
                "INVALID SQL",
            ]
        },
    )

    with pytest.raises(SchemaMigrationError, match="database unchanged"):
        db.open(db_path)

    with sqlite3.connect(db_path) as conn:
        assert CatalogDB._schema_version(conn) == 7
        assert conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='preview_migration_probe'"
        ).fetchone() is None


def test_catalog_db_rejects_newer_schema_version(tmp_path):
    db_path = _catalog_db(tmp_path)
    db = CatalogDB()
    db.open(db_path)
    with db._writer() as conn, conn:
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) "
            "VALUES (8, 'future')"
        )
    db.close()

    with pytest.raises(UnsupportedSchemaVersionError, match="newer"):
        db.open(db_path)


def test_get_doc_chunk_counts(tmp_path):
    db = CollectionDB()
    db.open(_meta_db(tmp_path))
    db.upsert_chunks(
        "doc3",
        [
            ("doc3::chunk_0", "chunks/doc3__chunk_0.txt", {"docid": "doc3"}),
            ("doc3::chunk_1", "chunks/doc3__chunk_1.txt", {"docid": "doc3"}),
        ],
        doc_meta={"docid": "doc3"},
    )
    db.upsert_chunks(
        "doc4",
        [("doc4::chunk_0", "chunks/doc4__chunk_0.txt", {"docid": "doc4"})],
        doc_meta={"docid": "doc4"},
    )
    assert db.get_doc_chunk_counts() == (2, 3)
    db.close()


def test_get_document_returns_doc_metadata_and_chunk_ids(tmp_path):
    db = CollectionDB()
    db.open(_meta_db(tmp_path))
    db.upsert_chunks(
        "doc5",
        [
            ("doc5::chunk_0", "chunks/doc5__chunk_0.txt", {"offset": 0}),
            ("doc5::chunk_1", "chunks/doc5__chunk_1.txt", {"offset": 50}),
        ],
        doc_meta={
            "docid": "doc5",
            "filename": "doc5.txt",
            "ingested_at": "2026-04-04T00:00:00Z",
            "lang": "pt",
        },
    )

    data = db.get_document("doc5")

    assert data is not None
    assert data["docid"] == "doc5"
    assert data["version"] == 1
    assert data["ingested_at"].endswith("Z")
    assert data["metadata"] == {
        "docid": "doc5",
        "filename": "doc5.txt",
        "ingested_at": "2026-04-04T00:00:00Z",
        "lang": "pt",
    }
    assert data["chunk_ids"] == ["doc5::chunk_0", "doc5::chunk_1"]
    assert data["chunk_count"] == 2
    assert db.get_document("missing") is None
    db.close()


def test_list_chunks_and_get_chunk_return_chunk_metadata(tmp_path):
    db = CollectionDB()
    db.open(_meta_db(tmp_path))
    db.upsert_chunks(
        "doc6",
        [
            ("doc6::chunk_0", "chunks/doc6__chunk_0.txt", {"offset": 0}),
            ("doc6::chunk_1", "chunks/doc6__chunk_1.txt", {"offset": 50}),
        ],
        doc_meta={
            "docid": "doc6",
            "filename": "doc6.txt",
            "ingested_at": "2026-04-04T00:00:00Z",
        },
    )

    chunks = db.list_chunks("doc6")
    chunk = db.get_chunk("doc6::chunk_1")

    assert [entry["rid"] for entry in chunks] == [
        "doc6::chunk_0",
        "doc6::chunk_1",
    ]
    assert chunks[0]["chunk_path"] == "chunks/doc6__chunk_0.txt"
    assert chunks[0]["meta"] == {"offset": 0}
    assert chunks[0]["ingested_at"].endswith("Z")
    assert chunk == {
        "docid": "doc6",
        "rid": "doc6::chunk_1",
        "chunk_path": "chunks/doc6__chunk_1.txt",
        "meta": {"offset": 50},
        "ingested_at": chunks[1]["ingested_at"],
    }
    assert db.list_chunks("missing") == []
    assert db.get_chunk("missing::chunk_0") is None
    db.close()


def test_catalog_db_bootstrap_seeds_and_prunes_orphans(tmp_path):
    existing = CollectionDB()
    collection_dir = tmp_path / "t_acme" / "c_docs"
    existing.open(collection_dir / "meta.db")
    existing.close()
    (collection_dir / COLLECTION_CONFIG_FILENAME).write_text(
        json.dumps(
            {
                "backend_type": "faiss",
                "backend_config": {},
                "embedder_type": "identity",
                "embed_model": "",
                "embedder_config": {"dim": 4},
            }
        ),
        encoding="utf-8",
    )

    db = CatalogDB()
    db.open(_catalog_db(tmp_path))
    db.register_collection("ghost", "orphan", backend_type="faiss")

    seeded, removed = db.bootstrap(tmp_path)

    assert seeded == 1
    assert removed == 1
    assert db.list_tenants() == ["acme"]
    assert db.list_collections("acme") == ["docs"]
    assert db.get_collection_config("acme", "docs")["embedder_config"] == {
        "dim": 4
    }
    db.close()


def test_catalog_db_get_collection_config(tmp_path):
    db = CatalogDB()
    db.open(_catalog_db(tmp_path))
    db.register_collection(
        "acme",
        "docs",
        display_name="Docs",
        meta={"owner": "ops"},
        backend_type="faiss",
        backend_config={"metric": "cosine"},
        embedder_type="sbert",
        embed_model="fake",
        embed_config={"normalize": True},
    )

    cfg = db.get_collection_config("acme", "docs")

    assert cfg is not None
    assert cfg["display_name"] == "Docs"
    assert cfg["meta"] == {"owner": "ops"}
    assert cfg["backend_type"] == "faiss"
    assert cfg["backend_config"] == {"metric": "cosine"}
    assert cfg["embedder_type"] == "sbert"
    assert cfg["embed_model"] == "fake"
    assert cfg["embedder_config"] == {"normalize": True}
    assert cfg["created_at"]
    assert db.get_collection_config("acme", "missing") is None
    db.close()


def test_catalog_db_move_collection_is_retry_safe(tmp_path):
    db = CatalogDB()
    db.open(_catalog_db(tmp_path))
    db.register_collection(
        "acme",
        "docs",
        backend_type="faiss",
        embedder_type="sbert",
        embed_model="fake",
    )

    db.move_collection("acme", "docs", "docs-v2")
    db.move_collection("acme", "docs", "docs-v2")

    assert db.list_collections("acme") == ["docs-v2"]
    db.close()


def test_chunk_meta_populated_on_upsert(tmp_path):
    db = CollectionDB()
    db.open(_meta_db(tmp_path))
    db.upsert_chunks(
        "doc1",
        [
            (
                "doc1::chunk_0",
                "chunks/doc1__chunk_0.txt",
                {"docid": "doc1", "lang": "en", "chunk": 0},
            ),
        ],
        doc_meta={"docid": "doc1"},
    )

    conn = db._conn
    assert conn is not None
    rows = conn.execute(
        "SELECT rid, key, value FROM chunk_meta ORDER BY rid, key"
    ).fetchall()

    assert rows == [
        ("doc1::chunk_0", "chunk", "0"),
        ("doc1::chunk_0", "docid", "doc1"),
        ("doc1::chunk_0", "lang", "en"),
    ]
    db.close()


def test_document_meta_populated_on_upsert(tmp_path):
    db = CollectionDB()
    db.open(_meta_db(tmp_path))
    db.upsert_chunks(
        "doc1",
        [
            (
                "doc1::chunk_0",
                "chunks/doc1__chunk_0.txt",
                {"chunk": 0},
            ),
        ],
        doc_meta={"docid": "doc1", "lang": "en", "source": "api"},
    )

    conn = db._conn
    assert conn is not None
    rows = conn.execute(
        "SELECT docid, key, value FROM document_meta ORDER BY docid, key"
    ).fetchall()

    assert rows == [
        ("doc1", "docid", "doc1"),
        ("doc1", "lang", "en"),
        ("doc1", "source", "api"),
    ]
    db.close()


def test_chunk_meta_cleaned_on_delete(tmp_path):
    db = CollectionDB()
    db.open(_meta_db(tmp_path))
    db.upsert_chunks(
        "doc1",
        [
            (
                "doc1::chunk_0",
                "chunks/doc1__chunk_0.txt",
                {"docid": "doc1", "lang": "en"},
            ),
        ],
        doc_meta={"docid": "doc1"},
    )

    db.delete_doc("doc1")

    conn = db._conn
    assert conn is not None
    count = conn.execute("SELECT COUNT(*) FROM chunk_meta").fetchone()[0]
    assert count == 0
    doc_count = conn.execute(
        "SELECT COUNT(*) FROM document_meta"
    ).fetchone()[0]
    assert doc_count == 0
    db.close()


def test_chunk_meta_reupsert_replaces_stale_rows(tmp_path):
    db = CollectionDB()
    db.open(_meta_db(tmp_path))
    db.upsert_chunks(
        "doc1",
        [
            (
                "doc1::chunk_0",
                "chunks/doc1__chunk_0.txt",
                {"docid": "doc1", "lang": "en", "category": "ml"},
            ),
        ],
        doc_meta={"docid": "doc1"},
    )

    db.upsert_chunks(
        "doc1",
        [
            (
                "doc1::chunk_0",
                "chunks/doc1__chunk_0.txt",
                {"docid": "doc1", "lang": "pt"},
            ),
        ],
        doc_meta={"docid": "doc1"},
    )

    conn = db._conn
    assert conn is not None
    rows = conn.execute(
        "SELECT rid, key, value FROM chunk_meta ORDER BY rid, key"
    ).fetchall()

    assert rows == [
        ("doc1::chunk_0", "docid", "doc1"),
        ("doc1::chunk_0", "lang", "pt"),
    ]
    db.close()


def test_filter_by_meta_applies_exact_or_and_semantics(tmp_path):
    db = CollectionDB()
    db.open(_meta_db(tmp_path))
    db.upsert_chunks(
        "doc1",
        [
            (
                "doc1::chunk_0",
                "chunks/doc1__chunk_0.txt",
                {"docid": "doc1", "lang": "en", "category": "ml"},
            ),
            (
                "doc1::chunk_1",
                "chunks/doc1__chunk_1.txt",
                {"docid": "doc1", "lang": "en", "category": "infra"},
            ),
        ],
        doc_meta={"docid": "doc1"},
    )
    db.upsert_chunks(
        "doc2",
        [
            (
                "doc2::chunk_0",
                "chunks/doc2__chunk_0.txt",
                {"docid": "doc2", "lang": "pt", "category": "ml"},
            ),
            (
                "doc2::chunk_1",
                "chunks/doc2__chunk_1.txt",
                {"docid": "doc2", "lang": "de", "category": "infra"},
            ),
        ],
        doc_meta={"docid": "doc2"},
    )

    matched = db.filter_by_meta(
        [
            "doc1::chunk_0",
            "doc1::chunk_1",
            "doc2::chunk_0",
            "doc2::chunk_1",
        ],
        {
            "lang": ["en", "pt"],
            "category": ["ml"],
        },
    )

    assert matched == {"doc1::chunk_0", "doc2::chunk_0"}
    db.close()


def test_filter_by_meta_matches_document_level_fields(tmp_path):
    db = CollectionDB()
    db.open(_meta_db(tmp_path))
    db.upsert_chunks(
        "doc1",
        [
            (
                "doc1::chunk_0",
                "chunks/doc1__chunk_0.txt",
                {"chunk": 0, "category": "ml"},
            ),
            (
                "doc1::chunk_1",
                "chunks/doc1__chunk_1.txt",
                {"chunk": 1, "category": "infra"},
            ),
        ],
        doc_meta={"docid": "doc1", "lang": "en", "source": "api"},
    )

    matched = db.filter_by_meta(
        ["doc1::chunk_0", "doc1::chunk_1"],
        {"lang": ["en"], "source": ["api"]},
    )

    assert matched == {"doc1::chunk_0", "doc1::chunk_1"}
    db.close()


def test_filter_by_meta_matches_list_valued_document_and_chunk_fields(tmp_path):
    db = CollectionDB()
    db.open(_meta_db(tmp_path))
    db.upsert_chunks(
        "doc1",
        [
            (
                "doc1::chunk_0",
                "chunks/doc1__chunk_0.txt",
                {"chunk_tags": ["gold", "chunk-group"]},
            ),
        ],
        doc_meta={"document_tags": ["gold", "doc-group"]},
    )

    matched = db.filter_by_meta(
        ["doc1::chunk_0"],
        {"document_tags": ["gold"], "chunk_tags": ["gold"]},
    )

    assert matched == {"doc1::chunk_0"}
    db.close()


def test_filter_by_meta_defers_negated_fields_to_canonical_postfilter(tmp_path):
    db = CollectionDB()
    db.open(_meta_db(tmp_path))
    db.upsert_chunks(
        "doc1",
        [
            (
                "doc1::chunk_0",
                "chunks/doc1__chunk_0.txt",
                {"chunk": 0},
            ),
        ],
        doc_meta={"docid": "doc1", "lang": "en"},
    )
    db.upsert_chunks(
        "doc2",
        [
            (
                "doc2::chunk_0",
                "chunks/doc2__chunk_0.txt",
                {"chunk": 0},
            ),
        ],
        doc_meta={"docid": "doc2", "lang": "pt"},
    )

    all_rids = ["doc1::chunk_0", "doc2::chunk_0"]

    matched = db.filter_by_meta(all_rids, {"lang": ["!pt"]})
    assert matched == set(all_rids)

    matched = db.filter_by_meta(all_rids, {"lang": ["!en"]})
    assert matched == set(all_rids)
    db.close()


def test_get_meta_batch_merges_document_and_chunk_metadata(tmp_path):
    db = CollectionDB()
    db.open(_meta_db(tmp_path))
    db.upsert_chunks(
        "doc1",
        [
            (
                "doc1::chunk_0",
                "chunks/doc1__chunk_0.txt",
                {"chunk": 0, "lang": "pt"},
            ),
        ],
        doc_meta={"docid": "doc1", "filename": "meta.txt", "lang": "en"},
    )

    meta = db.get_meta_batch(["doc1::chunk_0"])

    assert meta == {
        "doc1::chunk_0": {
            "docid": "doc1",
            "filename": "meta.txt",
            "lang": "pt",
            "chunk": 0,
        }
    }
    db.close()


def test_filter_by_meta_ignores_non_pushdown_values(tmp_path):
    db = CollectionDB()
    db.open(_meta_db(tmp_path))
    db.upsert_chunks(
        "doc1",
        [
            (
                "doc1::chunk_0",
                "chunks/doc1__chunk_0.txt",
                {"docid": "doc1", "lang": "en", "category": "ml"},
            ),
            (
                "doc1::chunk_1",
                "chunks/doc1__chunk_1.txt",
                {"docid": "doc1", "lang": "en", "category": "infra"},
            ),
        ],
        doc_meta={"docid": "doc1"},
    )

    candidates = ["doc1::chunk_0", "doc1::chunk_1"]
    matched = db.filter_by_meta(
        candidates,
        {
            "lang": ["en"],
            "category": ["*fra", "ml*"],
            "size": [">100"],
        },
    )

    assert matched == set(candidates)
    db.close()


def test_filter_by_meta_skips_mixed_or_key_with_postfilter_values(tmp_path):
    db = CollectionDB()
    db.open(_meta_db(tmp_path))
    db.upsert_chunks(
        "doc1",
        [
            (
                "doc1::chunk_0",
                "chunks/doc1__chunk_0.txt",
                {"category": "ml"},
            ),
            (
                "doc1::chunk_1",
                "chunks/doc1__chunk_1.txt",
                {"category": "nlp"},
            ),
            (
                "doc1::chunk_2",
                "chunks/doc1__chunk_2.txt",
                {"category": "infra"},
            ),
        ],
        doc_meta={"docid": "doc1"},
    )

    candidates = ["doc1::chunk_0", "doc1::chunk_1", "doc1::chunk_2"]
    matched = db.filter_by_meta(
        candidates,
        {"category": ["ml", "*lp"]},
    )

    assert matched == set(candidates)
    db.close()
