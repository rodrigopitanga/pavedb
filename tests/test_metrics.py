# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import json, os
import threading
from pave.main import VERSION
from pave import metrics

def test_metrics_json(client):
    r = client.get("/health/metrics")
    assert r.status_code == 200
    assert "uptime_seconds" in r.json()

def test_metrics_counters(client):
    # create, upload, search -> counters move
    r = client.post("/v1/collections/acme/m", headers={})
    assert r.status_code == 201
    r = client.post("/v1/collections/acme/m/documents",
                    files={"file": ("a.txt", b"hello world", "text/plain")},
                    data={"docid": "D1"})
    assert r.status_code == 201

    r = client.get("/v1/collections/acme/m/search", params={"q": "hello", "k": 5})
    assert r.status_code == 200

    snap = client.get("/health/metrics").json()
    assert snap["collections_created_total"] >= 1
    assert snap["documents_indexed_total"] >= 1
    assert snap["chunks_indexed_total"] >= 1
    assert snap["search_total"] >= 1
    assert snap["requests_total"] >= 3  # create + upload + search

def test_http_request_metrics_are_labeled_by_route_template(client):
    client.get("/health/live")
    client.post("/v1/collections/acme/http-metrics")

    snap = client.get("/health/metrics").json()
    series = {
        tuple(sorted(item["labels"].items())): item["value"]
        for item in snap["http_requests_total"]
    }

    assert series[
        (
            ("method", "GET"),
            ("path", "/health/live"),
            ("status_class", "2xx"),
        )
    ] >= 1
    assert series[
        (
            ("method", "POST"),
            ("path", "/v1/collections/{tenant}/{name}"),
            ("status_class", "2xx"),
        )
    ] >= 1

