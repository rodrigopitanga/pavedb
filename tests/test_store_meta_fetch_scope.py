# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from typing import Any

from pave.stores.local import LocalStore
from utils import FakeEmbedder


def _seed_records(count: int) -> list[tuple[str, str, dict]]:
    return [
        (f"r{i}", "shared query token", {"lang": "en", "chunk": i})
        for i in range(count)
    ]


def test_search_fetches_meta_for_all_candidates_without_post_filters(
    temp_data_dir,
):
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    tenant, collection = "tenant", "meta_scope_plain"
    store.index_records(tenant, collection, "doc", _seed_records(12))

    col_db = store._dbs[(tenant, collection)]
    seen_batches: list[list[str]] = []
    orig_get_meta_batch = col_db.get_meta_batch

    def _spy_get_meta_batch(rids: list[str]):
        seen_batches.append(list(rids))
        return orig_get_meta_batch(rids)

    col_db.get_meta_batch = _spy_get_meta_batch  # type: ignore[method-assign]

    hits = store.search(tenant, collection, "shared", k=5)
    assert len(hits) == 5
    assert seen_batches
    assert len(seen_batches[0]) > 5


def test_search_fetches_extended_meta_batch_with_post_filters(temp_data_dir):
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    tenant, collection = "tenant", "meta_scope_post"
    store.index_records(tenant, collection, "doc", _seed_records(12))

    col_db = store._dbs[(tenant, collection)]
    seen_batches: list[list[str]] = []
    orig_get_meta_batch = col_db.get_meta_batch

    def _spy_get_meta_batch(rids: list[str]):
        seen_batches.append(list(rids))
        return orig_get_meta_batch(rids)

    col_db.get_meta_batch = _spy_get_meta_batch  # type: ignore[method-assign]

    hits = store.search(
        tenant,
        collection,
        "shared",
        k=5,
        filters={"lang": "*n"},
    )
    assert len(hits) == 5
    assert seen_batches
    assert len(seen_batches[0]) > 5


def test_search_pushdown_receives_full_normed_filters(temp_data_dir):
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    tenant, collection = "tenant", "meta_scope_pushdown_mix"
    store.index_records(
        tenant,
        collection,
        "doc",
        [
            (
                "r1",
                "shared query token",
                {"lang": "en", "category": "ml", "size": 50},
            ),
            (
                "r2",
                "shared query token",
                {"lang": "en", "category": "infra", "size": 150},
            ),
            (
                "r3",
                "shared query token",
                {"lang": "pt", "category": "infra", "size": 250},
            ),
        ],
    )

    col_db = store._dbs[(tenant, collection)]
    seen: dict[str, object] = {}
    orig_filter_by_meta = col_db.filter_by_meta

    def _spy_filter_by_meta(rids: list[str], filters: dict[str, list[Any]]):
        seen["rids"] = list(rids)
        seen["filters"] = filters
        return orig_filter_by_meta(rids, filters)

    col_db.filter_by_meta = _spy_filter_by_meta  # type: ignore[method-assign]

    hits = store.search(
        tenant,
        collection,
        "shared",
        k=5,
        filters={
            "lang": ["en", "!pt"],
            "category": ["*fra"],
            "size": [">100"],
        },
    )

    assert seen["filters"] == {
        "lang": ["en", "!pt"],
        "category": ["*fra"],
        "size": [">100"],
    }
    assert seen["rids"]
    assert [hit.id.split("::")[-1] for hit in hits] == ["r2"]


def test_search_calls_pushdown_for_postfilter_only_conditions(temp_data_dir):
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    tenant, collection = "tenant", "meta_scope_post_only"
    store.index_records(tenant, collection, "doc", _seed_records(6))

    col_db = store._dbs[(tenant, collection)]
    called = False

    seen: dict[str, object] = {}

    def _spy_filter_by_meta(_rids: list[str], _filters: dict[str, list[Any]]):
        nonlocal called
        called = True
        seen["filters"] = _filters
        return set(_rids)

    col_db.filter_by_meta = _spy_filter_by_meta  # type: ignore[method-assign]

    hits = store.search(
        tenant,
        collection,
        "shared",
        k=5,
        filters={"lang": "*n"},
    )

    assert called is True
    assert seen["filters"] == {"lang": ["*n"]}
    assert len(hits) == 5
