# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from .config import (
    CONFIG_FIELDS,
    SEARCH_MODES,
    Config,
    ConfigField,
    cfg_bool,
    get_cfg,
    reload_cfg,
    should_enable_ui,
    should_warm_default_embedder,
    _DEFAULTS,
    _config_defaults,
    _load_config_fields,
)

# Dev stream (stderr) logger lives in pave.log — re-exported here for
# callers. Imported after the names above are bound on the package so that
# pave.log._init_logger()'s `from pave.config import get_cfg` resolves.
from pave.log import get_logger, LOG

__all__ = [
    "CONFIG_FIELDS",
    "Config",
    "ConfigField",
    "LOG",
    "SEARCH_MODES",
    "cfg_bool",
    "get_cfg",
    "get_logger",
    "reload_cfg",
    "should_enable_ui",
    "should_warm_default_embedder",
]