def test_metrics_exposes_build_labels(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    txt = r.text
    assert "version" in txt and VERSION in txt

def test_http_request_metrics_prometheus_labels(client):
    client.get("/health/live")

    r = client.get("/metrics")
    assert r.status_code == 200
    assert (
        'pavedb_http_requests_total{method="GET",path="/health/live",'
        'status_class="2xx"}'
    ) in r.text


def test_health_metrics_include_store_catalog_counts(client):
    client.post("/v1/collections/acme/c1")
    client.post("/v1/collections/acme/c2")
    client.post("/v1/collections/beta/c1")

    client.post(
        "/v1/collections/acme/c1/documents",
        files={"file": ("a.txt", b"alpha", "text/plain")},
        data={"docid": "A1"},
    )
    client.post(
        "/v1/collections/beta/c1/documents",
        files={"file": ("b.txt", b"beta", "text/plain")},
        data={"docid": "B1"},
    )

    client.app.state.store.refresh_catalog_metrics()
    snap = client.get("/health/metrics").json()
    assert snap["tenant_count"] >= 2
    assert snap["collection_count"] >= 3
    assert snap["doc_count"] >= 2
    assert snap["chunk_count"] >= 2
    assert snap["catalog_metrics_stale"] == 0
    assert snap["catalog_metrics_age_s"] >= 0


def test_metrics_prometheus_includes_store_catalog_counts(client):
    client.post("/v1/collections/acme/promc")
    client.app.state.store.refresh_catalog_metrics()
    r = client.get("/metrics")
    assert r.status_code == 200
    txt = r.text
    assert "pavedb_tenant_count" in txt
    assert "pavedb_collection_count" in txt
    assert "pavedb_doc_count" in txt
    assert "pavedb_chunk_count" in txt
    assert "pavedb_catalog_metrics_stale" in txt


def test_health_metrics_catalog_read_is_cache_only(client, monkeypatch):
    calls = {"n": 0}

    def fail_if_walked():
        calls["n"] += 1
        raise AssertionError("request path walked catalog metrics")

    monkeypatch.setattr(
        client.app.state.store.impl,
        "_compute_catalog_metrics",
        fail_if_walked,
    )

    snap = client.get("/health/metrics").json()

    assert calls["n"] == 0
    assert snap["catalog_metrics_age_s"] == -1.0
    assert snap["catalog_metrics_stale"] == 1

def test_latency_percentiles_in_snapshot(client):
    """After search and ingest, latency percentiles should appear in metrics."""
    # create collection and ingest a document
    client.post("/v1/collections/acme/lat", headers={})
    client.post("/v1/collections/acme/lat/documents",
                files={"file": ("b.txt", b"latency test content", "text/plain")},
                data={"docid": "D2"})
    # perform a search
    client.get("/v1/collections/acme/lat/search", params={"q": "latency", "k": 5})

    snap = client.get("/health/metrics").json()
    # Check search latency fields
    assert "search_latency_p50_ms" in snap
    assert "search_latency_p95_ms" in snap
    assert "search_latency_p99_ms" in snap
    assert "search_latency_count" in snap
    assert snap["search_latency_count"] >= 1
    # Check ingest latency fields
    assert "ingest_latency_p50_ms" in snap
    assert "ingest_latency_p95_ms" in snap
    assert "ingest_latency_p99_ms" in snap
    assert "ingest_latency_count" in snap
    assert snap["ingest_latency_count"] >= 1

def test_latency_prometheus_format(client):
    """Latency percentiles should be exported in Prometheus format."""
    client.post("/v1/collections/acme/prom", headers={})
    client.post("/v1/collections/acme/prom/documents",
                files={"file": ("c.txt", b"prometheus test", "text/plain")},
                data={"docid": "D3"})
    client.get("/v1/collections/acme/prom/search", params={"q": "prometheus", "k": 5})

    r = client.get("/metrics")
    txt = r.text
    assert "pavedb_search_latency_p50_ms" in txt
    assert "pavedb_search_latency_p95_ms" in txt
    assert "pavedb_search_latency_p99_ms" in txt
    assert "pavedb_ingest_latency_p50_ms" in txt
    assert "pavedb_ingest_latency_p95_ms" in txt
    assert "pavedb_ingest_latency_p99_ms" in txt

def test_percentile_calculation():
    """Unit test for percentile calculation."""
    # Clear existing samples
    metrics._latencies["test_op"] = metrics.deque(maxlen=100)
    # Add known samples
    for v in [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
        metrics.record_latency("test_op", float(v))
    pcts = metrics.latency_percentiles("test_op")
    assert pcts["count"] == 10
    assert pcts["p50"] == 55.0  # median of 10 values
    assert pcts["p95"] >= 90.0
    assert pcts["p99"] >= 95.0

def test_metrics_coalesce_and_flush(tmp_path):
    """inc/record_latency mark dirty; flush writes once; no .tmp debris."""
    metrics.set_data_dir(str(tmp_path))
    metrics.reset()
    path = tmp_path / "metrics.json"
    if path.exists():
        path.unlink()
    # mutations should NOT write immediately
    metrics.inc("search_total", 1)
    metrics.record_latency("search", 7.5)
    assert not path.exists(), "save should be deferred, not immediate"
    # flush writes
    metrics.flush()
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["counters"]["search_total"] == 1.0
    assert 7.5 in data["latencies"]["search"]
    # no leftover .tmp files
    assert not any(f.endswith(".tmp") for f in os.listdir(tmp_path))

def test_metrics_flush_if_idle_requires_dirty_idle_cache(tmp_path):
    metrics.set_data_dir(str(tmp_path))
    metrics.reset()
    path = tmp_path / "metrics.json"
    path.unlink()

    metrics.save()
    assert not path.exists()

    assert metrics.flush_if_idle(0) is False
    assert not path.exists()

    metrics.record_http_request("GET", "/health/live", 200)
    assert metrics.flush_if_idle(3600) is False
    assert not path.exists()

    assert metrics.flush_if_idle(0) is True
    assert path.exists()
    path.unlink()

    assert metrics.flush_if_idle(0) is False
    assert not path.exists()

def test_metrics_persistence(tmp_path):
    """Metrics should persist to disk and reload."""
    # Set up a temp data dir
    metrics.set_data_dir(str(tmp_path))
    # Reset to clean state
    metrics.reset()
    # Increment some counters
    metrics.inc("search_total", 5)
    metrics.inc("documents_indexed_total", 3)
    metrics.record_http_request("GET", "/health/live", 200)
    metrics.record_latency("search", 42.5)
    metrics.flush()
    # Verify file exists
    path = tmp_path / "metrics.json"
    assert path.exists()
    # Read and verify content
    data = json.loads(path.read_text())
    assert data["counters"]["search_total"] == 5.0
    assert data["counters"]["documents_indexed_total"] == 3.0
    http_series = data["labeled_counters"]["http_requests_total"]
    assert http_series == [
        {
            "labels": {
                "method": "GET",
                "path": "/health/live",
                "status_class": "2xx",
            },
            "value": 1.0,
        }
    ]
    assert 42.5 in data["latencies"]["search"]


def test_metrics_save_holds_lock_until_write_finishes(tmp_path, monkeypatch):
    metrics.set_data_dir(str(tmp_path))
    metrics.reset()
    metrics.inc("search_total", 1)

    writes: list[dict] = []
    first_write_started = threading.Event()
    allow_first_write = threading.Event()

    def fake_write(directory, path, data):
        writes.append(data)
        if len(writes) == 1:
            first_write_started.set()
            allow_first_write.wait(timeout=5)

    monkeypatch.setattr(metrics, "_write_metrics_file", fake_write)

    save_thread = threading.Thread(target=metrics.save)
    save_thread.start()
    assert first_write_started.wait(timeout=5)

    reset_thread = threading.Thread(target=metrics.reset)
    reset_thread.start()
    reset_thread.join(timeout=0.1)
    assert reset_thread.is_alive()

    allow_first_write.set()
    save_thread.join(timeout=5)
    reset_thread.join(timeout=5)

    assert not save_thread.is_alive()
    assert not reset_thread.is_alive()
    assert writes[0]["counters"]["search_total"] == 1.0
    assert writes[-1]["counters"]["search_total"] == 0.0
    data = json.loads((tmp_path / "metrics.json").read_text())
    assert data["counters"]["search_total"] == 0.0

def test_metrics_load_on_restart(tmp_path):
    """Metrics should be restored from disk on restart."""
    # Write a metrics file
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps({
        "counters": {"search_total": 100.0, "errors_total": 5.0},
        "labeled_counters": {
            "http_requests_total": [
                {
                    "labels": {
                        "method": "GET",
                        "path": "/health/live",
                        "status_class": "2xx",
                    },
                    "value": 4.0,
                }
            ]
        },
        "last_error": "test error",
        "latencies": {"search": [10.0, 20.0, 30.0]}
    }))
    # Load metrics from that dir
    metrics.set_data_dir(str(tmp_path))
    # Verify counters were loaded
    snap = metrics.snapshot()
    assert snap["search_total"] == 100.0
    assert snap["errors_total"] == 5.0
    assert snap["last_error"] == "test error"
    assert snap["search_latency_count"] == 3
    assert snap["http_requests_total"] == [
        {
            "labels": {
                "method": "GET",
                "path": "/health/live",
                "status_class": "2xx",
            },
            "value": 4.0,
        }
    ]


def test_build_app_disables_metrics_persistence_for_multiple_workers(
    monkeypatch,
    tmp_path,
):
    from pave.config import Config
    import pave.main as main_mod

    cfg = Config(path=tmp_path / "missing.yml")
    cfg.set("dev", True)
    cfg.set("auth.mode", "none")
    cfg.set("data_dir", str(tmp_path))
    cfg.set("server.workers", 2)
    cfg.set("embedder.warmup_default", False)

    calls: list[str] = []
    monkeypatch.setattr(
        main_mod,
        "metrics_set_data_dir",
        lambda path: calls.append(path),
    )

    app = main_mod.build_app(cfg)

    assert calls == []
    assert app.state.cfg.get("server.workers") == 2

def test_metrics_reset_api(client):
    """DELETE /admin/metrics should reset all metrics."""
    # First do some operations to have non-zero metrics
    client.post("/v1/collections/acme/rst", headers={})
    client.post("/v1/collections/acme/rst/documents",
                files={"file": ("r.txt", b"reset test", "text/plain")},
                data={"docid": "R1"})
    client.get("/v1/collections/acme/rst/search", params={"q": "reset", "k": 5})
    # Verify we have some metrics
    snap1 = client.get("/health/metrics").json()
    assert snap1["search_total"] >= 1
    # Reset metrics
    r = client.delete("/v1/admin/metrics")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # Verify metrics are reset
    snap2 = client.get("/health/metrics").json()
    assert snap2["search_total"] == 0.0
    assert snap2["documents_indexed_total"] == 0.0
