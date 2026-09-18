# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from pave.config import get_cfg
from pave.stores.local import LocalStore
from utils import FakeEmbedder


def test_apply_exact_token_boost_reorders_within_window(temp_data_dir):
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    tenant, collection = "acme", "boost_unit"
    store.create_collection(tenant, collection)
    store.index_records(
        tenant,
        collection,
        "DOC1",
        [
            ("DOC1::chunk_a", "totally unrelated filler text", {}),
            ("DOC1::chunk_b", "the quick brown fox", {}),
        ],
    )

    raw = [("DOC1::chunk_a", 0.9), ("DOC1::chunk_b", 0.5)]
    boosted, pre_boost = store._apply_exact_token_boost(
        tenant, collection, raw, frozenset({"fox"}), weight=1.0,
    )

    assert pre_boost == {"DOC1::chunk_b": 0.5}
    assert [rid for rid, _score in boosted][0] == "DOC1::chunk_b"


def test_apply_exact_token_boost_is_noop_without_matches(temp_data_dir):
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    tenant, collection = "acme", "boost_unit_noop"
    store.create_collection(tenant, collection)
    store.index_records(
        tenant,
        collection,
        "DOC1",
        [
            ("DOC1::chunk_a", "totally unrelated filler text", {}),
            ("DOC1::chunk_b", "the quick brown fox", {}),
        ],
    )

    raw = [("DOC1::chunk_a", 0.9), ("DOC1::chunk_b", 0.5)]
    boosted, pre_boost = store._apply_exact_token_boost(
        tenant, collection, raw, frozenset({"absent"}), weight=1.0,
    )

    assert boosted == raw
    assert pre_boost == {}


def test_apply_exact_token_boost_reads_each_chunk_once(
    temp_data_dir, monkeypatch,
):
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    tenant, collection = "acme", "boost_unit_cache"
    store.create_collection(tenant, collection)
    store.index_records(
        tenant,
        collection,
        "DOC1",
        [("DOC1::chunk_a", "the quick brown fox", {})],
    )

    reads: list[str] = []
    load_chunk_text = store._load_chunk_text

    def counting_load(*args, **kwargs):
        reads.append(args[2])
        return load_chunk_text(*args, **kwargs)

    monkeypatch.setattr(store, "_load_chunk_text", counting_load)

    # Filters make one search re-fetch a wider window; the shared cache
    # keeps that from re-reading a candidate's text every round.
    raw = [("DOC1::chunk_a", 0.5)]
    token_cache: dict[str, frozenset[str]] = {}
    for _ in range(3):
        store._apply_exact_token_boost(
            tenant, collection, raw, frozenset({"fox"}), 1.0,
            token_cache=token_cache,
        )

    assert reads == ["DOC1::chunk_a"]


def test_search_exact_token_boost_disabled_by_default(temp_data_dir):
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    tenant, collection = "acme", "boost_default"
    store.create_collection(tenant, collection)
    store.index_records(
        tenant, collection, "DOC1",
        [("DOC1::chunk_0", "quantum entanglement", {})],
    )

    assert get_cfg().get("search.exact_token_boost") is False

    result = store.search(tenant, collection, "quantum entanglement", k=1)

    assert "exact token match" not in result[0].match_reason


def test_search_exact_token_boost_enabled_marks_match_reason(temp_data_dir):
    get_cfg().set("search.exact_token_boost", True)
    get_cfg().set("search.exact_token_boost_weight", 0.2)
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    tenant, collection = "acme", "boost_enabled"
    store.create_collection(tenant, collection)
    store.index_records(
        tenant, collection, "DOC1",
        [("DOC1::chunk_0", "quantum entanglement", {})],
    )

    get_cfg().set("search.exact_token_boost", False)
    baseline_score = store.search(
        tenant, collection, "quantum entanglement", k=1,
    )[0].score

    get_cfg().set("search.exact_token_boost", True)
    result = store.search(tenant, collection, "quantum entanglement", k=1)

    assert "exact token match" in result[0].match_reason
    assert result[0].score > baseline_score


def test_search_exact_token_boost_weight_zero_is_noop(temp_data_dir):
    get_cfg().set("search.exact_token_boost", True)
    get_cfg().set("search.exact_token_boost_weight", 0)
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    tenant, collection = "acme", "boost_zero_weight"
    store.create_collection(tenant, collection)
    store.index_records(
        tenant, collection, "DOC1",
        [("DOC1::chunk_0", "quantum entanglement", {})],
    )

    result = store.search(tenant, collection, "quantum entanglement", k=1)

    assert "exact token match" not in result[0].match_reason


def test_search_exact_token_boost_keeps_the_percentage_a_similarity(
    temp_data_dir,
):
    get_cfg().set("search.exact_token_boost", True)
    get_cfg().set("search.exact_token_boost_weight", 0.2)
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    tenant, collection = "acme", "boost_reason"
    store.create_collection(tenant, collection)
    store.index_records(
        tenant, collection, "DOC1",
        [("DOC1::chunk_0", "quantum entanglement", {})],
    )

    result = store.search(tenant, collection, "quantum entanglement", k=1)[0]

    # The boost lifts the score past the cosine ceiling, so the reported
    # percentage has to stay the similarity the index produced.
    assert result.score > 1.0
    assert "exact token match" in result.match_reason
    percent = int(
        result.match_reason.split("semantic similarity ")[1].split("%")[0]
    )
    assert percent <= 100


def test_search_survives_a_non_numeric_boost_weight(temp_data_dir):
    get_cfg().set("search.exact_token_boost_weight", "off")
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    tenant, collection = "acme", "boost_bad_weight"
    store.create_collection(tenant, collection)
    store.index_records(
        tenant, collection, "DOC1",
        [("DOC1::chunk_0", "quantum entanglement", {})],
    )

    # A malformed weight must not reach the search path at all while the
    # boost is off, and must read as "disabled" once it is on.
    disabled = store.search(tenant, collection, "quantum entanglement", k=1)
    assert disabled[0].id == "DOC1::chunk_0"

    get_cfg().set("search.exact_token_boost", True)
    enabled = store.search(tenant, collection, "quantum entanglement", k=1)

    assert enabled[0].id == "DOC1::chunk_0"
    assert "exact token match" not in enabled[0].match_reason
