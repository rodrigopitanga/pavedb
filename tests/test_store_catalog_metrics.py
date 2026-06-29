# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from pathlib import Path

from pave.stores.local import LocalStore
from utils import FakeEmbedder


def test_catalog_metrics_counts_docs_and_chunks(cfg, tmp_path):
    cfg.set("data_dir", str(tmp_path))
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.index_records(
        "acme",
        "c1",
        "doc1",
        [("0", "alpha", {"lang": "en"}), ("1", "beta", {"lang": "en"})],
    )
    store.index_records(
        "acme",
        "c1",
        "doc2",
        [("0", "gamma", {"lang": "en"})],
    )
    store.index_records(
        "acme",
        "c2",
        "doc3",
        [("0", "delta", {"lang": "en"})],
    )

    # Empty tenant directories are not cataloged tenants anymore.
    data_dir = Path(cfg.get("data_dir"))
    (data_dir / "t_empty").mkdir(parents=True, exist_ok=True)

    snap = store.refresh_catalog_metrics()
    assert snap["tenant_count"] == 1
    assert snap["collection_count"] == 2
    assert snap["doc_count"] == 3
    assert snap["chunk_count"] == 4


def test_catalog_metrics_is_cache_only(cfg, tmp_path, monkeypatch):
    cfg.set("data_dir", str(tmp_path))
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.index_records(
        "acme", "c1", "doc1", [("0", "alpha", {"lang": "en"})]
    )

    calls = {"n": 0}
    real = store._compute_catalog_metrics

    def counting():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(store, "_compute_catalog_metrics", counting)

    first = store.refresh_catalog_metrics()
    second = store.catalog_metrics()
    assert calls["n"] == 1  # second call served from cache
    assert first == second

    # Returned dict is a copy: mutating it must not poison the cache.
    second["doc_count"] = 999
    assert store.catalog_metrics()["doc_count"] == first["doc_count"]
    assert calls["n"] == 1

    # Once the TTL window lapses, the next call recomputes.
    ts, value = store._catalog_metrics_cache
    store._catalog_metrics_cache = (ts - 3600.0, value)
    store.catalog_metrics()
    assert calls["n"] == 1
    assert store.catalog_metrics_freshness()[1] is True

    refreshed = store.refresh_catalog_metrics()
    assert calls["n"] == 2
    assert refreshed == first


def test_catalog_probe_does_not_walk_collections(cfg, tmp_path):
    cfg.set("data_dir", str(tmp_path))
    store = LocalStore(str(tmp_path), FakeEmbedder())

    # Succeeds on an empty catalog.
    assert store.catalog_probe() is None

    store.index_records(
        "acme", "c1", "doc1", [("0", "alpha", {"lang": "en"})]
    )

    # Unlike catalog_metrics, the probe never opens a collection's meta.db,
    # so a closed cached CollectionDB handle must not affect it.
    store._dbs[("acme", "c1")].close()
    assert store.catalog_probe() is None
