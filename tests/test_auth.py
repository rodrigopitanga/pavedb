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


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_enforce_policy_accepts_development_strings(value):
    from pave.auth import enforce_policy, resolve_bind
    from pave.config import Config

    cfg = Config(data={"dev": value, "auth": {"mode": "none"},
                       "server": {"host": "0.0.0.0"}})
    enforce_policy(cfg)
    assert resolve_bind(cfg)[0] == "127.0.0.1"


@pytest.mark.parametrize("value", ["no", "off", "disabled"])
def test_enforce_policy_refuses_disabled_dev_env_string(value, monkeypatch, tmp_path):
    """Built from the env, not from `data=`: that is where the string comes
    from. The overlay only coerces "true"/"false" and numbers, so PAVEDB_DEV=no
    stays the string "no" — truthy to Python, off to the operator."""
    from pave.auth import enforce_policy
    from pave.config import Config

    # Redirected so no installed ~/pavedb/config.yml joins this overlay.
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    monkeypatch.setenv("PAVEDB_DEV", value)
    cfg = Config()
    cfg.set("auth.mode", "none")

    with pytest.raises(RuntimeError, match="auth.mode=none not allowed"):
        enforce_policy(cfg)


@pytest.mark.parametrize("value", ["yes", "on"])
def test_enforce_policy_accepts_enabled_dev_env_string(value, monkeypatch, tmp_path):
    """The same uncoerced strings the other way: dev must still recognize them."""
    from pave.auth import enforce_policy, resolve_bind
    from pave.config import Config

    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    monkeypatch.setenv("PAVEDB_DEV", value)
    cfg = Config()
    cfg.set("auth.mode", "none")
    cfg.set("server.host", "0.0.0.0")

    enforce_policy(cfg)
    assert resolve_bind(cfg)[0] == "127.0.0.1"


def test_enforce_policy_constrains_multiple_workers_to_dev(caplog):
    from pave.auth import enforce_policy
    from pave.config.config import Config

    production = Config(data={
        "dev": False,
        "auth": {"mode": "static", "global_key": "test-key"},
        "server": {"workers": 2},
    })
    with pytest.raises(RuntimeError, match="NOT FOR PRODUCTION AS OF NOW"):
        enforce_policy(production)

    development = Config(data={
        "dev": True,
        "auth": {"mode": "none"},
        "server": {"workers": 2},
    })
    enforce_policy(development)

    assert "may corrupt data" in caplog.text
    assert "disposable development data" in caplog.text


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

def test_enforce_policy_unknown_mode_raises():
    from pave.auth import enforce_policy
    from pave.config.config import Config
    cfg = Config(data={"dev": True, "auth": {"mode": "key"}})
    with pytest.raises(RuntimeError, match="unknown auth.mode"):
        enforce_policy(cfg)

def test_auth_env_key_not_coerced(monkeypatch):
    monkeypatch.setenv("PAVEDB_AUTH__GLOBAL_KEY", "0123456789")
    from pave.config.config import Config
    cfg = Config()
    assert cfg.get("auth.global_key") == "0123456789", "digit-like key must not be int-coerced"

def test_enforce_policy_rejects_non_canonical_tenant_names():
    """Case variants would be two principals sharing one data directory."""
    from pave.auth import enforce_policy
    from pave.config.config import Config

    cfg = Config(data={
        "auth": {"mode": "static", "api_keys": {"Acme": "k1", "acme": "k2"}},
    })
    with pytest.raises(RuntimeError, match="invalid tenant name"):
        enforce_policy(cfg)


def test_enforce_policy_accepts_canonical_tenant_names():
    from pave.auth import enforce_policy
    from pave.config.config import Config

    cfg = Config(data={
        "auth": {"mode": "static", "api_keys": {"acme": "k1", "beta-2": "k2"}},
    })
    enforce_policy(cfg)


def test_tenant_and_collection_name_rules_are_the_same():
    """One identifier rule. If these drift, tenants and collections disagree."""
    from pave.auth import _TENANT_NAME_RE
    from pave.service import _COLLECTION_NAME_RE

    assert _TENANT_NAME_RE.pattern == _COLLECTION_NAME_RE.pattern


def test_enforce_policy_rejects_invalid_default_access_tenant():
    """In auth.mode=none this value becomes a real namespace on disk."""
    from pave.auth import enforce_policy
    from pave.config.config import Config

    cfg = Config(data={
        "auth": {"mode": "none", "default_access_tenant": "Public"},
        "dev": True,
    })
    with pytest.raises(RuntimeError, match="default_access_tenant"):
        enforce_policy(cfg)


