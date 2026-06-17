# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .config.render import render_config_example

DEFAULT_HOME = "~/pavedb"


@dataclass(frozen=True)
class RuntimePaths:
    home: str | None
    config: str | None
    tenants: str | None
    data_dir: str | None


def _expand(path: str | None) -> str | None:
    if not path:
        return None
    return str(Path(path).expanduser())


def resolve_runtime_paths(
    *,
    home: str | None = None,
    config: str | None = None,
    tenants: str | None = None,
    data_dir: str | None = None,
) -> RuntimePaths:
    home_path = Path(home).expanduser() if home else None
    return RuntimePaths(
        home=str(home_path) if home_path else None,
        config=_expand(config) or (str(home_path / "config.yml") if home_path else None),
        tenants=_expand(tenants) or (str(home_path / "tenants.yml") if home_path else None),
        data_dir=_expand(data_dir) or (str(home_path / "data") if home_path else None),
    )


def apply_runtime_env(
    *,
    home: str | None = None,
    config: str | None = None,
    tenants: str | None = None,
    data_dir: str | None = None,
) -> RuntimePaths:
    paths = resolve_runtime_paths(
        home=home,
        config=config,
        tenants=tenants,
        data_dir=data_dir,
    )
    if paths.config:
        os.environ["PAVEDB_CONFIG"] = paths.config
    if paths.tenants:
        os.environ["PAVEDB_AUTH__TENANTS_FILE"] = paths.tenants
    if paths.data_dir:
        os.environ["PAVEDB_DATA_DIR"] = paths.data_dir
    return paths


def load_asset_text(name: str) -> str:
    return resources.files("pave.assets").joinpath(name).read_text(encoding="utf-8")


def _yaml_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def render_config_template(*, data_dir: str, tenants_file: str) -> str:
    text = render_config_example()
    text = re.sub(
        r"^data_dir: .*$",
        f"data_dir: {_yaml_quote(data_dir)}",
        text,
        flags=re.MULTILINE,
    )
    text = re.sub(
        r"^  # tenants_file: .*$",
        f"  tenants_file: {_yaml_quote(tenants_file)}",
        text,
        flags=re.MULTILINE,
    )
    return text
