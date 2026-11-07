# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from pave.main import VERSION


@pytest.mark.parametrize("failure", [None, "filesystem", "catalog"])
def test_anonymous_health_omits_runtime_details(client, cfg, app, monkeypatch, failure):
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", "root")

    def fail(*args, **kwargs):
        raise OSError("probe unavailable")

    if failure == "filesystem":
        monkeypatch.setattr("pave.routes.health.tempfile.mkstemp", fail)
    elif failure == "catalog":
        monkeypatch.setattr(app.state.store, "catalog_probe", fail)

    ready = failure is None
    expected = {
        "/health/live": (200, {"ok": True, "status": "live"}),
        "/health": (200, {"ok": ready, "status": "ready" if ready else "degraded"}),
        "/health/ready": (
            200 if ready else 503,
            {
                "ok": ready,
                "writable": failure != "filesystem",
                "vector_backend_init": failure != "catalog",
            },
        ),
    }
    for path, (status_code, payload) in expected.items():
        response = client.get(path, headers={"X-Request-ID": "health-probe"})
        assert response.status_code == status_code
        body = response.json()
        assert body.pop("request_id") == "health-probe"
        assert isinstance(body.pop("latency_ms"), (int, float))
        assert body == payload


def test_health_ready_writable_under_concurrent_probes(client, cfg):
    def probe(_):
        r = client.get("/health/ready")
        return r.status_code, r.json()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(probe, range(80)))

    not_writable = [j for _, j in results if not j["writable"]]
    assert not not_writable, not_writable[:3]
    assert all(code == 200 for code, _ in results)
    leftovers = [
        n for n in os.listdir(cfg.data_dir) if n.startswith(".writetest")
    ]
    assert leftovers == []


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
        if path == "/health/metrics":
            assert admin.json()["version"] == VERSION
            assert admin.json()["vector_store"] == "faiss"
        else:
            assert f'version="{VERSION}"' in admin.text
            assert 'vector_store="faiss"' in admin.text
