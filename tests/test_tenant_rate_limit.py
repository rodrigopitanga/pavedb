# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from pave.config import get_cfg
from pave.main import build_app


def test_build_app_loads_default_and_tenant_rate_limits():
    cfg = get_cfg()
    cfg.set("tenants.default_max_rpm", 100)
    cfg.set("tenants.default_max_rph", 1000)
    cfg.set("tenants.acme.max_rpm", 10)

    configured = build_app(cfg)

    assert configured.state.tenant_default_rate_limits == {60: 100, 3600: 1000}
    assert configured.state.tenant_rate_limits["acme"] == {60: 10, 3600: 1000}


def test_zero_rate_window_denies_without_charging_other_window(app):
    store = app.state.store.impl
    denied = store.charge_rate_limit("zero-budget", "global", {60: 0, 3600: 2})
    assert not denied.allowed
    assert denied.limit == 0
    assert denied.retry_after == 0
    allowed = store.charge_rate_limit("zero-budget", "global", {3600: 2})
    assert allowed.allowed
    assert allowed.remaining == 1


def test_zero_request_budget_has_no_retry_after(app, monkeypatch):
    cfg = app.state.cfg
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", None)
    cfg.set("auth.api_keys", {"zero": "zero-token"})
    monkeypatch.setitem(
        app.state.tenant_rate_limits, "zero", {60: 0, 3600: -1}
    )
    response = TestClient(app).get(
        "/v1/collections/zero",
        headers={"Authorization": "Bearer zero-token"},
    )
    assert response.status_code == 429
    assert response.headers["X-RateLimit-Remaining"] == "0"
    assert "Retry-After" not in response.headers


def test_zero_tenant_concurrency_refuses_request(app, monkeypatch):
    cfg = app.state.cfg
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", None)
    cfg.set("auth.api_keys", {"zero": "zero-token"})
    monkeypatch.setitem(app.state.tenant_limits, "zero", 0)
    response = TestClient(app).get(
        "/v1/collections/zero",
        headers={"Authorization": "Bearer zero-token"},
    )
    assert response.status_code == 429
    assert response.json()["code"] == "tenant_rate_limited"


def test_tenant_rate_limited_returns_429(app, monkeypatch):
    """Tenant at its concurrent limit gets 429 tenant_rate_limited."""
    cfg = app.state.cfg
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", None)
    cfg.set("auth.api_keys", {"acme": "acmetoken"})

    monkeypatch.setitem(app.state.tenant_limits, "acme", 1)
    monkeypatch.setitem(app.state.tenant_active, "acme", 1)

    c = TestClient(app, base_url="http://localhost")
    r = c.post(
        "/v1/collections/acme/rlcol/search",
        json={"q": "x", "k": 1},
        headers={"Authorization": "Bearer acmetoken"},
    )
    assert r.status_code == 429
    data = r.json()
    assert data["ok"] is False
    assert data["code"] == "tenant_rate_limited"
    assert r.headers.get("X-RateLimit-Remaining") == "0"
    assert r.headers.get("Retry-After") == "1"


def test_admin_bypasses_tenant_limit(app, monkeypatch):
    """Admin requests (auth.mode=none → is_admin=True) bypass tenant limits."""
    # Default conftest auth.mode=none → all requests are admin.
    monkeypatch.setitem(app.state.tenant_limits, "acme", 1)
    monkeypatch.setitem(app.state.tenant_active, "acme", 1)

    c = TestClient(app, base_url="http://localhost")
    c.post("/v1/collections/acme/adminbypass")
    r = c.post(
        "/v1/collections/acme/adminbypass/search",
        json={"q": "x", "k": 1},
    )
    # Admin bypasses the rate limit; must not be 429.
    assert r.status_code != 429


def test_shared_scope_search_honours_tenant_limit(app, monkeypatch):
    """/v1/search enters the per-tenant gate like the scoped search routes."""
    cfg = app.state.cfg
    cfg.set("common_enabled", True)
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", None)
    cfg.set("auth.api_keys", {"acme": "acmetoken"})

    monkeypatch.setitem(app.state.tenant_limits, "acme", 1)
    monkeypatch.setitem(app.state.tenant_active, "acme", 1)

    c = TestClient(app, base_url="http://localhost")
    headers = {"Authorization": "Bearer acmetoken"}
    r = c.post("/v1/search", json={"q": "x", "k": 1}, headers=headers)
    assert r.status_code == 429
    assert r.json()["code"] == "tenant_rate_limited"
    assert r.headers.get("Retry-After") == "1"

    g = c.get("/v1/search", params={"q": "x", "k": 1}, headers=headers)
    assert g.status_code == 429
    assert g.json()["code"] == "tenant_rate_limited"


