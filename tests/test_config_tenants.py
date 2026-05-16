# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from pathlib import Path

from pave.config import Config


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_tenants_sidecar_is_opt_in(monkeypatch, tmp_path):
    monkeypatch.delenv("PAVEDB_AUTH__TENANTS_FILE", raising=False)
    home = tmp_path / "home"
    sidecar = home / "pavedb" / "tenants.yml"
    sidecar.parent.mkdir(parents=True)
    _write(
        sidecar,
        "auth:\n"
        "  api_keys:\n"
        "    acme: sidecar-key\n"
        "tenants:\n"
        "  acme:\n"
        "    max_concurrent: 9\n",
    )
    monkeypatch.setenv("HOME", str(home))

    config_path = tmp_path / "config.yml"
    _write(
        config_path,
        "auth:\n"
        "  mode: static\n",
    )

    cfg = Config(path=config_path)

    assert cfg.get("auth.tenants_file") is None
    assert cfg.get("auth.api_keys") == {}
    assert cfg.get("tenants.acme.max_concurrent") is None


def test_dev_mode_skips_default_user_config(monkeypatch, tmp_path):
    home = tmp_path / "home"
    default_cfg = home / "pavedb" / "config.yml"
    default_cfg.parent.mkdir(parents=True)
    _write(
        default_cfg,
        "auth:\n"
        "  mode: static\n"
        "instance:\n"
        "  name: User config\n",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PAVEDB_CONFIG", raising=False)
    monkeypatch.setenv("PAVEDB_DEV", "1")

    cfg = Config()

    assert cfg.get("auth.mode") == "none"
    assert cfg.get("vector_store.type") == "faiss"
    assert cfg.get("instance.name") != "User config"


def test_explicit_config_path_still_wins_in_dev(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yml"
    _write(
        config_path,
        "auth:\n"
        "  mode: static\n"
        "instance:\n"
        "  name: Explicit config\n",
    )
    monkeypatch.setenv("PAVEDB_DEV", "1")
    monkeypatch.setenv("PAVEDB_CONFIG", str(config_path))

    cfg = Config()

    assert cfg.get("auth.mode") == "static"
    assert cfg.get("instance.name") == "Explicit config"


def test_env_tenants_file_selects_sidecar_over_config_value(monkeypatch, tmp_path):
    sidecar_a = tmp_path / "tenants-a.yml"
    sidecar_b = tmp_path / "tenants-b.yml"
    _write(
        sidecar_a,
        "auth:\n"
        "  api_keys:\n"
        "    acme: config-sidecar-key\n"
        "tenants:\n"
        "  acme:\n"
        "    max_concurrent: 1\n",
    )
    _write(
        sidecar_b,
        "auth:\n"
        "  api_keys:\n"
        "    acme: env-sidecar-key\n"
        "tenants:\n"
        "  acme:\n"
        "    max_concurrent: 7\n",
    )

    config_path = tmp_path / "config.yml"
    _write(
        config_path,
        f"auth:\n"
        f"  mode: static\n"
        f"  tenants_file: {sidecar_a}\n",
    )
    monkeypatch.setenv("PAVEDB_AUTH__TENANTS_FILE", str(sidecar_b))

    cfg = Config(path=config_path)

    assert cfg.get("auth.api_keys.acme") == "env-sidecar-key"
    assert cfg.get("tenants.acme.max_concurrent") == 7


def test_inline_tenant_values_override_sidecar(tmp_path):
    sidecar = tmp_path / "tenants.yml"
    _write(
        sidecar,
        "auth:\n"
        "  api_keys:\n"
        "    acme: sidecar-key\n"
        "tenants:\n"
        "  acme:\n"
        "    max_concurrent: 1\n",
    )

    config_path = tmp_path / "config.yml"
    _write(
        config_path,
        f"auth:\n"
        f"  mode: static\n"
        f"  tenants_file: {sidecar}\n"
        f"  api_keys:\n"
        f"    acme: inline-key\n"
        f"tenants:\n"
        f"  acme:\n"
        f"    max_concurrent: 2\n",
    )

    cfg = Config(path=config_path)

    assert cfg.get("auth.api_keys.acme") == "inline-key"
    assert cfg.get("tenants.acme.max_concurrent") == 2


def test_env_tenant_values_override_inline_and_sidecar(monkeypatch, tmp_path):
    sidecar = tmp_path / "tenants.yml"
    _write(
        sidecar,
        "auth:\n"
        "  api_keys:\n"
        "    acme: sidecar-key\n"
        "tenants:\n"
        "  acme:\n"
        "    max_concurrent: 1\n",
    )

    config_path = tmp_path / "config.yml"
    _write(
        config_path,
        f"auth:\n"
        f"  mode: static\n"
        f"  tenants_file: {sidecar}\n"
        f"  api_keys:\n"
        f"    acme: inline-key\n"
        f"tenants:\n"
        f"  acme:\n"
        f"    max_concurrent: 2\n",
    )
    monkeypatch.setenv("PAVEDB_AUTH__API_KEYS__acme", "env-key")
    monkeypatch.setenv("PAVEDB_TENANTS__acme__MAX_CONCURRENT", "3")

    cfg = Config(path=config_path)

    assert cfg.get("auth.api_keys.acme") == "env-key"
    assert cfg.get("tenants.acme.max_concurrent") == 3


def test_tenants_sidecar_only_overlays_tenant_keys(tmp_path):
    sidecar = tmp_path / "tenants.yml"
    _write(
        sidecar,
        "auth:\n"
        "  mode: none\n"
        "  api_keys:\n"
        "    acme: sidecar-key\n"
        "search:\n"
        "  max_concurrent: 1\n"
        "tenants:\n"
        "  acme:\n"
        "    max_concurrent: 4\n",
    )

    config_path = tmp_path / "config.yml"
    _write(
        config_path,
        f"auth:\n"
        f"  mode: static\n"
        f"  tenants_file: {sidecar}\n"
        f"search:\n"
        f"  max_concurrent: 42\n",
    )

    cfg = Config(path=config_path)

    assert cfg.get("auth.mode") == "static"
    assert cfg.get("search.max_concurrent") == 42
    assert cfg.get("auth.api_keys.acme") == "sidecar-key"
    assert cfg.get("tenants.acme.max_concurrent") == 4
