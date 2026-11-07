# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from pathlib import Path

from fastapi.testclient import TestClient

from pave.config import get_cfg
from pave.main import build_app
from pave.ui import attach_ui


def _mk_collection(store, base: Path, tenant: str, collection: str):
    store._data_dir = str(base)
    store.create_collection(tenant, collection)


def test_admin_list_tenants_sorted(client, temp_data_dir):
    _mk_collection(client.app.state.store.impl, Path(temp_data_dir), "beta", "docs")
    _mk_collection(client.app.state.store.impl, Path(temp_data_dir), "alpha", "docs")
    r = client.get("/v1/admin/tenants")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["tenants"] == ["alpha", "beta"]
    assert data["count"] == 2


def test_admin_list_tenants_requires_admin(tmp_path):
    cfg = get_cfg()
    cfg.set("data_dir", str(tmp_path))
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", "sekret")

    app = build_app(cfg)
    try:
        attach_ui(app)
    except Exception:
        pass

    client = TestClient(app, base_url="http://localhost")
    app.state.store.create_collection("acme", "docs")

    r = client.get("/v1/admin/tenants")
    assert r.status_code == 401

    r = client.get("/v1/admin/tenants", headers={"Authorization": "Bearer sekret"})
    assert r.status_code == 200
    assert r.json()["tenants"] == ["acme"]


def test_admin_list_embedders_hides_credentials(client):
    cfg = get_cfg()
    cfg.set("embedder.instances.native.api_key", "secret-token")

    r = client.get("/v1/admin/embedders")

    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["tenant"] is None
    assert data["embedders"] == [
        {
            "key": "native",
            "vector_key": "sbert:fake:",
            "embedder_type": "sbert",
            "embed_model": "fake",
        }
    ]
    assert data["default"] == {
        "selector": "native",
        "vector_key": "sbert:fake:",
        "instance_keys": ["native"],
    }
    assert "secret-token" not in r.text
    assert "api_key" not in r.text


def test_tenant_list_embedders_uses_tenant_auth(tmp_path):
    cfg = get_cfg()
    cfg.set("data_dir", str(tmp_path))
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", "admin-key")
    cfg.set("auth.api_keys", {"acme": "acme-key", "beta": "beta-key"})

    app = build_app(cfg)
    try:
        attach_ui(app)
    except Exception:
        pass

    client = TestClient(app, base_url="http://localhost")

    r = client.get(
        "/v1/embedders/acme",
        headers={"Authorization": "Bearer acme-key"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["tenant"] == "acme"
    assert data["embedders"][0]["key"] == "native"

    r = client.get(
        "/v1/embedders/acme",
        headers={"Authorization": "Bearer beta-key"},
    )
    assert r.status_code == 403
