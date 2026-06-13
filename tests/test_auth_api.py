# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later


def test_auth_missing_token_returns_code(client, cfg):
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", None)
    cfg.set("auth.api_keys", {"acme": "sekret"})

    r = client.post("/v1/collections/acme/invoices")
    assert r.status_code == 401
    data = r.json()
    assert data["ok"] is False
    assert data["code"] == "auth_invalid"
    assert "authorization" in data["error"].lower()


def test_auth_forbidden_returns_code(client, cfg):
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", None)
    cfg.set("auth.api_keys", {"acme": "sekret"})

    r = client.post(
        "/v1/collections/acme/invoices",
        headers={"Authorization": "Bearer nope"},
    )
    assert r.status_code == 403
    data = r.json()
    assert data["ok"] is False
    assert data["code"] == "auth_forbidden"


def test_admin_move_collection_requires_admin_token(client, cfg):
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", "root")
    cfg.set("auth.api_keys", {"acme": "sekret"})

    client.post(
        "/v1/collections/acme/invoices",
        headers={"Authorization": "Bearer root"},
    )
    r = client.post(
        "/v1/admin/collections/acme/invoices/move",
        json={"new_name": "bills"},
        headers={"Authorization": "Bearer sekret"},
    )

    assert r.status_code == 403
    assert r.json()["code"] == "admin_required"
