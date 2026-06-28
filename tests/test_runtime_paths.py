# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
from pathlib import Path

from pave.runtime_paths import (
    apply_runtime_env,
    load_asset_text,
    render_config_template,
    resolve_runtime_paths,
)


def test_resolve_runtime_paths_derives_from_home(tmp_path):
    home = tmp_path / "instance"

    paths = resolve_runtime_paths(home=str(home))

    assert paths.home == str(home)
    assert paths.config == str(home / "config.yml")
    assert paths.tenants == str(home / "tenants.yml")
    assert paths.data_dir == str(home / "data")


def test_resolve_runtime_paths_anchors_to_explicit_config(monkeypatch, tmp_path):
    for name in (
        "PAVEDB_HOME",
        "PAVEDB_CONFIG",
        "PAVEDB_AUTH__TENANTS_FILE",
        "PAVEDB_DATA_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / "etc" / "config.yml"

    paths = resolve_runtime_paths(config=str(config))

    assert paths.home == str(config.parent)
    assert paths.config == str(config)
    assert paths.tenants == str(config.parent / "tenants.yml")
    assert paths.data_dir == str(config.parent / "data")


def test_explicit_runtime_paths_override_home_defaults(tmp_path):
    home = tmp_path / "instance"
    config = tmp_path / "etc" / "config.yml"
    tenants = tmp_path / "var" / "tenants.yml"
    data_dir = tmp_path / "srv" / "data"

    paths = resolve_runtime_paths(
        home=str(home),
        config=str(config),
        tenants=str(tenants),
        data_dir=str(data_dir),
    )

    assert paths.config == str(config)
    assert paths.tenants == str(tenants)
    assert paths.data_dir == str(data_dir)


def test_render_config_template_injects_runtime_paths(tmp_path):
    data_dir = str(tmp_path / "data")
    tenants = str(tmp_path / "tenants.yml")

    rendered = render_config_template(data_dir=data_dir, tenants_file=tenants)

    assert f"data_dir: '{data_dir}'" in rendered
    assert f"  tenants_file: '{tenants}'" in rendered


def test_apply_runtime_env_sets_pavedb_vars(monkeypatch, tmp_path):
    for name in (
        "PAVEDB_HOME",
        "PAVEDB_CONFIG",
        "PAVEDB_AUTH__TENANTS_FILE",
        "PAVEDB_DATA_DIR",
    ):
        monkeypatch.delenv(name, raising=False)

    home = tmp_path / "instance"
    paths = apply_runtime_env(home=str(home))

    assert os.environ["PAVEDB_HOME"] == str(home)
    assert "PAVEDB_CONFIG" not in os.environ
    assert "PAVEDB_AUTH__TENANTS_FILE" not in os.environ
    assert "PAVEDB_DATA_DIR" not in os.environ
    assert paths.config == str(home / "config.yml")


def test_packaged_templates_match_repo_examples():
    assert load_asset_text("tenants.yml.example") == Path("tenants.yml.example").read_text(
        encoding="utf-8"
    )
