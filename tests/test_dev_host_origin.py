# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from fastapi.testclient import TestClient

from pave.config import get_cfg
from pave.main import build_app


def test_unauthenticated_dev_rejects_rebound_host_and_foreign_origin(client):
    path = "/health/live"
    assert client.get(path).status_code == 200

    for headers in (
        {"Host": "attacker.example"},
        {"Host": "attacker.example:8086"},
        {"Origin": "http://attacker.example"},
        {"Origin": "null"},
        {"Origin": "http://localhost:9999"},
    ):
        response = client.get(path, headers=headers)
        assert response.status_code == 403
        assert response.json()["code"] == "invalid_origin"

    response = client.get(path, headers={"Origin": "http://localhost"})
    assert response.status_code == 200

    response = client.get(
        path,
        headers=[("Host", "localhost"), ("Host", "attacker.example")],
    )
    assert response.status_code == 403


def test_authenticated_app_accepts_other_hosts(temp_data_dir):
    cfg = get_cfg()
    cfg.set("data_dir", str(temp_data_dir))
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", "test-key")
    app = build_app(cfg)
    with TestClient(app) as client:
        response = client.get(
            "/health/live",
            headers={"Host": "api.example", "Origin": "https://console.example"},
        )
    assert response.status_code == 200
