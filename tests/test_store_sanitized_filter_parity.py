# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from typing import Any

import pytest

from pave.stores.local import LocalStore
from utils import FakeEmbedder


def _snapshot(results: list[Any]) -> list[tuple[str, float, dict[str, Any]]]:
    return [
        (hit.id, round(float(hit.score), 6), dict(hit.meta))
        for hit in results
    ]


def _search_without_pushdown(
    store: LocalStore,
    tenant: str,
    collection: str,
    query: str,
    filters: dict[str, Any] | None,
) -> list[Any]:
    col_db = store._dbs[(tenant, collection)]
    original = col_db.filter_by_meta

    def _no_pushdown(rids: list[str], _filters: dict[str, list[Any]]) -> set[str]:
        return set(rids)

    col_db.filter_by_meta = _no_pushdown  # type: ignore[method-assign]
    try:
        return store.search(
            tenant,
            collection,
            query,
            k=5,
            filters=filters,
        )
    finally:
        col_db.filter_by_meta = original  # type: ignore[method-assign]


@pytest.fixture()
def sanitized_store(tmp_path) -> tuple[LocalStore, str, str, str, str]:
    store = LocalStore(str(tmp_path), FakeEmbedder())
    tenant = "tenant"
    collection = "sanitized_filters"
    exact_value = "x'; DROP TABLE chunk_meta; --"
    wildcard_value = "needle';select"

    store.index_records(
        tenant,
        collection,
        "doc1",
        [
            (
                "r1",
                "shared token alpha",
                {"na me": exact_value, "ta g": wildcard_value},
            ),
            (
                "r2",
                "shared token beta",
                {"na me": "plain", "ta g": "other"},
            ),
        ],
        doc_meta={"so urce": exact_value},
    )
    store.index_records(
        tenant,
        collection,
        "doc2",
        [
            (
                "r3",
                "shared token gamma",
                {"na me": "other", "ta g": "backup"},
            ),
        ],
        doc_meta={"so urce": "plain-source"},
    )

    return store, tenant, collection, exact_value, wildcard_value


def test_sqlish_doc_metadata_exact_filter_matches_with_and_without_pushdown(
    sanitized_store,
):
    store, tenant, collection, exact_value, _wildcard_value = sanitized_store
    filters = {"so urce": exact_value}

    pushed = _snapshot(store.search(tenant, collection, "shared token", 5, filters))
    canonical = _snapshot(
        _search_without_pushdown(
            store,
            tenant,
            collection,
            "shared token",
            filters,
        )
    )

    assert pushed == canonical
    assert {rid for rid, _score, _meta in pushed} == {"doc1::r1", "doc1::r2"}
    assert all(
        meta["so_urce"] == exact_value
        for _rid, _score, meta in pushed
    )


def test_sqlish_chunk_metadata_exact_filter_matches_with_and_without_pushdown(
    sanitized_store,
):
    store, tenant, collection, exact_value, _wildcard_value = sanitized_store
    filters = {"na me": exact_value}

    pushed = _snapshot(store.search(tenant, collection, "shared token", 5, filters))
    canonical = _snapshot(
        _search_without_pushdown(
            store,
            tenant,
            collection,
            "shared token",
            filters,
        )
    )

    assert pushed == canonical
    assert {rid for rid, _score, _meta in pushed} == {"doc1::r1"}
    assert pushed[0][2]["na_me"] == exact_value


def test_sqlish_chunk_metadata_wildcard_filter_matches_with_and_without_pushdown(
    sanitized_store,
):
    store, tenant, collection, _exact_value, wildcard_value = sanitized_store
    filters = {"ta g": f"*{wildcard_value}*"}

    col_db = store._dbs[(tenant, collection)]
    seen: dict[str, object] = {}
    original = col_db.filter_by_meta

    def _spy_filter_by_meta(rids: list[str], filt: dict[str, list[Any]]) -> set[str]:
        seen["filters"] = filt
        return original(rids, filt)

    col_db.filter_by_meta = _spy_filter_by_meta  # type: ignore[method-assign]
    try:
        pushed = _snapshot(store.search(tenant, collection, "shared token", 5, filters))
    finally:
        col_db.filter_by_meta = original  # type: ignore[method-assign]

    canonical = _snapshot(
        _search_without_pushdown(
            store,
            tenant,
            collection,
            "shared token",
            filters,
        )
    )

    assert seen["filters"] == {"ta_g": [f"*{wildcard_value}*"]}
    assert pushed == canonical
    assert {rid for rid, _score, _meta in pushed} == {"doc1::r1"}
    assert pushed[0][2]["ta_g"] == wildcard_value


def test_distinct_sqlish_values_remain_distinct_in_filters(tmp_path):
    store = LocalStore(str(tmp_path), FakeEmbedder())
    tenant, collection = "tenant", "distinct_values"
    values = (
        ("doc1", "a'; -- first", "x/* first"),
        ("doc2", "a'; -- second", "x/* second"),
    )
    for docid, source, tag in values:
        store.index_records(
            tenant,
            collection,
            docid,
            [("r1", "shared token", {"tag": tag})],
            doc_meta={"source": source},
        )

    for docid, source, tag in values:
        for filters in ({"source": source}, {"tag": tag}):
            pushed = _snapshot(
                store.search(tenant, collection, "shared token", 5, filters)
            )
            canonical = _snapshot(
                _search_without_pushdown(
                    store, tenant, collection, "shared token", filters
                )
            )
            assert pushed == canonical
            assert {rid for rid, _score, _meta in pushed} == {f"{docid}::r1"}
            assert pushed[0][2]["source"] == source
            assert pushed[0][2]["tag"] == tag


def test_negated_filter_keeps_chunk_metadata_overrides(tmp_path):
    store = LocalStore(str(tmp_path), FakeEmbedder())
    tenant, collection = "tenant", "negated_filter_override"
    store.index_records(
        tenant,
        collection,
        "doc1",
        [("r1", "shared token", {"visibility": "public"})],
        doc_meta={"visibility": "private"},
    )
    filters = {"visibility": "!private"}

    pushed = _snapshot(store.search(tenant, collection, "shared", 1, filters))
    canonical = _snapshot(
        _search_without_pushdown(store, tenant, collection, "shared", filters)
    )

    assert pushed == canonical
    assert [rid for rid, _score, _meta in pushed] == ["doc1::r1"]
