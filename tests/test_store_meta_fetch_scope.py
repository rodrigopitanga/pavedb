# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import sqlite3
import time
from typing import Any

import numpy as np
import pytest

from pave.stores.local import _PREFILTER_MAX_SELECTIVITY, LocalStore
from utils import FakeEmbedder

# Long enough that the screen dominates every other filter phase in the
# assertion below, short enough to stay cheap.
_SCREEN_DELAY_MS = 50.0

# Host parameters one statement can bind on pre-3.32 SQLite, and a
# collection past it: the k below overfetches 1250 candidates, so every
# rid-list query on the fallback path has to batch under the floor.
_LEGACY_SQL_VARS = 999
_OVER_FLOOR_RECORDS = 1200
_OVER_FLOOR_K = 250


def _seed_records(count: int) -> list[tuple[str, str, dict]]:
    return [
        (f"r{i}", "shared query token", {"lang": "en", "chunk": i})
        for i in range(count)
    ]


class _RankedEmbedder:
    @property
    def dim(self) -> int:
        return 2

    def encode(self, texts: list[str]) -> np.ndarray:
        rows = []
        for text in texts:
            if text == "needle query":
                rows.append([1.0, 0.0])
            elif text == "target payload":
                rows.append([-1.0, 0.0])
            else:
                rows.append([1.0, 0.0])
        return np.asarray(rows, dtype=np.float32)


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


@pytest.mark.parametrize("other_count", [0, 19])
def test_filtered_search_hydrates_from_filter_snapshot(
    temp_data_dir, other_count,
):
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    tenant, collection = "tenant", "meta_scope_snapshot"
    records = _seed_records(1) + [
        (f"other{i}", "shared query token", {"lang": "pt"})
        for i in range(other_count)
    ]
    store.index_records(tenant, collection, "doc", records)

    col_db = store._dbs[(tenant, collection)]
    original_filter = col_db.filter_by_meta

    def _filter_then_mutate(rids, filters):
        surviving = original_filter(rids, filters)
        rid = next(iter(surviving))
        col_db.upsert_chunks("doc", [(rid, None, {"lang": "pt"})])
        return surviving

    col_db.filter_by_meta = _filter_then_mutate  # type: ignore[method-assign]

    hits = store.search(
        tenant,
        collection,
        "shared",
        k=1,
        filters={"lang": "en"},
    )

    assert len(hits) == 1
    assert {hit.meta["lang"] for hit in hits} == {"en"}
    current_meta = col_db.get_meta_batch([hit.id for hit in hits])
    assert {
        meta["lang"] for meta in current_meta.values()
    } == {"pt"}


def test_filtered_search_expands_candidate_window_until_match(temp_data_dir):
    store = LocalStore(str(temp_data_dir), _RankedEmbedder())
    tenant, collection = "tenant", "meta_scope_expand"
    store.index_records(
        tenant,
        collection,
        "other",
        [
            (f"r{i}", "near payload", {"shape": "other"})
            for i in range(60)
        ],
    )
    store.index_records(
        tenant,
        collection,
        "target",
        [("r0", "target payload", {"shape": "target"})],
    )

    backend = store._emb[(tenant, collection)]
    seen_limits: list[int] = []
    original_search = backend.search
    original_search_filtered = backend.search_filtered

    # Backends without the optional capability retain the vector-first
    # expansion path.
    backend.search_filtered = None  # type: ignore[method-assign]

    def _spy_search(vector, limit):
        seen_limits.append(limit)
        return original_search(vector, limit)

    backend.search = _spy_search  # type: ignore[method-assign]

    hits = store.search(
        tenant,
        collection,
        "needle query",
        k=1,
        filters={"shape": "target"},
    )

    assert [hit.id for hit in hits] == ["target::r0"]
    assert seen_limits[0] == 50
    assert max(seen_limits) > 50
    backend.search_filtered = original_search_filtered  # type: ignore[method-assign]


