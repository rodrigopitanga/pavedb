# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from pathlib import Path

from fastapi.testclient import TestClient

from pave.config import get_cfg
from pave.main import build_app
from pave.ui import attach_ui


def _mk_collection(store, base: Path, tenant: str, collection: str) -> None:
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

    client = TestClient(app)
    app.state.store.create_collection("acme", "docs")

    r = client.get("/v1/admin/tenants")
    assert r.status_code == 401

    r = client.get("/v1/admin/tenants", headers={"Authorization": "Bearer sekret"})
    assert r.status_code == 200
    assert r.json()["tenants"] == ["acme"]
