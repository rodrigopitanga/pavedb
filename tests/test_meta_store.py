# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from pave.metadb import (
    CatalogDB,
    CollectionDB,
    LegacyMetadataError,
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
        "AND name IN ('schema_migrations', 'collections', 'query_home')"
    )
    names = {row[0] for row in cur.fetchall()}
    assert {"schema_migrations", "collections", "query_home"} <= names
    version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    assert version == (3,)
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

    def _writer() -> None:
        with db._writer() as conn, conn:
            conn.execute("SELECT 1")
            writer_entered.set()
            assert release_writer.wait(timeout=1.0)

    def _closer() -> None:
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


def test_catalog_db_open_rejects_older_schema_version(tmp_path):
    db_path = _catalog_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (2, 'old')"
    )
    conn.commit()
    conn.close()

    db = CatalogDB()
    with pytest.raises(UnsupportedSchemaVersionError):
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
    existing.open(tmp_path / "t_acme" / "c_docs" / "meta.db")
    existing.close()

    db = CatalogDB()
    db.open(_catalog_db(tmp_path))
    db.register_collection("ghost", "orphan", backend_type="faiss")

    seeded, removed = db.bootstrap(
        tmp_path,
        backend_type="faiss",
        backend_config={},
        embedder_type="sbert",
        embed_model="fake",
        embed_config={},
    )

    assert seeded == 1
    assert removed == 1
    assert db.list_tenants() == ["acme"]
    assert db.list_collections("acme") == ["docs"]
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


def test_catalog_db_rename_collection_is_retry_safe(tmp_path):
    db = CatalogDB()
    db.open(_catalog_db(tmp_path))
    db.register_collection(
        "acme",
        "docs",
        backend_type="faiss",
        embedder_type="sbert",
        embed_model="fake",
    )

    db.rename_collection("acme", "docs", "docs-v2")
    db.rename_collection("acme", "docs", "docs-v2")

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


def test_filter_by_meta_applies_exact_negation_or_and_semantics(tmp_path):
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
            "lang": ["en", "!pt"],
            "category": ["ml"],
        },
    )

    assert matched == {"doc1::chunk_0"}
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


def test_filter_by_meta_negates_document_level_fields(tmp_path):
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
    assert matched == {"doc1::chunk_0"}

    matched = db.filter_by_meta(all_rids, {"lang": ["!en"]})
    assert matched == {"doc2::chunk_0"}
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