def test_filtered_search_constrains_vector_work_to_sql_selection(temp_data_dir):
    store = LocalStore(str(temp_data_dir), _RankedEmbedder())
    tenant, collection = "tenant", "meta_scope_backend_filter"
    store.index_records(
        tenant,
        collection,
        "other",
        [
            (f"r{i}", "near payload", {"shape": "other"})
            for i in range(60)
        ],
    )
    store.index_records(
        tenant,
        collection,
        "target",
        [("r0", "target payload", {"shape": "target"})],
    )

    backend = store._emb[(tenant, collection)]
    seen: list[tuple[int, set[str]]] = []
    normal_search_calls = 0
    original_search = backend.search
    original_search_filtered = backend.search_filtered
    col_db = store._dbs[(tenant, collection)]
    original_get_meta_batch = col_db.get_meta_batch
    hydrated: list[list[str]] = []

    def _spy_search(vector, limit):
        nonlocal normal_search_calls
        normal_search_calls += 1
        return original_search(vector, limit)

    def _spy_search_filtered(vector, limit, allowed_rids):
        seen.append((limit, set(allowed_rids)))
        return original_search_filtered(vector, limit, allowed_rids)

    def _spy_get_meta_batch(rids):
        hydrated.append(list(rids))
        return original_get_meta_batch(rids)

    backend.search = _spy_search  # type: ignore[method-assign]
    backend.search_filtered = _spy_search_filtered  # type: ignore[method-assign]
    col_db.get_meta_batch = _spy_get_meta_batch  # type: ignore[method-assign]

    hits = store.search(
        tenant,
        collection,
        "needle query",
        k=1,
        filters={"shape": "target"},
    )

    assert [hit.id for hit in hits] == ["target::r0"]
    assert seen == [(1, {"target::r0"})]
    assert normal_search_calls == 0
    assert hydrated == [["target::r0"]]


@pytest.mark.parametrize(
    "filters, content_filter",
    [
        ({"shape": "other"}, None),
        (None, {"op": "prefix", "value": "near"}),
    ],
)
def test_broad_filter_skips_full_collection_prefilter(
    temp_data_dir, filters, content_filter,
):
    store = LocalStore(str(temp_data_dir), _RankedEmbedder())
    tenant, collection = "tenant", "meta_scope_broad_filter"
    store.index_records(
        tenant,
        collection,
        "other",
        [
            (f"r{i}", "near payload", {"shape": "other"})
            for i in range(60)
        ],
    )
    store.index_records(
        tenant,
        collection,
        "target",
        [("r0", "target payload", {"shape": "target"})],
    )

    backend = store._emb[(tenant, collection)]
    col_db = store._dbs[(tenant, collection)]
    assert len(col_db.select_filter_candidates(
        {"shape": ["other"]},
        {"op": "prefix", "value": "near"},
        62,
    )) == 60

    def _unexpected(*_args, **_kwargs):
        pytest.fail("broad filter enumerated the whole collection")

    backend.search_filtered = _unexpected  # type: ignore[method-assign]
    col_db.get_chunk_rids = _unexpected  # type: ignore[method-assign]

    hits = store.search(
        tenant,
        collection,
        "needle query",
        k=3,
        filters=filters,
        content_filter=content_filter,
    )

    assert len(hits) == 3
    assert {hit.meta["shape"] for hit in hits} == {"other"}


def test_selectivity_screen_selects_candidates_once_and_is_timed(
    temp_data_dir,
):
    store = LocalStore(str(temp_data_dir), _RankedEmbedder())
    tenant, collection = "tenant", "meta_scope_screen_once"
    store.index_records(
        tenant,
        collection,
        "other",
        [
            (f"r{i}", "near payload", {"shape": "other"})
            for i in range(60)
        ],
    )
    store.index_records(
        tenant,
        collection,
        "target",
        [("r0", "target payload", {"shape": "target"})],
    )

    col_db = store._dbs[(tenant, collection)]
    original_select = col_db.select_filter_candidates
    limits: list[int | None] = []

    def _spy_select(filters, content_filter, limit=None):
        limits.append(limit)
        time.sleep(_SCREEN_DELAY_MS / 1000.0)
        return original_select(filters, content_filter, limit)

    col_db.select_filter_candidates = _spy_select  # type: ignore[method-assign]

    hits = store.search(
        tenant,
        collection,
        "needle query",
        k=1,
        filters={"shape": "target"},
    )

    assert [hit.id for hit in hits] == ["target::r0"]
    # One capped pass both screens selectivity and keeps the eligible IDs:
    # the predicate drives a full chunks scan, so selecting twice would
    # double it for exactly the selective filters this path accelerates.
    assert limits == [int(61 * _PREFILTER_MAX_SELECTIVITY) + 1]
    # The screen applies the metadata filter, so its cost belongs to
    # filter_ms. Reported milliseconds are rounded, hence the slack.
    assert hits.timing["filter_ms"] > _SCREEN_DELAY_MS - 1.0


