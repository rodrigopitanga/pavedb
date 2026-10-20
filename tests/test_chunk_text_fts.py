# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json

import pave.metadb as metadb
from pave.metadb import CollectionDB
from pave.stores.local import LocalStore
from scripts.check_sqlite_fts import main as check_sqlite_fts
from utils import FakeEmbedder


def _db_path(tmp_path):
    return tmp_path / "t_acme" / "c_docs" / "meta.db"


def test_runtime_sqlite_supports_required_fts_tokenizers():
    assert check_sqlite_fts() == 0


def test_collection_db_indexes_content_filters_and_lexical_candidates(tmp_path):
    db = CollectionDB()
    db.open(_db_path(tmp_path))
    chunks = [
        ("a::0", None, {"kind": "novel"}),
        ("b::0", None, {"kind": "note"}),
        ("c::0", None, {"kind": "vector"}),
    ]
    db.upsert_chunks(
        "doc",
        chunks,
        texts={
            "a::0": "Captain Nemo commands the Nautilus",
            "b::0": "A captain studies ocean charts",
            "c::0": None,
        },
    )
    rids = [rid for rid, _path, _meta in chunks]

    assert db.filter_by_content(
        rids,
        {"op": "exact", "value": "Captain Nemo commands the Nautilus"},
    ) == {"a::0"}
    assert db.filter_by_content(
        rids,
        {"op": "phrase", "value": "captain nemo"},
    ) == {"a::0"}
    assert db.filter_by_content(
        rids,
        {"op": "prefix", "value": "captain ne"},
    ) == {"a::0"}
    assert db.filter_by_content(
        rids,
        {"op": "contains", "value": "tain Ne"},
    ) == {"a::0"}
    assert db.filter_by_content(
        rids,
        {"op": "phrase", "value": 'captain" OR nemo'},
    ) == set()
    for content_filter in (
        {"op": "exact", "value": "Captain Nemo commands the Nautilus"},
        {"op": "phrase", "value": "captain nemo"},
        {"op": "prefix", "value": "captain ne"},
        {"op": "contains", "value": "tain Ne"},
    ):
        selected = db.select_filter_candidates({}, content_filter)
        assert selected == db.filter_by_content(rids, content_filter)
        assert db.select_filter_candidates(
            {}, content_filter, len(rids) + 1,
        ) == selected
    assert db.select_filter_candidates(
        {"kind": ["novel"]},
        {"op": "contains", "value": "Nautilus"},
    ) == {"a::0"}

    candidates = db.lexical_candidates("captain nemo", 10)
    assert [rid for rid, _score in candidates] == ["a::0", "b::0"]
    assert all(isinstance(score, float) for _rid, score in candidates)
    db.rebuild_text_indexes()
    assert db.filter_by_content(
        rids,
        {"op": "phrase", "value": "captain nemo"},
    ) == {"a::0"}
    db.verify_text_indexes()

    db.upsert_chunks(
        "doc",
        [("a::0", None, {"kind": "novel"})],
        texts={"a::0": "Professor Aronnax boards the Nautilus"},
    )
    assert db.filter_by_content(
        rids,
        {"op": "phrase", "value": "captain nemo"},
    ) == set()
    assert db.filter_by_content(
        rids,
        {"op": "prefix", "value": "professor aron"},
    ) == {"a::0"}
    db.delete_doc("doc")
    assert db.filter_by_content(
        rids,
        {"op": "contains", "value": "Nautilus"},
    ) == set()
    db.close()


