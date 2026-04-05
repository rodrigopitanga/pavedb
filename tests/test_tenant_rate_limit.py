# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from fastapi.testclient import TestClient


def test_tenant_rate_limited_returns_429(app, monkeypatch):
    """Tenant at its concurrent limit gets 429 tenant_rate_limited."""
    cfg = app.state.cfg
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", None)
    cfg.set("auth.api_keys", {"acme": "acmetoken"})

    monkeypatch.setitem(app.state.tenant_limits, "acme", 1)
    monkeypatch.setitem(app.state.tenant_active, "acme", 1)

    c = TestClient(app)
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

    c = TestClient(app)
    c.post("/v1/collections/acme/adminbypass")
    r = c.post(
        "/v1/collections/acme/adminbypass/search",
        json={"q": "x", "k": 1},
    )
    # Admin bypasses the rate limit; must not be 429.
    assert r.status_code != 429


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

    c = TestClient(app)
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
    """default_limit=0 means unlimited; requests must not get 429."""
    cfg = app.state.cfg
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", None)
    cfg.set("auth.api_keys", {"acme": "acmetoken"})

    # No per-tenant limit; global default = 0 (unlimited).
    monkeypatch.setattr(app.state, "tenant_default_limit", 0)
    # Even with a high active count, unlimited means no 429.
    monkeypatch.setitem(app.state.tenant_active, "acme", 9999)

    c = TestClient(app)
    r = c.post(
        "/v1/collections/acme/unlimited_rl/search",
        json={"q": "x", "k": 1},
        headers={"Authorization": "Bearer acmetoken"},
    )
    # Rate limit must NOT fire; any other status code is acceptable.
    assert r.status_code != 429
