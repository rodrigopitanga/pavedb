# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest

import pave.main as main_mod
from pave.config import get_cfg, should_enable_ui
from pave.ui import attach_ui


@pytest.mark.parametrize(
    "dev, ui_enabled, expected",
    [
        (True, None, True),    # dev default: on
        (False, None, False),  # prod default (paveserv run + docker): off
        (True, False, False),  # explicit off wins in dev
        (False, True, True),   # explicit on wins in prod
    ],
)
def test_should_enable_ui_policy(dev, ui_enabled, expected):
    cfg = get_cfg()
    cfg.set("dev", dev)
    cfg.set("ui.enabled", ui_enabled)
    assert should_enable_ui(cfg) is expected


def _routes(app):
    return {getattr(r, "path", None) for r in app.routes}


def _build(temp_data_dir, ui_enabled):
    # Mirror the production gate in pave.main.__getattr__: the constructor
    # drops /docs, /redoc, /openapi.json and attach_ui is skipped when off.
    cfg = get_cfg()
    cfg.set("ui.enabled", ui_enabled)
    app = main_mod.build_app(cfg)
    if app.state.ui_enabled:
        attach_ui(app)
    return app


def test_ui_off_hides_all_openapi_surfaces(temp_data_dir):
    app = _build(temp_data_dir, ui_enabled=False)
    assert app.state.ui_enabled is False
    paths = _routes(app)
    for gone in ("/docs", "/redoc", "/openapi.json", "/ui", "/ui/search"):
        assert gone not in paths, gone


def test_ui_on_exposes_docs_and_bundle(temp_data_dir):
    app = _build(temp_data_dir, ui_enabled=True)
    assert app.state.ui_enabled is True
    paths = _routes(app)
    for present in ("/docs", "/openapi.json", "/ui", "/ui/search"):
        assert present in paths, present
