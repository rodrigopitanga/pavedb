# (C) 2025 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from pave.main import VERSION

def test_health_endpoints(client):
    r = client.get("/health/live")
    assert r.status_code == 200 and r.json()["ok"] is True

    r2 = client.get("/health")
    assert r2.status_code == 200 and "version" in r2.json()

    r3 = client.get("/health/ready")
    assert r3.status_code in (200, 503)
    assert "vector_store" in r3.json()

def test_health_defaults(client):
    r = client.get("/health")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] in (True, False)
    assert j["version"] == VERSION

def test_health_ready_contains_effective_fields(client):
    r = client.get("/health/ready")
    assert r.status_code in (200, 503)
    j = r.json()
    assert "data_dir" not in j
    assert j.get("vector_store") == "faiss"
    assert "writable" in j and isinstance(j["writable"], bool)
    assert "vector_backend_init" in j and isinstance(j["vector_backend_init"], bool)


def test_metrics_require_admin_when_static_auth(client, cfg):
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", "root")
    cfg.set("auth.api_keys", {"acme": "sekret"})

    for path in ("/health/metrics", "/metrics"):
        missing = client.get(path)
        assert missing.status_code == 401
        assert missing.json()["code"] == "auth_invalid"

        tenant = client.get(path, headers={"Authorization": "Bearer sekret"})
        assert tenant.status_code == 403
        assert tenant.json()["code"] == "admin_required"

        admin = client.get(path, headers={"Authorization": "Bearer root"})
        assert admin.status_code == 200
