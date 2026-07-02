# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from pave import metrics
from pave.stores.local import LocalStore
from utils import FakeEmbedder


class CloseTrackingEmbedder(FakeEmbedder):
    def __init__(self):
        super().__init__()
        self.close_count = 0

    def close(self):
        self.close_count += 1


def _eviction_series():
    return {
        tuple(sorted(item["labels"].items())): item["value"]
        for item in metrics.snapshot().get("cache_evictions_total", [])
    }


def test_collection_cache_evicts_lru_and_closes_handles(cfg, tmp_path):
    metrics.reset()
    cfg.set("cache.collection_max", 2)
    store = LocalStore(str(tmp_path), FakeEmbedder())

    store.index_records("acme", "c1", "doc", [("0", "alpha", {})])
    store.index_records("acme", "c2", "doc", [("0", "beta", {})])
    store.search("acme", "c1", "alpha", k=1)
    evicted_db = store._dbs[("acme", "c2")]

    store.index_records("acme", "c3", "doc", [("0", "gamma", {})])

    assert ("acme", "c1") in store._dbs
    assert ("acme", "c3") in store._dbs
    assert ("acme", "c2") not in store._dbs
    assert ("acme", "c2") not in store._emb
    assert evicted_db._rconn is None
    assert evicted_db._wconn is None
    assert store.refresh_catalog_metrics()["cache_estimated_bytes"] > 0

    series = _eviction_series()
    assert series[(("kind", "backend"),)] >= 1
    assert series[(("kind", "db"),)] >= 1
    assert 'pavedb_cache_evictions_total{kind="backend"}' in metrics.to_prometheus()


def test_collection_cache_skips_locked_lru_entry(cfg, tmp_path):
    cfg.set("cache.collection_max", 1)
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.index_records("acme", "busy", "doc", [("0", "alpha", {})])

    with store._collection_read_lock("acme", "busy"):
        store.index_records("acme", "new", "doc", [("0", "beta", {})])

    assert ("acme", "busy") in store._dbs
    assert ("acme", "new") in store._dbs


def test_embedder_cache_evicts_orphaned_runtime(tmp_path):
    store = LocalStore(str(tmp_path), FakeEmbedder())
    orphan = CloseTrackingEmbedder()
    store._embedder_cache["orphan"] = orphan
    store._embedder_cache_max = 1

    store._evict_embedder_cache_if_needed()

    assert "orphan" not in store._embedder_cache
    assert orphan.close_count == 1
