# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import pytest

import pave.metadb as metadb
from pave.metadb import CollectionDB
from pave.stores.local import LocalStore, _reciprocal_rank_fusion
from utils import FakeEmbedder


def test_reciprocal_rank_fusion_uses_ranks_and_vector_tiebreak():
    fused = _reciprocal_rank_fusion(
        [("vector", 0.99), ("both", 0.01)],
        [("lexical", -12.0), ("both", -0.1)],
    )

    assert [row[0] for row in fused] == [
        "both",
        "vector",
        "lexical",
    ]
    by_rid = {row[0]: list(row[1:]) for row in fused}
    assert by_rid["both"][0] == pytest.approx(2 / 62)
    assert by_rid["both"][1:] == [2, 2]
    assert by_rid["vector"][0] == pytest.approx(1 / 61)
    assert by_rid["vector"][1:] == [1, None]
    assert by_rid["lexical"][0] == pytest.approx(1 / 61)
    assert by_rid["lexical"][1:] == [None, 1]


def test_hybrid_search_fuses_independent_candidate_lists(
    monkeypatch,
    temp_data_dir,
):
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    tenant, collection = "acme", "hybrid"
    store.create_collection(tenant, collection)
    records = [
        ("DOC::vector", "semantic candidate", {"kind": "keep"}),
        ("DOC::lexical", "captain nemo lexical", {"kind": "keep"}),
        ("DOC::both", "captain nemo combined", {"kind": "keep"}),
    ]
    store.index_records(tenant, collection, "DOC", records)
    backend = store._emb[(tenant, collection)]
    col_db = store._dbs[(tenant, collection)]
    limits: dict[str, int] = {}

    def vector_search(_vector, limit):
        limits["vector"] = limit
        return [
            ("DOC::vector", 0.99),
            ("DOC::both", 0.01),
        ][:limit]

    def lexical_search(_query, limit):
        limits["lexical"] = limit
        return [
            ("DOC::lexical", -12.0),
            ("DOC::both", -0.1),
        ][:limit]

    monkeypatch.setattr(
        backend,
        "search",
        vector_search,
    )
    monkeypatch.setattr(
        col_db,
        "lexical_candidates",
        lexical_search,
    )

    result = store.search(
        tenant,
        collection,
        "captain nemo",
        k=3,
        filters={"kind": "keep"},
        mode="hybrid",
    )

    assert [hit.id for hit in result] == [
        "DOC::both",
        "DOC::vector",
        "DOC::lexical",
    ]
    assert result[0].score == pytest.approx(2 / 62)
    assert result[0].match_reason.startswith("vector rank 2; lexical rank 2")
    assert result[1].match_reason.startswith("vector rank 1")
    assert result[2].match_reason.startswith("lexical rank 1")
    assert limits == {"vector": 3, "lexical": 50}
    assert result.timing["search_ms"] == pytest.approx(
        result.timing["vector_search_ms"]
        + result.timing["lexical_search_ms"],
        abs=0.01,
    )


def test_hybrid_mode_is_logged_and_replayed(client):
    collection = "hybridlog"
    client.post(f"/v1/collections/acme/{collection}")
    client.post(
        f"/v1/collections/acme/{collection}/documents",
        files={"file": ("a.txt", b"captain nemo", "text/plain")},
        data={"docid": "DOC-1"},
    )

    response = client.post(
        f"/v1/collections/acme/{collection}/search",
        json={"q": "captain", "k": 5, "mode": "hybrid"},
    )

    assert response.status_code == 200
    assert response.json()["timing"]["vector_search_ms"] >= 0.0
    assert response.json()["timing"]["lexical_search_ms"] >= 0.0
    query_id = response.json()["query_id"]
    entry = client.get(
        f"/v1/collections/acme/{collection}/queries/{query_id}"
    ).json()["query"]
    assert entry["mode"] == "hybrid"

    replay = client.post(
        f"/v1/collections/acme/{collection}/queries/{query_id}/replay"
    )
    replay_query_id = replay.json()["replay_query_id"]
    replay_entry = client.get(
        f"/v1/collections/acme/{collection}/queries/{replay_query_id}"
    ).json()["query"]
    assert replay_entry["mode"] == "hybrid"


def test_hybrid_search_rejects_raw_vector(client):
    response = client.post(
        "/v1/collections/acme/hybridvector/search",
        json={"v": [1.0, 0.0, 0.0, 0.0], "mode": "hybrid"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "search_mode_requires_text"


def test_collection_schema_migrates_logged_search_mode(monkeypatch, tmp_path):
    db_path = tmp_path / "t_acme" / "c_docs" / "meta.db"
    monkeypatch.setattr(metadb, "_COLLECTION_SCHEMA_VERSION", 7)
    db = CollectionDB()
    db.open(db_path)
    with db._writer() as conn, conn:
        conn.execute(
            "INSERT INTO query_log "
            "(query_id, tenant, collection, actor, query_text, k) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("legacy-query", "acme", "docs", "test", "captain", 5),
        )
    db.close()

    monkeypatch.setattr(metadb, "_COLLECTION_SCHEMA_VERSION", 8)
    db.open(db_path)

    assert db.get_query_log_entry("legacy-query")["mode"] == "vector"
    assert db._schema_version(db._require_rconn()) == 8
    db.close()
