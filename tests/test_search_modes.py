# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Search modes: operator serves, collection defaults, caller overrides."""

import sqlite3

import pytest

import pave.metadb as metadb
from pave.config import get_cfg
from pave.config.config import _validate_search
from pave.metadb import CollectionDB
from pave.stores.base import SearchModeError
from pave.stores.local import LocalStore
from utils import FakeEmbedder


def _store(temp_data_dir, name, *, search_mode=None):
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    store.create_collection("acme", name, search_mode=search_mode)
    store.index_records(
        "acme",
        name,
        "DOC1",
        [
            ("DOC1::chunk_a", "totally unrelated filler text", {}),
            ("DOC1::chunk_b", "the quick brown Fox", {}),
        ],
    )
    return store


def test_boost_counts_tokens_the_way_fts5_matches_them(temp_data_dir):
    store = _store(temp_data_dir, "boost-unit")
    col_db = store._dbs[("acme", "boost-unit")]
    raw = [("DOC1::chunk_a", 0.9), ("DOC1::chunk_b", 0.5)]

    boosted, hits = store._apply_exact_token_boost(
        col_db, raw, ["FOX", "quick", "absent"], 0.9,
    )

    # Case differs from the chunk text; FTS5 still matches, 2 of 3 tokens.
    assert hits == {"DOC1::chunk_b": (0.5, 2)}
    assert boosted[0] == ("DOC1::chunk_b", pytest.approx(0.5 + 0.9 * 2 / 3))


def test_boost_matches_each_candidate_once_per_search(
    temp_data_dir, monkeypatch,
):
    store = _store(temp_data_dir, "boost-cache")
    col_db = store._dbs[("acme", "boost-cache")]
    calls: list[list[str]] = []
    count = col_db.token_match_counts

    def counting(tokens, rids):
        calls.append(list(rids))
        return count(tokens, rids)

    monkeypatch.setattr(col_db, "token_match_counts", counting)
    cache: dict[str, int] = {}
    for _ in range(3):
        store._apply_exact_token_boost(
            col_db, [("DOC1::chunk_b", 0.5)], ["fox"], 0.1, counts_cache=cache,
        )

    assert calls == [["DOC1::chunk_b"]]


def test_search_runs_the_collection_default_mode(temp_data_dir):
    store = _store(temp_data_dir, "default-boost", search_mode="boost")

    out = store.search("acme", "default-boost", "quick fox", k=2)

    assert out.mode == "boost"
    assert "exact tokens 2/2" in out[0].match_reason


def test_caller_mode_overrides_the_collection_default(temp_data_dir):
    store = _store(temp_data_dir, "override", search_mode="boost")

    out = store.search("acme", "override", "quick fox", k=2, mode="vector")

    assert out.mode == "vector"
    assert "exact tokens" not in out[0].match_reason


def test_modes_the_operator_does_not_serve_are_refused(temp_data_dir):
    store = _store(temp_data_dir, "served", search_mode="hybrid")
    get_cfg().set("search.modes", ["vector"])

    for kwargs in ({}, {"mode": "boost"}):
        with pytest.raises(SearchModeError) as info:
            store.search("acme", "served", "quick fox", k=1, **kwargs)
        assert info.value.code == "search_mode_disabled"
    with pytest.raises(SearchModeError) as info:
        store.create_collection("acme", "other", search_mode="boost")
    assert info.value.code == "search_mode_disabled"


def test_raw_vector_ranks_as_vector_whatever_the_default(temp_data_dir):
    store = _store(temp_data_dir, "byov", search_mode="boost")
    vector = [0.0] * store._collection_embedders[("acme", "byov")].dim
    vector[0] = 1.0

    out = store.search("acme", "byov", "", k=1, query_vector=vector)
    assert out.mode == "vector"
    with pytest.raises(SearchModeError) as info:
        store.search(
            "acme", "byov", "", k=1, query_vector=vector, mode="boost",
        )
    assert info.value.code == "search_mode_requires_text"


def test_recreating_with_another_mode_is_a_conflict(temp_data_dir):
    store = _store(temp_data_dir, "fixed-mode", search_mode="boost")

    assert store.create_collection("acme", "fixed-mode")["search_mode"] == (
        "boost"
    )
    with pytest.raises(SearchModeError) as info:
        store.create_collection("acme", "fixed-mode", search_mode="vector")
    assert info.value.code == "search_mode_conflict"


def test_unloaded_pre_v9_collection_migrates_on_first_search(temp_data_dir):
    old = LocalStore(str(temp_data_dir), FakeEmbedder())
    old.index_records("acme", "old", "D", [("D::0", "quick fox", {})])
    old.close()
    # Turn it back into a v8 file: no settings table, no v9 migration row.
    conn = sqlite3.connect(temp_data_dir / "t_acme" / "c_old" / "meta.db")
    with conn:
        conn.execute("DROP TABLE settings")
        conn.execute("DELETE FROM schema_migrations WHERE version = 9")
    conn.close()

    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    out = store.search("acme", "old", "quick fox", k=1)

    assert out.mode == "vector"
    assert out[0].id == "D::0"


def test_auto_created_collection_records_its_mode(temp_data_dir):
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    store.index_records("acme", "auto", "D", [("D::0", "text", {})])

    info = store.create_collection("acme", "auto", search_mode="vector")
    assert info["search_mode"] == "vector"
    with pytest.raises(SearchModeError) as caught:
        store.create_collection("acme", "auto", search_mode="boost")
    assert caught.value.code == "search_mode_conflict"