def test_tenant_name_error_names_the_data_directory_move():
    """A rename without moving t_<name> silently orphans that tenant's data."""
    from pave.auth import enforce_policy
    from pave.config.config import Config

    cfg = Config(data={
        "auth": {"mode": "static", "api_keys": {"Acme": "k1"}},
    })
    with pytest.raises(RuntimeError) as exc:
        enforce_policy(cfg)
    assert "t_Acme" in str(exc.value)
    assert "orphans" in str(exc.value)


def test_non_string_api_key_is_rejected(monkeypatch):
    """`acme: yes` in YAML is a bool; str() made "True" a working token."""
    import pave.auth as auth_mod

    from pave.auth import enforce_policy
    from pave.config.config import Config

    cfg = Config(data={
        "auth": {"mode": "static", "api_keys": {"acme": True}},
    })
    with pytest.raises(RuntimeError, match="invalid bearer token"):
        enforce_policy(cfg)

    monkeypatch.setattr(auth_mod, "get_cfg", lambda: cfg, raising=True)
    client = TestClient(build_app())
    r = client.post(
        "/collections/acme/x", headers={"Authorization": "Bearer True"}
    )
    assert r.status_code == 403


def test_enforce_policy_rejects_duplicate_api_keys():
    """One token for two tenants binds its holder to whoever comes first."""
    from pave.auth import enforce_policy
    from pave.config.config import Config

    cfg = Config(data={
        "auth": {"mode": "static", "api_keys": {"acme": "k1", "beta": "k1"}},
    })
    with pytest.raises(RuntimeError, match="duplicate bearer token") as exc:
        enforce_policy(cfg)
    assert "acme" in str(exc.value) and "beta" in str(exc.value)


def test_enforce_policy_rejects_invalid_common_tenant():
    """common_tenant reaches store.search() as a raw t_<x> path segment."""
    from pave.auth import enforce_policy
    from pave.config.config import Config

    cfg = Config(data={
        "auth": {"mode": "static", "api_keys": {"acme": "k1"}},
        "common_enabled": True,
        "common_tenant": "Global",
    })
    with pytest.raises(RuntimeError, match="common_tenant"):
        enforce_policy(cfg)


def test_non_string_global_key_is_rejected(monkeypatch):
    """The admin credential goes through the same coercion as tenant keys."""
    import pave.auth as auth_mod

    from pave.auth import enforce_policy
    from pave.config.config import Config

    cfg = Config(data={"auth": {"mode": "static", "global_key": True}})
    with pytest.raises(RuntimeError, match="invalid bearer token"):
        enforce_policy(cfg)

    monkeypatch.setattr(auth_mod, "get_cfg", lambda: cfg, raising=True)
    client = TestClient(build_app())
    r = client.post(
        "/collections/acme/x", headers={"Authorization": "Bearer True"}
    )
    assert r.status_code == 403


def test_tenant_key_equal_to_global_key_is_rejected():
    """Its holder would authenticate as admin, not as that tenant."""
    from pave.auth import enforce_policy
    from pave.config.config import Config

    cfg = Config(data={
        "auth": {
            "mode": "static",
            "global_key": "shared",
            "api_keys": {"acme": "shared"},
        },
    })
    with pytest.raises(RuntimeError, match="duplicate bearer token") as exc:
        enforce_policy(cfg)
    assert "auth.global_key" in str(exc.value)


def test_padded_api_key_is_rejected():
    """auth_ctx strips the presented token, so a padded value never matches."""
    from pave.auth import enforce_policy
    from pave.config.config import Config

    cfg = Config(data={
        "auth": {"mode": "static", "api_keys": {"acme": " k1 "}},
    })
    with pytest.raises(RuntimeError, match="whitespace"):
        enforce_policy(cfg)


@pytest.mark.parametrize("tenant", [None, ""])
def test_actor_of_never_labels_an_unidentified_caller_admin(tenant):
    from pave.auth import AuthContext, actor_of

    assert actor_of(AuthContext(tenant=tenant, is_admin=False)) == "unknown"
    assert actor_of(AuthContext(tenant=None, is_admin=True)) == "admin"
    assert actor_of(AuthContext(tenant="acme", is_admin=False)) == "tenant:acme"


def test_static_auth_rejects_unknown_and_non_ascii_tokens():
    client = TestClient(build_app())
    ok = client.post(
        "/collections/acme/x", headers={"Authorization": "Bearer sekret"}
    )
    assert ok.status_code == 200
    for token in ("sekreu", "sekret-longer", "sékret"):
        r = client.post(
            "/collections/acme/x",
            headers={"Authorization": f"Bearer {token}".encode("latin-1")},
        )
        assert r.status_code == 403, token