def test_admin_bypasses_shared_scope_tenant_limit(app, monkeypatch):
    """The shared-scope gate must keep tenant_rate_limit's admin bypass.

    Under auth.mode=none the admin context still carries the default access
    tenant, so a gate keyed on ctx.tenant alone would cap the admin identity.
    """
    cfg = app.state.cfg
    cfg.set("common_enabled", True)
    saturated = (
        str(cfg.get("auth.default_access_tenant")),
        str(cfg.get("common_tenant")),
    )
    for tenant in saturated:
        monkeypatch.setitem(app.state.tenant_limits, tenant, 1)
        monkeypatch.setitem(app.state.tenant_active, tenant, 1)

    c = TestClient(app, base_url="http://localhost")
    r = c.post("/v1/search", json={"q": "x", "k": 1})
    assert r.status_code != 429
    g = c.get("/v1/search", params={"q": "x", "k": 1})
    assert g.status_code != 429


def test_unconfigured_tenant_uses_default(app, monkeypatch):
    """Tenant absent from tenant_limits uses tenant_default_limit; 429 when hit."""
    cfg = app.state.cfg
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", None)
    cfg.set("auth.api_keys", {"acme": "acmetoken"})

    # No per-tenant limit entry; default = 1.
    monkeypatch.setattr(app.state, "tenant_default_limit", 1)
    # Simulate one request already active for this tenant.
    monkeypatch.setitem(app.state.tenant_active, "acme", 1)

    c = TestClient(app, base_url="http://localhost")
    r = c.post(
        "/v1/collections/acme/rlcol2/search",
        json={"q": "x", "k": 1},
        headers={"Authorization": "Bearer acmetoken"},
    )
    assert r.status_code == 429
    data = r.json()
    assert data["ok"] is False
    assert data["code"] == "tenant_rate_limited"


def test_unconfigured_tenant_no_default_unlimited(app, monkeypatch):
    """default_limit=-1 means unlimited; requests must not get 429."""
    cfg = app.state.cfg
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", None)
    cfg.set("auth.api_keys", {"acme": "acmetoken"})

    # No per-tenant limit; global default = -1 (unlimited).
    monkeypatch.setattr(app.state, "tenant_default_limit", -1)
    # Even with a high active count, unlimited means no 429.
    monkeypatch.setitem(app.state.tenant_active, "acme", 9999)

    c = TestClient(app, base_url="http://localhost")
    r = c.post(
        "/v1/collections/acme/unlimited-rl/search",
        json={"q": "x", "k": 1},
        headers={"Authorization": "Bearer acmetoken"},
    )
    # Rate limit must NOT fire; any other status code is acceptable.
    assert r.status_code != 429


def test_moving_window_request_limit_returns_429(app, monkeypatch):
    cfg = app.state.cfg
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", None)
    cfg.set("auth.api_keys", {"acme": "acmetoken"})
    monkeypatch.setitem(app.state.tenant_rate_limits, "acme", {60: 2, 3600: -1})
    monkeypatch.setattr(app.state, "tenant_rate_clock", lambda: 120.0)

    client = TestClient(app, base_url="http://localhost")
    headers = {"Authorization": "Bearer acmetoken"}
    first = client.get("/v1/collections/acme", headers=headers)
    second = client.get("/v1/collections/acme", headers=headers)
    denied = client.get("/v1/collections/acme", headers=headers)

    assert first.status_code != 429
    assert first.headers["X-RateLimit-Remaining"] == "1"
    assert second.status_code != 429
    assert second.headers["X-RateLimit-Remaining"] == "0"
    assert denied.status_code == 429
    assert denied.json()["code"] == "tenant_rate_limited"
    assert denied.headers["X-RateLimit-Limit"] == "2"
    assert denied.headers["X-RateLimit-Window"] == "60"
    assert denied.headers["Retry-After"] == "60"


def test_moving_window_limits_are_isolated_by_tenant(app, monkeypatch):
    cfg = app.state.cfg
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", None)
    cfg.set(
        "auth.api_keys",
        {"acme": "acmetoken", "other": "othertoken"},
    )
    monkeypatch.setattr(app.state, "tenant_default_rate_limits", {60: 1, 3600: -1})
    monkeypatch.setattr(app.state, "tenant_rate_clock", lambda: 120.0)

    client = TestClient(app, base_url="http://localhost")
    acme = {"Authorization": "Bearer acmetoken"}
    other = {"Authorization": "Bearer othertoken"}
    assert client.get("/v1/collections/acme", headers=acme).status_code != 429
    assert client.get("/v1/collections/acme", headers=acme).status_code == 429
    assert client.get("/v1/collections/other", headers=other).status_code != 429


