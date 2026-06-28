# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .config.config import CONFIG_ENV, DEFAULT_HOME, HOME_ENV
from .config.render import render_config_example

TENANTS_ENV = "PAVEDB_AUTH__TENANTS_FILE"
DATA_DIR_ENV = "PAVEDB_DATA_DIR"


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


def _home_path(*, home: str | None, config: str | None) -> Path:
    if home:
        return Path(home).expanduser()
    if config:
        return Path(config).expanduser().parent
    env_home = _expand(os.environ.get(HOME_ENV))
    if env_home:
        return Path(env_home)
    env_config = _expand(os.environ.get(CONFIG_ENV))
    if env_config:
        return Path(env_config).parent
    return Path(DEFAULT_HOME).expanduser()


def resolve_runtime_paths(
    *,
    home: str | None = None,
    config: str | None = None,
    tenants: str | None = None,
    data_dir: str | None = None,
) -> RuntimePaths:
    home_path = _home_path(home=home, config=config)
    config_path = _expand(config) or _expand(os.environ.get(CONFIG_ENV))
    tenants_path = _expand(tenants) or _expand(os.environ.get(TENANTS_ENV))
    data_path = _expand(data_dir) or _expand(os.environ.get(DATA_DIR_ENV))
    return RuntimePaths(
        home=str(home_path),
        config=config_path or str(home_path / "config.yml"),
        tenants=tenants_path or str(home_path / "tenants.yml"),
        data_dir=data_path or str(home_path / "data"),
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
    if home or config:
        os.environ[HOME_ENV] = paths.home or ""
    if config:
        os.environ[CONFIG_ENV] = paths.config or ""
    if tenants:
        os.environ[TENANTS_ENV] = paths.tenants or ""
    if data_dir:
        os.environ[DATA_DIR_ENV] = paths.data_dir or ""
    return paths


def load_asset_text(name: str) -> str:
    return resources.files("pave.assets").joinpath(name).read_text(encoding="utf-8")


def _yaml_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def render_config_template(
    *,
    data_dir: str,
    tenants_file: str,
    ops_log: str | None = None,
    access_log: str | None = None,
) -> str:
    text = render_config_example()
    text = re.sub(
        r"^#? ?data_dir: .*$",
        f"data_dir: {_yaml_quote(data_dir)}",
        text,
        flags=re.MULTILINE,
    )
    text = re.sub(
        r"^  #? ?tenants_file: .*$",
        f"  tenants_file: {_yaml_quote(tenants_file)}",
        text,
        flags=re.MULTILINE,
    )
    if ops_log:
        text = re.sub(
            r"^  #? ?ops_log: .*$",
            f"  # ops_log: {_yaml_quote(ops_log)}",
            text,
            flags=re.MULTILINE,
        )
    if access_log:
        text = re.sub(
            r"^  #? ?access_log: .*$",
            f"  # access_log: {_yaml_quote(access_log)}",
            text,
            flags=re.MULTILINE,
        )
    return text
