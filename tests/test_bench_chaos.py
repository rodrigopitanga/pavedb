# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def test_chaos_accepts_only_the_ingest_overload_503():
    expected = chaos.httpx.Response(503, json={"code": "ingest_overloaded"})
    other = chaos.httpx.Response(503, json={"code": "internal_error"})

    assert chaos._is_ingest_overload(expected)
    assert not chaos._is_ingest_overload(other)


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


# --- P1-52 recipe regression ---
# chaos-nightly passes CHAOS_DURATION/CHAOS_CONCUR to `make chaos` via the
# environment; the recipe must forward them to benchmarks/chaos.py, not fall
# back to the Makefile's 120/24 defaults. `make -n` renders the recipe without
# running it. The Makefile requires GNU Make >= 4 (gmake on macOS).
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _gnu_make() -> str | None:
    for cand in ("make", "gmake"):
        exe = shutil.which(cand)
        if exe is None:
            continue
        lines = subprocess.run(
            [exe, "--version"], capture_output=True, text=True
        ).stdout.splitlines()
        if lines and lines[0].startswith("GNU Make"):
            try:
                if int(lines[0].split()[2].split(".")[0]) >= 4:
                    return exe
            except (IndexError, ValueError):
                continue
    return None


def _render_chaos_run(**chaos_env: str) -> str:
    make = _gnu_make()
    if make is None:
        pytest.skip("GNU Make >= 4 not available")
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("CHAOS_DURATION", "CHAOS_CONCUR")
    }
    env.update(chaos_env)
    return subprocess.run(
        [make, "-C", str(_REPO_ROOT), "-n", "chaos-run"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    ).stdout


def test_chaos_run_forwards_schedule_duration_and_concurrency():
    out = _render_chaos_run(CHAOS_DURATION="300", CHAOS_CONCUR="48")
    assert "--duration 300" in out
    assert "--concurrency 48" in out


def test_chaos_run_uses_makefile_defaults_when_unset():
    out = _render_chaos_run()
    assert "--duration 120" in out
    assert "--concurrency 24" in out


def test_large_volume_ingest_gets_its_own_timeout():
    seen = {}

    class Client:
        timeout = 30

        def post(self, path, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(status_code=201)

    chaos._ingest_raw(Client(), "c", b"x")
    assert seen["timeout"] == 30
    chaos._ingest_raw(Client(), "c", b"x", timeout=chaos.LARGE_VOLUME_TIMEOUT_S)
    assert seen["timeout"] == chaos.LARGE_VOLUME_TIMEOUT_S
