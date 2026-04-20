# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import time as _time
import pave.routes.search as search_routes


def test_search_failure_returns_code(client, app, monkeypatch):
    client.post("/v1/collections/acme/failsearch")

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(app.state.store, "search", boom, raising=True)
    r = client.post(
        "/v1/collections/acme/failsearch/search",
        json={"q": "x", "k": 1},
    )
    assert r.status_code == 500
    data = r.json()
    assert data["ok"] is False
    assert data["code"] == "search_failed"


def test_search_timeout_returns_503(client, app, monkeypatch):
    """Search that exceeds timeout_ms returns 503 search_timeout."""
    client.post("/v1/collections/acme/timeouttestcol")

    def slow(*args, **kwargs):
        _time.sleep(5)
        return []

    monkeypatch.setattr(app.state, "search_timeout_s", 0.05)
    monkeypatch.setattr(app.state.store, "search", slow, raising=True)

    r = client.post(
        "/v1/collections/acme/timeouttestcol/search",
        json={"q": "x", "k": 1},
    )
    assert r.status_code == 503
    data = r.json()
    assert data["ok"] is False
    assert data["code"] == "search_timeout"


def test_search_overloaded_returns_503(client, app, monkeypatch):
    """Counter at max returns 503 search_overloaded immediately."""
    client.post("/v1/collections/acme/overloadcol")

    monkeypatch.setattr(app.state, "max_searches", 1)
    monkeypatch.setattr(app.state, "active_searches", 1)

    r = client.post(
        "/v1/collections/acme/overloadcol/search",
        json={"q": "x", "k": 1},
    )
    assert r.status_code == 503
    data = r.json()
    assert data["ok"] is False
    assert data["code"] == "search_overloaded"


def test_search_result_not_found_maps_to_404(client, monkeypatch):
    client.post("/v1/collections/acme/nfsearch")

    def fake_search(*args, **kwargs):
        return {
            "ok": False,
            "code": "query_not_found",
            "error": "query not found",
            "error_type": "not_found",
        }

    monkeypatch.setattr(
        search_routes,
        "svc_search",
        fake_search,
        raising=True,
    )

    r = client.post(
        "/v1/collections/acme/nfsearch/search",
        json={"q": "x", "k": 1},
    )
    assert r.status_code == 404
    data = r.json()
    assert data["ok"] is False
    assert data["code"] == "query_not_found"


def test_search_result_error_maps_to_500(client, monkeypatch):
    client.post("/v1/collections/acme/errsearch")

    def fake_search(*args, **kwargs):
        return {
            "ok": False,
            "code": "search_failed",
            "error": "boom",
        }

    monkeypatch.setattr(
        search_routes,
        "svc_search",
        fake_search,
        raising=True,
    )

    r = client.post(
        "/v1/collections/acme/errsearch/search",
        json={"q": "x", "k": 1},
    )
    assert r.status_code == 500
    data = r.json()
    assert data["ok"] is False
    assert data["code"] == "search_failed"