def test_many_exact_filter_values_use_vector_first_on_legacy_sqlite(
    temp_data_dir,
):
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    tenant, collection = "tenant", "meta_scope_many_filter_values"
    store.index_records(tenant, collection, "doc", _seed_records(20))

    col_db = store._dbs[(tenant, collection)]
    if hasattr(col_db._rconn, "setlimit"):
        col_db._rconn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)

    filters = {"lang": [f"miss{i}" for i in range(499)] + ["en"]}
    hits = store.search(tenant, collection, "shared", k=5, filters=filters)

    assert len(hits) == 5
    assert {hit.meta["lang"] for hit in hits} == {"en"}


def _legacy_variable_limit(store, tenant, collection):
    """Pin the collection reader to the pre-3.32 variable floor."""
    col_db = store._dbs[(tenant, collection)]
    if not hasattr(col_db._rconn, "setlimit"):
        pytest.skip("sqlite3.Connection.setlimit requires Python 3.11+")
    col_db._rconn.setlimit(
        sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, _LEGACY_SQL_VARS
    )


@pytest.mark.parametrize(
    "filters",
    [
        pytest.param({"lang": ["en"]}, id="meta-key"),
        pytest.param({"docid": ["doc"]}, id="docid"),
    ],
)
def test_broad_filter_fallback_batches_under_legacy_sqlite(
    temp_data_dir, filters,
):
    """The vector-first path the prefilter guard defers to must also fit.

    The guard sends over-wide predicates here, and this path used to bind
    the rid batch plus its key and value in one statement.
    """
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    tenant, collection = "tenant", "meta_scope_legacy_fallback"
    store.index_records(
        tenant, collection, "doc", _seed_records(_OVER_FLOOR_RECORDS)
    )
    _legacy_variable_limit(store, tenant, collection)

    # A broad filter keeps every overfetched candidate eligible.
    hits = store.search(
        tenant, collection, "shared", k=_OVER_FLOOR_K, filters=filters,
    )

    assert len(hits) == _OVER_FLOOR_K
    assert {hit.meta["lang"] for hit in hits} == {"en"}


def test_boost_token_counts_batch_under_legacy_sqlite(temp_data_dir):
    """Token counting binds its token alongside the rid batch."""
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    tenant, collection = "tenant", "meta_scope_legacy_boost"
    store.index_records(
        tenant, collection, "doc", _seed_records(_OVER_FLOOR_RECORDS)
    )
    _legacy_variable_limit(store, tenant, collection)

    hits = store.search(
        tenant, collection, "shared", k=_OVER_FLOOR_K, mode="boost",
    )

    assert len(hits) == _OVER_FLOOR_K


def test_backend_filtered_search_matches_vector_first_fallback(temp_data_dir):
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    tenant, collection = "tenant", "meta_scope_backend_parity"
    store.index_records(
        tenant,
        collection,
        "doc",
        [("r1", "alpha one", {"lang": "en"})] + [
            (f"r{i}", f"alpha {i}", {"lang": "pt"})
            for i in range(2, 21)
        ],
    )

    backend = store._emb[(tenant, collection)]
    original_search_filtered = backend.search_filtered
    filtered_calls = 0

    def _spy_search_filtered(vector, limit, allowed_rids):
        nonlocal filtered_calls
        filtered_calls += 1
        return original_search_filtered(vector, limit, allowed_rids)

    backend.search_filtered = _spy_search_filtered  # type: ignore[method-assign]
    accelerated = store.search(
        tenant,
        collection,
        "alpha",
        k=1,
        filters={"lang": "en"},
    )

    backend.search_filtered = None  # type: ignore[method-assign]
    fallback = store.search(
        tenant,
        collection,
        "alpha",
        k=1,
        filters={"lang": "en"},
    )
    backend.search_filtered = original_search_filtered  # type: ignore[method-assign]

    assert filtered_calls == 1
    assert [hit.id for hit in accelerated] == [hit.id for hit in fallback]
    assert [hit.score for hit in accelerated] == pytest.approx(
        [hit.score for hit in fallback]
    )