def test_admin_bypasses_moving_window_request_limit(app, monkeypatch):
    monkeypatch.setattr(app.state, "tenant_default_rate_limits", {60: 1, 3600: 1})
    monkeypatch.setattr(app.state, "tenant_rate_clock", lambda: 120.0)

    client = TestClient(app, base_url="http://localhost")
    first = client.get("/v1/collections/public")
    second = client.get("/v1/collections/public")

    assert first.status_code != 429
    assert second.status_code != 429


def test_concurrency_refusal_does_not_charge_moving_window(app, monkeypatch):
    """A 429 on the concurrent cap spends nothing from the rpm/rph budget."""
    cfg = app.state.cfg
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", None)
    cfg.set("auth.api_keys", {"acme": "acmetoken"})
    monkeypatch.setitem(app.state.tenant_rate_limits, "acme", {60: 2, 3600: -1})
    monkeypatch.setattr(app.state, "tenant_rate_clock", lambda: 120.0)
    monkeypatch.setitem(app.state.tenant_limits, "acme", 1)
    monkeypatch.setitem(app.state.tenant_active, "acme", 1)

    client = TestClient(app, base_url="http://localhost")
    headers = {"Authorization": "Bearer acmetoken"}
    refused = client.get("/v1/collections/acme", headers=headers)
    assert refused.status_code == 429
    assert refused.json()["code"] == "tenant_rate_limited"

    monkeypatch.setitem(app.state.tenant_active, "acme", 0)
    first = client.get("/v1/collections/acme", headers=headers)
    second = client.get("/v1/collections/acme", headers=headers)
    assert first.status_code != 429
    assert first.headers["X-RateLimit-Remaining"] == "1"
    assert second.status_code != 429
    assert second.headers["X-RateLimit-Remaining"] == "0"


def test_rate_charge_error_releases_concurrency_slot(app, monkeypatch):
    cfg = app.state.cfg
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", None)
    cfg.set("auth.api_keys", {"acme": "acmetoken"})
    monkeypatch.setitem(app.state.tenant_limits, "acme", 1)
    monkeypatch.setitem(app.state.tenant_rate_limits, "acme", {60: 4})

    charge = app.state.tenant_rate_limiter

    def fail_charge(*args, **kwargs):
        raise RuntimeError("rate charge failed")

    client = TestClient(app, base_url="http://localhost")
    headers = {"Authorization": "Bearer acmetoken"}
    path = "/v1/collections/acme"
    app.state.tenant_rate_limiter = fail_charge
    try:
        with pytest.raises(RuntimeError, match="rate charge failed"):
            client.get(path, headers=headers)
    finally:
        # Explicit restore, not monkeypatch.context(): under this suite's
        # Starlette State (plain dict-backed __setattr__), a nested
        # monkeypatch context occasionally failed to restore the attribute
        # before the next assertion, leaving app.state without
        # tenant_rate_limiter and failing the next request with an
        # unrelated-looking AttributeError.
        app.state.tenant_rate_limiter = charge

    assert app.state.tenant_active["acme"] == 0

    for remaining in (3, 2):
        response = client.get(path, headers=headers)
        assert response.status_code == 200
        assert response.headers["X-RateLimit-Remaining"] == str(remaining)
        assert app.state.tenant_active["acme"] == 0

    started = threading.Event()
    release = threading.Event()

    def blocked_charge(*args, **kwargs):
        started.set()
        release.wait(timeout=5)
        return charge(*args, **kwargs)

    async def cancel_during_charge():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost"
        ) as async_client:
            request = asyncio.create_task(async_client.get(path, headers=headers))
            try:
                # A loaded CI runner can take well over a second to schedule
                # the threadpool worker that sets ``started``; budget 10s
                # instead of 1s so this does not flake under contention.
                for _ in range(10_000):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.001)
                assert started.is_set()
                assert app.state.tenant_active["acme"] == 1
                request.cancel()
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await request

    app.state.tenant_rate_limiter = blocked_charge
    try:
        asyncio.run(cancel_during_charge())
    finally:
        app.state.tenant_rate_limiter = charge

    assert app.state.tenant_active["acme"] == 0
    recovered = client.get(path, headers=headers)
    assert recovered.status_code == 200
    assert app.state.tenant_active["acme"] == 0
