# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from pave.stores.local import LocalStore
from utils import FakeEmbedder


def test_search_returns_phase_timing_and_list_like_matches(temp_data_dir):
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    tenant, collection = "acme", "timed"
    store.create_collection(tenant, collection)
    store.index_records(
        tenant,
        collection,
        "DOC1",
        [
            ("DOC1::chunk_0", "alpha beta gamma", {"lang": "en"}),
            ("DOC1::chunk_1", "delta epsilon zeta", {"lang": "en"}),
        ],
    )

    result = store.search(tenant, collection, "alpha", k=5)

    assert len(result) >= 1
    assert result[0].id.endswith("chunk_0")
    assert [hit.id for hit in result]
    assert set(result.timing) == {
        "embed_ms",
        "search_ms",
        "filter_ms",
        "hydrate_ms",
    }
    assert all(value >= 0.0 for value in result.timing.values())


def test_search_filter_timing_is_exposed_with_filters(temp_data_dir):
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    tenant, collection = "acme", "timed_filters"
    store.create_collection(tenant, collection)
    store.index_records(
        tenant,
        collection,
        "DOC2",
        [
            ("DOC2::chunk_0", "alpha beta gamma", {"lang": "en"}),
            ("DOC2::chunk_1", "alpha beta gamma", {"lang": "pt"}),
        ],
    )

    result = store.search(
        tenant,
        collection,
        "alpha",
        k=5,
        filters={"lang": "en"},
    )

    assert len(result) == 1
    assert result[0].meta["lang"] == "en"
    assert result.timing["filter_ms"] >= 0.0
