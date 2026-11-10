# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Concurrency slot accounting: per-tenant share and detached work.

Every wait here is bounded so a leaked or deadlocked slot fails the test
instead of hanging the suite.
"""

import asyncio
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import pave.routes.documents as doc_routes


def _wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.001)
    return predicate()


def test_ingest_share_caps_one_tenant_below_instance_cap(
    client, app, monkeypatch
):
    """One tenant must not be able to hold every shared ingest slot."""
    # Off by default: a share is pure throughput loss on a single-tenant
    # instance, so it is opt-in and a shared deployment turns it on.
    assert app.state.max_ingests_per_tenant == -1

    client.post("/v1/collections/acme/sharecol")
    client.post("/v1/collections/other/sharecol")

    monkeypatch.setattr(app.state, "max_ingests", 4)
    monkeypatch.setattr(app.state, "max_ingests_per_tenant", 2)
    monkeypatch.setitem(app.state.tenant_active_ingests, "acme", 2)

    r = client.post(
        "/v1/collections/acme/sharecol/documents",
        json={"text": "hello world", "docid": "d1"},
    )
    assert r.status_code == 503
    data = r.json()
    assert data["code"] == "ingest_overloaded"
    assert "tenant-scoped ingest cap" in data["error"]

    # The batch endpoint shares the same gate.
    rb = client.post(
        "/v1/collections/acme/sharecol/documents:batch",
        json={"documents": [{"text": "hello world", "docid": "d2"}]},
    )
    assert rb.status_code == 503
    assert rb.json()["code"] == "ingest_overloaded"

    # Instance pool still has room, so another tenant is unaffected.
    assert app.state.active_ingests == 0
    other = client.post(
        "/v1/collections/other/sharecol/documents",
        json={"text": "hello world", "docid": "d1"},
    )
    assert other.status_code == 201, other.text


def test_search_share_caps_one_tenant_below_instance_cap(app, monkeypatch):
    """One tenant must not be able to hold every shared search slot."""
    # Off by default, like the ingest share: a share is pure throughput
    # loss on a single-tenant instance, so a shared deployment turns it on.
    assert app.state.max_searches_per_tenant == -1

    cfg = app.state.cfg
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", None)
    cfg.set("auth.api_keys", {"acme": "acmetoken", "other": "othertoken"})
    acme = {"Authorization": "Bearer acmetoken"}
    other = {"Authorization": "Bearer othertoken"}

    c = TestClient(app, base_url="http://localhost")
    c.post("/v1/collections/acme/sharesearch", headers=acme)
    c.post("/v1/collections/other/sharesearch", headers=other)

    monkeypatch.setattr(app.state, "max_searches", 4)
    monkeypatch.setattr(app.state, "max_searches_per_tenant", 2)
    monkeypatch.setitem(app.state.tenant_active_searches, "acme", 2)

    r = c.post(
        "/v1/collections/acme/sharesearch/search",
        json={"q": "x", "k": 1},
        headers=acme,
    )
    assert r.status_code == 503
    data = r.json()
    assert data["code"] == "search_overloaded"
    # Distinguishable from the instance cap, which returns the same code.
    assert "tenant-scoped search share" in data["error"]

    # Instance pool still has room, so another tenant is unaffected.
    assert app.state.active_searches == 0
    ok = c.post(
        "/v1/collections/other/sharesearch/search",
        json={"q": "x", "k": 1},
        headers=other,
    )
    assert ok.status_code == 200, ok.text


def test_timed_out_search_releases_share_once(app, monkeypatch):
    """The share follows the detached thread and is released exactly once."""
    cfg = app.state.cfg
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", None)
    cfg.set("auth.api_keys", {"acme": "acmetoken"})
    headers = {"Authorization": "Bearer acmetoken"}

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

    c = TestClient(app, base_url="http://localhost")
    c.post("/v1/collections/acme/sharetimeout", headers=headers)

    monkeypatch.setattr(app.state, "max_searches_per_tenant", 3)
    monkeypatch.setattr(app.state, "search_timeout_s", 0.05)
    monkeypatch.setattr(app.state.store, "search", slow, raising=True)
    # A search of the same tenant already in flight: the timed-out request
    # must give back its own slot and only its own.
    monkeypatch.setitem(app.state.tenant_active_searches, "acme", 1)

    try:
        r = c.post(
            "/v1/collections/acme/sharetimeout/search",
            json={"q": "x", "k": 1},
            headers=headers,
        )
        assert r.status_code == 503
        assert r.json()["code"] == "search_timeout"
        assert started.wait(timeout=1.0)
        assert not drained.is_set()
        # Still on a pool thread, so still charged to the tenant's share.
        assert app.state.tenant_active_searches["acme"] == 2
    finally:
        release.set()

    assert drained.wait(timeout=1.0)
    assert _wait_until(
        lambda: app.state.tenant_active_searches["acme"] == 1
    )
    # Give a double release a chance to show up as the pre-seeded slot
    # disappearing.
    time.sleep(0.05)
    assert app.state.tenant_active_searches["acme"] == 1
    assert _wait_until(lambda: app.state.active_searches == 0)


def test_timed_out_search_keeps_tenant_slot(app, monkeypatch):
    """A detached search thread keeps counting against its tenant."""
    cfg = app.state.cfg
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", None)
    cfg.set("auth.api_keys", {"acme": "acmetoken"})
    monkeypatch.setitem(app.state.tenant_limits, "acme", 1)

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

    headers = {"Authorization": "Bearer acmetoken"}
    c = TestClient(app, base_url="http://localhost")
    c.post("/v1/collections/acme/tenantslotcol", headers=headers)

    monkeypatch.setattr(app.state, "search_timeout_s", 0.05)
    monkeypatch.setattr(app.state.store, "search", slow, raising=True)

    try:
        r = c.post(
            "/v1/collections/acme/tenantslotcol/search",
            json={"q": "x", "k": 1},
            headers=headers,
        )
        assert r.status_code == 503
        assert r.json()["code"] == "search_timeout"
        assert started.wait(timeout=1.0)
        assert not drained.is_set()
        # The thread still holds an instance slot, so the tenant must
        # still be charged for it.
        assert app.state.tenant_active["acme"] == 1

        capped = c.post(
            "/v1/collections/acme/tenantslotcol/search",
            json={"q": "x", "k": 1},
            headers=headers,
        )
        assert capped.status_code == 429
        assert capped.json()["code"] == "tenant_rate_limited"
    finally:
        release.set()

    assert drained.wait(timeout=1.0)
    assert _wait_until(lambda: app.state.tenant_active["acme"] == 0)


def test_cancelled_ingest_keeps_ingest_slot(client, app, monkeypatch):
    """A disconnecting client must not free a slot the pool still holds."""
    client.post("/v1/collections/acme/cancelingest")

    release = threading.Event()
    started = threading.Event()
    drained = threading.Event()

    def slow_ingest(*args, **kwargs):
        started.set()
        try:
            release.wait(timeout=5)
            return {"ok": True, "docid": "d1", "chunks": 1}
        finally:
            drained.set()

    monkeypatch.setattr(app.state, "max_ingests", 1)
    monkeypatch.setattr(doc_routes, "svc_ingest_text", slow_ingest)

    async def cancel_request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost"
        ) as async_client:
            request = asyncio.create_task(
                async_client.post(
                    "/v1/collections/acme/cancelingest/documents",
                    json={"text": "hello world", "docid": "d1"},
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
        assert app.state.active_ingests == 1

        capped = client.post(
            "/v1/collections/acme/cancelingest/documents",
            json={"text": "hello world", "docid": "d2"},
        )
        assert capped.status_code == 503
        assert capped.json()["code"] == "ingest_overloaded"
    finally:
        release.set()

    assert drained.wait(timeout=1.0)
    assert _wait_until(lambda: app.state.active_ingests == 0)
    assert _wait_until(
        lambda: app.state.tenant_active_ingests.get("acme", 0) == 0
    )
