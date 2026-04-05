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