@pytest.mark.parametrize(
    "search",
    [
        {"modes": ["boost", "hybrid"], "boost_weight": 0.1},
        {"modes": ["vector", "exact"], "boost_weight": 0.1},
        {"modes": ["vector"], "boost_weight": 0},
        {"modes": ["vector"], "boost_weight": 1.5},
        {"modes": ["vector"], "boost_weight": True},
    ],
)
def test_operator_search_config_is_checked_at_startup(search):
    with pytest.raises(ValueError, match="search\\."):
        _validate_search({"search": search})


def test_v9_migration_seeds_the_mode_and_keeps_the_query_log(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "t_acme" / "c_old" / "meta.db"
    migrations = dict(metadb._COLLECTION_MIGRATIONS)
    migrations.pop(9)
    with monkeypatch.context() as patch:
        patch.setattr(metadb, "_COLLECTION_SCHEMA_VERSION", 8)
        patch.setattr(metadb, "_COLLECTION_MIGRATIONS", migrations)
        db = CollectionDB()
        db.open(db_path)
        db.log_query(
            query_id="q1", tenant="acme", collection="old", actor="admin",
            query_text="fox", k=3, mode="hybrid",
        )
        db.close()

    db = CollectionDB()
    db.open(db_path)
    # No setting reads as vector, the only mode v8 collections had.
    assert db.get_setting("search_mode") is None
    assert db.get_query_log_entry("q1")["mode"] == "hybrid"
    db.log_query(
        query_id="q2", tenant="acme", collection="old", actor="admin",
        query_text="fox", k=3, mode="boost",
    )
    assert db.get_query_log_entry("q2")["mode"] == "boost"
    with pytest.raises(sqlite3.IntegrityError):
        db.log_query(
            query_id="q3", tenant="acme", collection="old", actor="admin",
            query_text="fox", k=3, mode="exact",
        )
    db.close()


def test_http_mode_errors_name_the_mistake(client, cfg):
    client.post("/v1/collections/acme/errs")
    search = "/v1/collections/acme/errs/search"
    junk = "x" * 10_000

    for response in (
        client.post(search, json={"q": "fox", "mode": "exact"}),
        client.post(search, json={"q": "fox", "mode": junk}),
        client.get("/v1/search", params={"q": "fox", "mode": junk}),
        client.post("/v1/collections/acme/other", json={"search_mode": junk}),
    ):
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "invalid_search_mode"
        assert junk not in response.text

    needs_text = client.post(search, json={"v": [0.1] * 64, "mode": "boost"})
    assert needs_text.status_code == 400
    assert needs_text.json()["code"] == "search_mode_requires_text"

    cfg.set("search.modes", ["vector"])
    disabled = client.post(search, json={"q": "fox", "mode": "boost"})
    assert disabled.status_code == 400
    assert disabled.json()["code"] == "search_mode_disabled"


def test_http_mode_round_trip(client, cfg):
    created = client.post(
        "/v1/collections/acme/modes", json={"search_mode": "boost"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["search_mode"] == "boost"
    assert client.get("/v1/collections/acme/modes/detail").json()["search_mode"] == (
        "boost"
    )
    client.post(
        "/v1/collections/acme/modes/documents",
        json={"text": "the quick brown fox", "docid": "D1"},
    )

    found = client.post(
        "/v1/collections/acme/modes/search", json={"q": "quick fox"},
    ).json()
    assert found["mode"] == "boost"
    logged = client.get(
        f"/v1/collections/acme/modes/queries/{found['query_id']}"
    ).json()["query"]
    assert logged["mode"] == "boost"
    replayed = client.post(
        f"/v1/collections/acme/modes/queries/{found['query_id']}/replay"
    ).json()
    assert replayed["mode"] == "boost"

    cfg.set("search.modes", ["vector"])
    refused = client.post(
        "/v1/collections/acme/modes/search", json={"q": "quick fox"},
    )
    assert refused.status_code == 400
    assert refused.json()["code"] == "search_mode_disabled"
    assert client.post(
        "/v1/collections/acme/modes/search",
        json={"q": "quick fox", "mode": "vector"},
    ).json()["mode"] == "vector"


def test_boost_reason_adds_up_to_the_score(temp_data_dir):
    import re

    store = _store(temp_data_dir, "reason", search_mode="boost")

    for hit in store.search("acme", "reason", "quick fox", k=2):
        found = re.match(
            r"boost: cosine (-?[\d.]+) \+ ([\d.]+) for exact tokens (\d)/2$",
            hit.match_reason,
        )
        assert found, hit.match_reason
        cosine, added = float(found[1]), float(found[2])
        assert cosine + added == pytest.approx(hit.score, abs=2e-3)
        assert added == pytest.approx(0.1 * int(found[3]) / 2, abs=1e-3)


def test_raw_vector_hits_explain_their_cosine(temp_data_dir):
    store = _store(temp_data_dir, "byov-reason")
    vector = [0.0] * store._collection_embedders[("acme", "byov-reason")].dim
    vector[0] = 1.0

    hit = store.search("acme", "byov-reason", "", k=1, query_vector=vector)[0]

    assert hit.match_reason == f"vector: cosine {hit.score:.3f}"
