# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pave.main import VERSION
from scripts.openapi_snapshot import OPENAPI_SNAPSHOT, render_openapi_json


def test_openapi_snapshot_is_current():
    assert OPENAPI_SNAPSHOT.read_text(encoding="utf-8") == render_openapi_json()


def test_openapi_snapshot_uses_product_version():
    assert f'"version": "{VERSION}"' in OPENAPI_SNAPSHOT.read_text(
        encoding="utf-8"
    )
