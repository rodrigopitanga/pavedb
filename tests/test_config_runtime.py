# (C) 2025 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest

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


def test_api_keys_env_json_map_and_nested_override(monkeypatch, tmp_path):
    monkeypatch.setenv("PAVEDB_AUTH__API_KEYS__ACME", "rotated-key")
    monkeypatch.setenv(
        "PAVEDB_AUTH__API_KEYS", '{"acme":"old-key","beta":"beta-key"}'
    )

    cfg = Config(path=tmp_path / "missing.yml")

    assert cfg.get("auth.api_keys") == {
        "acme": "rotated-key",
        "beta": "beta-key",
    }


@pytest.mark.parametrize("value", ["secret", "[]", "null", "{invalid}"])
def test_api_keys_env_requires_json_object(monkeypatch, tmp_path, value):
    monkeypatch.setenv("PAVEDB_AUTH__API_KEYS", value)

    with pytest.raises(ValueError, match="PAVEDB_AUTH__API_KEYS must be a JSON object"):
        Config(path=tmp_path / "missing.yml")


def test_insane_chunking_bounds_are_rejected(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "chunking:\n"
        "  default_size: 1000\n"
        "  default_overlap: 900\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="default_overlap"):
        Config(path=config_path)

    monkeypatch.setenv("PAVEDB_CHUNKING__MIN_SIZE", "9000")
    with pytest.raises(ValueError, match="min_size"):
        Config(path=tmp_path / "missing.yml")


def test_replace_keeps_child_views_live(tmp_path):
    cfg = Config(data={"auth": {"mode": "none"}})
    auth_view = cfg.auth
    assert auth_view.get("mode") == "none"

    cfg.replace(data={"auth": {"mode": "static"}})

    assert auth_view.get("mode") == "static"


@pytest.mark.parametrize(
    "path",
    [
        "ingest.max_file_size_mb",
        "ingest.max_batch_size_mb",
        "server.max_request_body_mb",
    ],
)
@pytest.mark.parametrize(
    "value", ["invalid", True, -1, float("nan"), float("inf"), 1e-9, 1e308]
)
def test_invalid_size_caps_are_rejected_on_load_and_set(path, value):
    section, name = path.split(".")
    with pytest.raises(ValueError, match=path):
        Config(data={section: {name: value}})

    cfg = Config(data={})
    before = cfg.get(path)
    with pytest.raises(ValueError, match=path):
        cfg.set(path, value)
    assert cfg.get(path) == before


def test_invalid_size_cap_env_and_reload_leave_previous_config(monkeypatch, tmp_path):
    cfg = Config(data={})
    monkeypatch.setenv("PAVEDB_SERVER__MAX_REQUEST_BODY_MB", "invalid")
    with pytest.raises(ValueError, match="server.max_request_body_mb"):
        cfg.replace(path=tmp_path / "missing.yml")
    assert cfg.get("server.max_request_body_mb") == 16


@pytest.mark.parametrize(
    "path,value,limit_path",
    [
        ("ingest", {"max_file_size_mb": -1}, "ingest.max_file_size_mb"),
        (
            "ingest",
            {"max_file_size_mb": 10, "max_batch_size_mb": -1},
            "ingest.max_batch_size_mb",
        ),
        ("server", {"max_request_body_mb": -1}, "server.max_request_body_mb"),
    ],
)
def test_invalid_size_caps_are_rejected_in_section_updates(
    path, value, limit_path
):
    cfg = Config(data={})
    before = cfg.get(limit_path)

    with pytest.raises(ValueError, match=limit_path):
        cfg.set(path, value)

    assert cfg.get(limit_path) == before


@pytest.mark.parametrize(
    "section,name",
    [
        ("ingest", "max_file_size_mb"),
        ("ingest", "max_batch_size_mb"),
        ("server", "max_request_body_mb"),
    ],
)
def test_invalid_size_caps_are_rejected_in_child_views(section, name):
    cfg = Config(data={})
    path = f"{section}.{name}"
    before = cfg.get(path)

    with pytest.raises(ValueError, match=path):
        getattr(cfg, section).set(name, -1)

    assert cfg.get(path) == before
