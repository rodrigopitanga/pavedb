# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "benchmarks"),
)

import search_latency  # type: ignore[import-not-found]


def test_search_sends_hybrid_mode():
    seen: dict[str, object] = {}

    async def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"matches": []})

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://pavedb.test",
        ) as client:
            return await search_latency.search(
                client,
                "acme",
                "docs",
                "captain nemo",
                mode="hybrid",
            )

    _latency, ok, _detail, _hits = asyncio.run(run())

    assert ok is True
    assert seen["mode"] == "hybrid"


def test_latency_slo_violation_returns_message():
    violation = search_latency._latency_slo_violation(
        [10.0, 100.0],
        50.0,
    )

    assert violation is not None
    assert violation.startswith("SLO VIOLATION: p99=")
    assert violation.endswith(" > 50.0ms")


def test_latency_slo_disabled_returns_none():
    assert search_latency._latency_slo_violation(
        [10.0, 100.0],
        0,
    ) is None


def test_main_exits_1_on_slo_failure(monkeypatch):
    async def fake_run_benchmark(*args, **kwargs):
        return None

    monkeypatch.setattr(
        search_latency,
        "run_benchmark",
        fake_run_benchmark,
        raising=True,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["search_latency.py", "--slo-p99-ms", "10"],
    )

    with pytest.raises(SystemExit) as exc:
        search_latency.main()

    assert exc.value.code == 1


# job 17708580396 (bench, 245464bc, !90) failed on filtering=mixed at
# p99=3088.3ms against the 2000ms gate: min 32.4ms, p50 64.1ms, p95 295.1ms,
# max 3097.5ms, 200 queries at concurrency 8, 0 errors. Rebuilt here to those
# exact aggregates, which is the point — percentile() reads p99 off the
# third-worst sample, so reproducing 3088.3ms at a 295.1ms p95 takes a
# cluster of slow samples the width of a concurrency wave, not an outlier.
_MIXED_FAST = [32.4 + (64.1 - 32.4) * i / 99 for i in range(100)]
_MIXED_MID = [64.1 + (295.1 - 64.1) * i / 89 for i in range(90)]
_MIXED_UPPER = [296.0, 340.0, 420.0, 520.0, 680.0, 900.0]
_MIXED_WAVE = [3088.2, 3088.3, 3092.0, 3097.5]
_MIXED = _MIXED_FAST + _MIXED_MID + _MIXED_UPPER + _MIXED_WAVE


def test_the_rebuilt_run_reports_what_the_failing_job_reported():
    """Guards the fixture below: wrong samples would prove nothing."""
    assert len(_MIXED) == 200
    assert round(min(_MIXED), 1) == 32.4
    assert round(max(_MIXED), 1) == 3097.5
    assert round(search_latency.percentile(_MIXED, 50), 1) == 64.1
    assert round(search_latency.percentile(_MIXED, 95), 1) == 295.1
    assert round(search_latency.percentile(_MIXED, 99), 1) == 3088.3


def test_one_slow_request_does_not_move_p99():
    """Why the count below is the discriminating number and not the max.

    percentile() interpolates between the third- and second-worst at a
    hundredth of the gap, so a single 3s request in 200 lands nowhere near
    the gate: a red p99 means a whole wave stalled together.
    """
    # The same run with the stalled wave replaced by one stalled request.
    lone_outlier = (
        _MIXED_FAST + _MIXED_MID + _MIXED_UPPER + [901.0, 902.0, 903.0, 3097.5]
    )
    assert len(lone_outlier) == len(_MIXED)
    assert max(lone_outlier) == max(_MIXED)

    assert search_latency.percentile(lone_outlier, 99) < 1000.0
    assert search_latency.percentile(_MIXED, 99) > 2000.0


def test_slo_tail_report_counts_what_cleared_the_threshold(monkeypatch):
    """What the failing job's own report could not say.

    The results table stops at p95/p99/max: against `SLO VIOLATION:
    p99=3088.3ms` it shows neither how much of the run was over the gate nor
    where the host's queue was, and both are what names the fault.
    """
    monkeypatch.setattr(os, "getloadavg", lambda: (11.42, 9.8, 6.31))
    monkeypatch.setattr(os, "cpu_count", lambda: 16)

    report = search_latency._slo_tail_report(_MIXED, 2000.0)

    assert report.splitlines() == [
        "    over SLO  : 4 of 200 samples (2.0%)",
        "    slowest   : 3097.5ms, 3092.0ms, 3088.3ms, 3088.2ms, 900.0ms",
        "    mean/p95  : 182.6ms / 295.1ms",
        "    host load : 1m 11.42 (0.71/core over 16), 5m 9.80, 15m 6.31",
    ]


def test_slo_tail_report_separates_a_stalled_wave_from_a_slower_server():
    """Same verdict shape, two different faults.

    A server that got slower puts the run over the gate and pulls the mean
    above p95; a wave the box starved leaves 98% of the run untouched and
    the mean below p95. `SLO VIOLATION: p99=...` reads the same either way.
    """
    slower_server = [3000.0 + i for i in range(200)]

    assert search_latency.percentile(_MIXED, 99) > 2000.0
    assert search_latency.percentile(slower_server, 99) > 2000.0

    wave = search_latency._slo_tail_report(_MIXED, 2000.0)
    slower = search_latency._slo_tail_report(slower_server, 2000.0)

    assert "over SLO  : 4 of 200 samples (2.0%)" in wave
    assert "mean/p95  : 182.6ms / 295.1ms" in wave
    assert "over SLO  : 200 of 200 samples (100.0%)" in slower
    assert "mean/p95  : 3099.5ms / 3189.1ms" in slower


def test_slo_tail_report_is_empty_without_samples():
    """`_latency_slo_violation` never fires on an empty run; stay safe anyway."""
    assert search_latency._slo_tail_report([], 2000.0) == ""


def test_slo_violation_prints_the_tail_under_the_verdict(monkeypatch, capsys):
    """The breakdown has to reach the saved report, not just exist.

    `bench-latency-run` pipes this stdout through `tee` into
    `benchmarks/results/latency-*.txt`, and that directory is the whole of
    what the bench job uploads: what is not printed here is gone with the
    container, as the `/tmp` server state the Makefile "preserves" already
    is.
    """
    monkeypatch.setattr(os, "getloadavg", lambda: (11.42, 9.8, 6.31))
    monkeypatch.setattr(os, "cpu_count", lambda: 16)
    real_client = httpx.AsyncClient

    async def handler(request):
        return httpx.Response(200, json={"matches": []})

    def _client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr(search_latency.httpx, "AsyncClient", _client)

    # Every real request clears a 0.001ms gate, so the verdict and the
    # breakdown under it are live, computed from measured samples.
    result = asyncio.run(
        search_latency.run_benchmark(
            "http://pavedb.test", 4, 2, slo_p99_ms=0.001
        )
    )
    out = capsys.readouterr().out

    assert result is None
    assert "SLO VIOLATION: p99=" in out
    assert "    over SLO  : 4 of 4 samples (100.0%)" in out
    assert "    slowest   : " in out
    assert "    mean/p95  : " in out
    assert "    host load : 1m 11.42 (0.71/core over 16)" in out