def test_collection_migration_copies_and_removes_legacy_sidecars(
    monkeypatch,
    tmp_path,
):
    db_path = _db_path(tmp_path)
    monkeypatch.setattr(metadb, "_COLLECTION_SCHEMA_VERSION", 6)
    db = CollectionDB()
    db.open(db_path)
    with db._writer() as conn, conn:
        conn.execute(
            "INSERT INTO documents(docid, meta_json) VALUES (?, ?)",
            ("legacy", "{}"),
        )
        conn.execute(
            "INSERT INTO chunks(docid, rid, chunk_path, meta_json) "
            "VALUES (?, ?, ?, ?)",
            ("legacy", "legacy::0", "chunks/legacy__0.txt", "{}"),
        )
    db.close()

    sidecar = db_path.parent / "chunks" / "legacy__0.txt"
    sidecar.parent.mkdir()
    sidecar.write_text("Legacy Captain Nemo text", encoding="utf-8")

    monkeypatch.setattr(metadb, "_COLLECTION_SCHEMA_VERSION", 7)
    db.open(db_path)
    LocalStore._migrate_chunk_text_sidecars_in(db_path.parent, db)

    assert db.get_chunk_text("legacy::0") == "Legacy Captain Nemo text"
    assert db.get_chunk("legacy::0")["chunk_path"] is None
    assert db.filter_by_content(
        ["legacy::0"],
        {"op": "contains", "value": "Captain"},
    ) == {"legacy::0"}
    assert not sidecar.exists()
    db.close()


def test_collection_migration_indexes_text_less_rows(monkeypatch, tmp_path):
    db_path = _db_path(tmp_path)
    monkeypatch.setattr(metadb, "_COLLECTION_SCHEMA_VERSION", 6)
    db = CollectionDB()
    db.open(db_path)
    with db._writer() as conn, conn:
        conn.execute(
            "INSERT INTO documents(docid, meta_json) VALUES (?, ?)",
            ("raw", "{}"),
        )
        conn.execute(
            "INSERT INTO chunks(docid, rid, chunk_path, meta_json) "
            "VALUES (?, ?, NULL, ?)",
            ("raw", "raw::0", "{}"),
        )
    db.close()

    # A BYOV row has no sidecar, so no sidecar migration (and no rebuild)
    # runs for it: the schema migration alone must leave the indexes whole.
    monkeypatch.setattr(metadb, "_COLLECTION_SCHEMA_VERSION", 7)
    db.open(db_path)
    try:
        assert db.legacy_chunk_text_rows() == []
        db.verify_text_indexes()
    finally:
        db.close()


def test_content_filter_is_additive_and_persists_for_replay(client):
    client.post("/v1/collections/acme/content")
    for docid, text, kind in (
        ("BOOK-1", b"Captain Nemo commands the Nautilus", "novel"),
        ("NOTE-1", b"Captain Nemo appears in a short note", "note"),
    ):
        response = client.post(
            "/v1/collections/acme/content/documents",
            files={"file": (f"{docid}.txt", text, "text/plain")},
            data={"docid": docid, "metadata": json.dumps({"kind": kind})},
        )
        assert response.status_code == 201

    response = client.post(
        "/v1/collections/acme/content/search",
        json={
            "q": "captain",
            "k": 5,
            "filters": {"kind": "novel"},
            "content_filter": {"op": "phrase", "value": "captain nemo"},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert [match["meta"]["docid"] for match in body["matches"]] == ["BOOK-1"]
    assert "content phrase match" in body["matches"][0]["match_reason"]

    query = client.get(
        f"/v1/collections/acme/content/queries/{body['query_id']}"
    ).json()["query"]
    assert query["content_filter"] == {
        "op": "phrase",
        "value": "captain nemo",
    }
    replay = client.post(
        f"/v1/collections/acme/content/queries/{body['query_id']}/replay"
    )
    assert replay.status_code == 200
    assert [match["meta"]["docid"] for match in replay.json()["matches"]] == [
        "BOOK-1"
    ]


def test_contains_content_filter_requires_three_characters(client):
    client.post("/v1/collections/acme/content-validation")
    response = client.post(
        "/v1/collections/acme/content-validation/search",
        json={
            "q": "x",
            "content_filter": {"op": "contains", "value": "xy"},
        },
    )
    assert response.status_code == 422
