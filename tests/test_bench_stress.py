# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "benchmarks"),
)

import stress  # type: ignore[import-not-found]


def test_move_operation_covers_admin_and_tenant_routes():
    assert set(stress.op_move_collection.__bench_covers__) == {
        ("POST", "/v1/admin/collections/{tenant}/{name}/move"),
        ("POST", "/v1/collections/{tenant}/{name}/move"),
    }


def test_error_rate_violation_returns_message():
    violation = stress._error_rate_violation(
        100,
        2.5,
        1.0,
    )

    assert violation == "ERROR RATE VIOLATION: 2.5% > 1.0%"


def test_error_rate_violation_disabled_returns_none():
    assert stress._error_rate_violation(
        100,
        2.5,
        0,
    ) is None


def test_main_exits_1_on_error_rate_failure(monkeypatch):
    async def fake_run_stress(*args, **kwargs):
        return None

    monkeypatch.setattr(
        stress,
        "run_stress",
        fake_run_stress,
        raising=True,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["stress.py", "--max-error-pct", "1"],
    )

    with pytest.raises(SystemExit) as exc:
        stress.main()

    assert exc.value.code == 1
