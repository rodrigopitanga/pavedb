# (C) 2025 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from pave.config import Config


def test_overlay_get_set_snapshot(cfg):
    # baseline efetivo (sem depender de arquivo em dev)
    assert cfg.get("auth.mode") == "none"
    assert cfg.get("vector_store.type") == "faiss"

    # runtime overlay supersedes codebase and data
    cfg.set("auth.mode", "static")
    cfg.set("vector_store.type", "custom")
    assert cfg.get("auth.mode") == "static"
    assert cfg.get("vector_store.type") == "custom"

    snap = cfg.snapshot()
    assert snap["auth"]["mode"] == "static"
    assert snap["vector_store"]["type"] == "custom"
    cfg.set("auth.mode", "none")
    cfg.set("vector_store.type", "faiss")


def test_runtime_overlay_reflected_in_app(app, client):
    # muda runtime no cfg do app e checa que /health reflete esses valores
    app.state.cfg.set("auth.mode", "static")
    app.state.cfg.set("auth.global_key", "root")
    app.state.cfg.set("vector_store.type", "custom")

    r = client.get(
        "/health/metrics",
        headers={"Authorization": "Bearer root"},
    )
    assert r.status_code == 200
    j = r.json()
    assert j["auth"] == "static"
    assert j["vector_store"] == "custom"


def test_effective_defaults_when_missing(cfg, app, client):
    # se não houver config file, build_app aplica defaults efetivos
    # garante que nunca seja None nos endpoints
    r = client.get("/health/metrics")
    assert r.status_code == 200
    j = r.json()
    assert j["auth"] == "none"
    assert j["vector_store"] == "faiss"


def test_runtime_overlay_does_not_mutate_future_default_configs(tmp_path):
    cfg = Config(path=tmp_path / "missing.yml")
    cfg.set("vector_store.type", "custom")
    cfg.set("auth.api_keys", {"acme": "sekret"})

    fresh = Config(path=tmp_path / "still-missing.yml")

    assert fresh.get("vector_store.type") == "faiss"
    assert fresh.get("auth.api_keys") == {}


def test_list_valued_env_overrides_are_split(monkeypatch, tmp_path):
    monkeypatch.setenv("PAVEDB_LOG__DEBUG", "pave.api,pave.service")
    monkeypatch.setenv("PAVEDB_LOG__WATCH", '["pave.routes","pave.cli"]')

    cfg = Config(path=tmp_path / "missing.yml")

    assert cfg.get("log.debug") == ["pave.api", "pave.service"]
    assert cfg.get("log.watch") == ["pave.routes", "pave.cli"]


def test_replace_keeps_child_views_live(tmp_path):
    cfg = Config(data={"auth": {"mode": "none"}})
    auth_view = cfg.auth
    assert auth_view.get("mode") == "none"

    cfg.replace(data={"auth": {"mode": "static"}})

    assert auth_view.get("mode") == "static"
