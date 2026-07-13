# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "benchmarks"),
)

import chaos  # type: ignore[import-not-found]


class FakeClient:
    def __init__(self):
        self.calls = []

    def post(self, path, json):
        self.calls.append((path, json))
        return SimpleNamespace(status_code=200)


def test_move_collection_uses_admin_route():
    client = FakeClient()

    resp = chaos._move_collection(client, "old-name", "new-name")

    assert resp.status_code == 200
    assert client.calls == [
        (
            "/v1/admin/collections/chaos/old-name/move",
            {"new_name": "new-name"},
        )
    ]
