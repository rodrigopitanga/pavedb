# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import threading
import time

import httpx
import pytest

import pave.routes.search as search_routes


def test_search_failure_returns_code(client, app, monkeypatch):
    client.post("/v1/collections/acme/failsearch")

    def boom(*args, **kwargs):
        raise OSError(36, "File name too long", "/srv/pavedb/data/t_acme/c_x")

    monkeypatch.setattr(app.state.store, "search", boom, raising=True)
    r = client.post(
        "/v1/collections/acme/failsearch/search",
        json={"q": "x", "k": 1},
    )
    assert r.status_code == 500
    data = r.json()
    assert data["ok"] is False
    assert data["code"] == "search_failed"
    # Unclassified exception text stays in the server log (B59).
    assert "/srv/pavedb" not in r.text


def test_search_encode_failure_returns_503(client, app, monkeypatch):
    client.post("/v1/collections/acme/encode-failure-search")
    embedder = app.state.store.impl._collection_embedders[
        ("acme", "encode-failure-search")
    ]

    def fail_encode(_texts):
        raise RuntimeError("upstream=http://10.0.0.7:8000 route=prod-embed")

    # An offline embedder cannot serve either surface. The query path uses
    # encode_queries (the worker pool routes it separately from encode), so
    # patching only encode no longer reaches the query encode.
    monkeypatch.setattr(embedder, "encode", fail_encode)
    monkeypatch.setattr(embedder, "encode_queries", fail_encode, raising=False)
    response = client.post(
        "/v1/collections/acme/encode-failure-search/search",
        json={"q": "hello", "k": 1},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "embedder_unavailable"
    # Upstream embedder detail is for the operator only (B43).
    assert "10.0.0.7" not in response.text


def test_search_timeout_returns_503(client, app, monkeypatch):
    """Search that exceeds timeout_ms returns 503 search_timeout."""
    client.post("/v1/collections/acme/timeouttestcol")

    release = threading.Event()
    started = threading.Event()
    drained = threading.Event()

    def slow(*args, **kwargs):
        started.set()
        try:
            release.wait(timeout=5)
            return []
        finally:
            drained.set()

    monkeypatch.setattr(app.state, "max_searches", 1)
    monkeypatch.setattr(app.state, "search_timeout_s", 0.05)
    monkeypatch.setattr(app.state.store, "search", slow, raising=True)

    try:
        r = client.post(
            "/v1/collections/acme/timeouttestcol/search",
            json={"q": "x", "k": 1},
        )
        assert r.status_code == 503
        data = r.json()
        assert data["ok"] is False
        assert data["code"] == "search_timeout"
        assert started.wait(timeout=1.0)
        assert not drained.is_set()
        assert app.state.active_searches == 1

        capped = client.post(
            "/v1/collections/acme/timeouttestcol/search",
            json={"q": "x", "k": 1},
        )
        assert capped.status_code == 503
        assert capped.json()["code"] == "search_overloaded"
    finally:
        release.set()

    assert drained.wait(timeout=1.0)
    deadline = time.monotonic() + 1.0
    while app.state.active_searches != 0 and time.monotonic() < deadline:
        time.sleep(0.001)
    assert app.state.active_searches == 0


def test_cancelled_search_keeps_slot(client, app, monkeypatch):
    client.post("/v1/collections/acme/canceltestcol")

    release = threading.Event()
    started = threading.Event()
    drained = threading.Event()

    def slow(*args, **kwargs):
        started.set()
        try:
            release.wait(timeout=5)
            return []
        finally:
            drained.set()

    monkeypatch.setattr(app.state, "max_searches", 1)
    monkeypatch.setattr(app.state.store, "search", slow, raising=True)

    async def cancel_request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as async_client:
            request = asyncio.create_task(
                async_client.post(
                    "/v1/collections/acme/canceltestcol/search",
                    json={"q": "x", "k": 1},
                )
            )
            deadline = time.monotonic() + 1.0
            while not started.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.001)
            assert started.is_set()
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request

    try:
        asyncio.run(cancel_request())
        assert not drained.is_set()
        assert app.state.active_searches == 1

        capped = client.post(
            "/v1/collections/acme/canceltestcol/search",
            json={"q": "x", "k": 1},
        )
        assert capped.status_code == 503
        assert capped.json()["code"] == "search_overloaded"
    finally:
        release.set()

    assert drained.wait(timeout=1.0)
    deadline = time.monotonic() + 1.0
    while app.state.active_searches != 0 and time.monotonic() < deadline:
        time.sleep(0.001)
    assert app.state.active_searches == 0


def test_search_unlimited_uses_default_executor(client, app, monkeypatch):
    client.post("/v1/collections/acme/unlimitedcol")
    monkeypatch.setattr(app.state, "max_searches", 0)
    monkeypatch.setattr(app.state, "search_executor", None)

    r = client.post(
        "/v1/collections/acme/unlimitedcol/search",
        json={"q": "x", "k": 1},
    )

    assert r.status_code == 200


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


def test_search_k_above_cap_rejected(client):
    """An unbounded k lets one request pull a whole collection."""
    client.post("/v1/collections/acme/kcapcol")

    r = client.post(
        "/v1/collections/acme/kcapcol/search",
        json={"q": "x", "k": 1001},
    )
    assert r.status_code == 422

    r = client.get("/v1/collections/acme/kcapcol/search?q=x&k=1001")
    assert r.status_code == 422

    # The shared-scope route is the one every tenant on a shared instance
    # reaches, so it needs the bound pinned too.
    r = client.get("/v1/search?q=x&k=1001")
    assert r.status_code == 422

    r = client.post(
        "/v1/collections/acme/kcapcol/search",
        json={"q": "x", "k": 1000},
    )
    assert r.status_code == 200


def test_replay_clamps_a_k_logged_before_the_bound(client, app, cfg):
    """A row logged pre-upgrade must not stay replayable at its old size."""
    import uuid

    from pave.schemas import MAX_SEARCH_K

    client.post("/v1/collections/acme/kreplay")
    client.post(
        "/v1/collections/acme/kreplay/documents",
        json={"text": "hello world", "docid": "d1"},
    )

    # Forge the pre-bound row the upgrade inherits.
    qid = str(uuid.uuid4())
    app.state.store.log_query(
        query_id=qid,
        tenant="acme",
        collection="kreplay",
        actor="tenant:acme",
        query_text="hello",
        k=99999,
        filters=None,
        result_ids=[],
        result_count=0,
        latency_ms=1.0,
    )

    store = app.state.store
    store.calls.clear()
    r = client.post(f"/v1/collections/acme/kreplay/queries/{qid}/replay")
    assert r.status_code == 200
    searches = [c for c in store.calls if c[0] == "search"]
    assert searches, "replay must still run a search"
    assert all(c[4] <= MAX_SEARCH_K for c in searches), searches
