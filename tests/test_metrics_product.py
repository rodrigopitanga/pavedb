# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from pathlib import Path
import time

from pave.metrics import refresh_product_gauges


def _series(snapshot, name):
    return {
        tuple(sorted(item["labels"].items())): item["value"]
        for item in snapshot[name]
    }


def test_product_search_metrics(client, app, cfg):
    cfg.set("common_enabled", True)
    cfg.set("common_tenant", "global")
    cfg.set("common_collection", "common")
    client.post("/v1/collections/acme/metrics")
    client.post("/v1/collections/global/common")

    plain = client.get(
        "/v1/collections/acme/metrics/search",
        params={"q": "missing"},
    )
    filtered = client.post(
        "/v1/collections/acme/metrics/search",
        json={"q": "missing", "filters": {"kind": "report"}},
    )
    merged = client.post(
        "/v1/collections/acme/metrics/search",
        json={"q": "missing", "include_common": True},
    )

    assert plain.status_code == filtered.status_code == merged.status_code == 200
    refresh_product_gauges(app.state.store)
    snap = client.get("/health/metrics").json()
    labels = (("collection", "metrics"), ("tenant", "acme"))
    assert _series(snap, "searches_zero_matches_total")[labels] >= 3
    assert _series(snap, "searches_with_filters_total")[labels] >= 1
    assert _series(snap, "searches_without_filters_total")[labels] >= 2
    assert snap["searches_common_merge_total"] >= 1
    assert snap["query_log_rows"] >= 3
    assert snap["query_log_oldest_age_seconds"] >= 0

    prom = client.get("/metrics").text
    assert "# TYPE pavedb_searches_zero_matches_total counter" in prom
    assert "pavedb_searches_zero_matches_total" in prom
    assert 'collection="metrics",tenant="acme"' in prom


def test_chunk_drift_metrics(client, app):
    client.post("/v1/collections/acme/drift")
    client.post(
        "/v1/collections/acme/drift/documents",
        files={"file": ("a.txt", b"drift metrics", "text/plain")},
        data={"docid": "doc-1"},
    )
    chunk = app.state.store.list_chunks("acme", "drift", "doc-1")[0]
    path = Path(app.state.cfg.data_dir) / "t_acme" / "c_drift" / chunk["chunk_path"]
    path.unlink()
    app.state.store.impl._chunk_drift_metrics_cache = None
    refresh_product_gauges(app.state.store)

    snap = client.get("/health/metrics").json()
    assert snap["chunks_missing_sidecar_total"] >= 1


def test_search_timeout_metric(client, app):
    client.post("/v1/collections/acme/timeout")
    app.state.search_timeout_s = 0.001
    search = app.state.store.search

    def slow_search(*args, **kwargs):
        time.sleep(0.02)
        return search(*args, **kwargs)

    app.state.store.search = slow_search
    response = client.get(
        "/v1/collections/acme/timeout/search",
        params={"q": "slow"},
    )

    assert response.status_code == 503
    assert client.get("/health/metrics").json()["searches_timeout_total"] >= 1
