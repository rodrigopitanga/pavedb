# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import sqlite3
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


def test_catalog_disk_violations_report_missing_and_orphaned_collections(tmp_path):
    with sqlite3.connect(tmp_path / "catalog.db") as conn:
        conn.execute("CREATE TABLE collections (tenant TEXT, name TEXT)")
        conn.execute("INSERT INTO collections VALUES ('acme', 'catalog-only')")
        conn.execute("INSERT INTO collections VALUES ('acme', 'ok')")

    ok = tmp_path / "t_acme" / "c_ok"
    ok.mkdir(parents=True)
    (ok / "meta.db").touch()
    orphan = tmp_path / "t_acme" / "c_orphan"
    orphan.mkdir()
    (orphan / "meta.db").touch()

    assert chaos.catalog_disk_violations(tmp_path) == [
        "catalog row missing disk collection acme/catalog-only",
        "disk collection missing catalog row acme/orphan",
    ]
