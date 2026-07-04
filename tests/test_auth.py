# (C) 2025 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient

def build_app():
    from pave.auth import authorize_tenant
    app = FastAPI()

    @app.post("/collections/{tenant}/{name}")
    def create_collection(tenant: str, name: str, ctx = Depends(authorize_tenant)):
        # If we reached here, auth passed for this tenant
        return {"ok": True, "tenant": tenant, "name": name}

    return app

@pytest.fixture(autouse=True)
def patch_cfg(monkeypatch, tmp_path):
    import pave.auth as auth_mod
    class DummyCFG:
        data_dir = str(tmp_path / "data")
        def get(self, key, default=None):
            # auth.* keys read by your code
            if key == "auth.mode":
                return "static"
            if key == "auth.global_key":
                return None  # default no global; test both modes below
            if key == "auth.api_keys":
                return {"acme": "sekret"}  # per-tenant mode
            return default
    monkeypatch.setattr(auth_mod, "get_cfg", lambda: DummyCFG(), raising=True)
    yield

def test_static_auth_per_tenant_allows_matching_token_only():
    app = build_app()
    client = TestClient(app)

    # 200: correct per-tenant token on the same tenant path
    r = client.post("/collections/acme/invoices", headers={"Authorization": "Bearer sekret"})
    assert r.status_code == 200
    assert r.json()["tenant"] == "acme"

    # 403: tenant mismatch (token is for 'acme' but URL tenant is 'other')
    r = client.post("/collections/other/invoices", headers={"Authorization": "Bearer sekret"})
    assert r.status_code == 403

def test_static_auth_global_key(monkeypatch):
    # Reconfigure to use global key instead of per-tenant keys
    import pave.auth as auth_mod
    class CFGGlobal:
        data_dir = "/tmp"
        def get(self, key, default=None):
            if key == "auth.mode":
                return "static"
            if key == "auth.global_key":
                return "topsekret"  # global key active
            if key == "auth.api_keys":
                return None
            return default
    monkeypatch.setattr(auth_mod, "get_cfg", lambda: CFGGlobal(), raising=True)

    app = build_app()
    client = TestClient(app)

    # Any tenant should accept the same global token
    for tenant in ("acme", "other"):
        r = client.post(f"/collections/{tenant}/invoices", headers={"Authorization": "Bearer topsekret"})
        assert r.status_code == 200
        assert r.json()["tenant"] == tenant

def test_enforce_policy_dev_loopback_coercion():
    from pave.auth import enforce_policy, resolve_bind
    from pave.config.config import Config
    cfg = Config(data={"dev": True, "auth": {"mode": "none"}, "server": {"host": "0.0.0.0", "port": 8086}})
    enforce_policy(cfg)
    assert resolve_bind(cfg) == ("127.0.0.1", 8086)

def test_bearer_scheme_case_insensitive(monkeypatch):
    import pave.auth as auth_mod
    class CFGGlobal:
        data_dir = "/tmp"
        def get(self, key, default=None):
            if key == "auth.mode": return "static"
            if key == "auth.global_key": return "sekret"
            return default
    monkeypatch.setattr(auth_mod, "get_cfg", lambda: CFGGlobal(), raising=True)
    app = build_app()
    client = TestClient(app)
    for scheme in ("bearer", "BEARER", "Bearer"):
        r = client.post("/collections/acme/x", headers={"Authorization": f"{scheme} sekret"})
        assert r.status_code == 200, f"scheme {scheme!r} rejected"
