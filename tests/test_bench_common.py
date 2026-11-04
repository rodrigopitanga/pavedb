# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What a saved benchmark report has to say about the box it measured.

The bench gate takes whatever runner the project assigns and serializes
nothing but its own bench jobs, so a neighbouring job's CPU is inside every
measurement. `gitlab-ci.yml` and PLAN-BENCH-CI both send a reader of a red
gate to "the runner the job reports and the results it saves" before calling
it a regression — and the header named the host but never its queue, so that
step could not be taken from the artifact. These pin the queue into it.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "benchmarks"),
)

import common  # type: ignore[import-not-found]


def test_host_load_reports_all_three_windows_per_core(monkeypatch):
    """A queue of 8.0 on 16 cores and on 2 are different machines."""
    monkeypatch.setattr(os, "getloadavg", lambda: (11.42, 9.8, 6.31))
    monkeypatch.setattr(os, "cpu_count", lambda: 16)

    assert common.host_load() == (
        "1m 11.42 (0.71/core over 16), 5m 9.80, 15m 6.31"
    )


def test_host_load_omits_the_per_core_figure_without_a_core_count(monkeypatch):
    """A header line is worth more than a ZeroDivisionError mid-benchmark."""
    monkeypatch.setattr(os, "getloadavg", lambda: (2.0, 1.0, 0.5))
    monkeypatch.setattr(os, "cpu_count", lambda: None)

    assert common.host_load() == "1m 2.00, 5m 1.00, 15m 0.50"


def test_host_load_is_none_where_the_platform_has_none(monkeypatch):
    """Windows has no getloadavg; the rest of the header still prints."""

    def _unavailable():
        raise OSError("not supported")

    monkeypatch.setattr(os, "getloadavg", _unavailable)

    assert common.host_load() is None


def test_run_header_carries_the_host_queue(monkeypatch, capsys):
    monkeypatch.setattr(os, "getloadavg", lambda: (11.42, 9.8, 6.31))
    monkeypatch.setattr(os, "cpu_count", lambda: 16)

    async def handler(request):
        return httpx.Response(404)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://127.0.0.1:18086",
        ) as client:
            await common.print_run_header(
                client,
                "http://127.0.0.1:18086",
                "search_latency",
            )

    asyncio.run(run())
    lines = capsys.readouterr().out.splitlines()

    assert "    host load : 1m 11.42 (0.71/core over 16), 5m 9.80, 15m 6.31" in lines
    # Under the machine it describes, above the server it was measuring.
    assert [i for i, ln in enumerate(lines) if ln.startswith("    runner")][0] < (
        [i for i, ln in enumerate(lines) if ln.startswith("    host load")][0]
    )


def test_run_header_prints_without_a_queue_to_report(monkeypatch, capsys):
    def _unavailable():
        raise OSError("not supported")

    monkeypatch.setattr(os, "getloadavg", _unavailable)

    async def handler(request):
        return httpx.Response(404)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://127.0.0.1:18086",
        ) as client:
            await common.print_run_header(
                client,
                "http://127.0.0.1:18086",
                "search_latency",
            )

    asyncio.run(run())
    out = capsys.readouterr().out

    assert "==> Benchmark: search_latency" in out
    assert "host load" not in out


# --- what survives a failed bench job ---
# `_bench-with-server` sets keep_data_dir on every failure and prints the
# ephemeral data_dir and server.log it kept. Those live under $TMPDIR, which no
# GitLab artifact can reach: job 17708580396 ended on "Preserving ephemeral
# server state at /tmp/pavedb-bench.2z28Qi", uploaded `benchmarks/results/` and
# left the log behind in the container. The recipe now also copies the log in
# beside the saved results, without touching the exit it fails with.
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


def _failed_bench_run(tmp_path, **env_overrides):
    """Drive _bench-with-server to its startup-failure path and come back.

    A one-second startup budget against a $PYTHON_BIN that does not exist is
    the cheapest real failure the recipe has: the server writes its own error
    to the log, never answers /health/live, and the recipe exits non-zero
    through the same branch a red benchmark takes.
    """
    make = _gnu_make()
    if make is None:
        pytest.skip("GNU Make >= 4 not available")
    tmp_path.mkdir(parents=True, exist_ok=True)
    shutil.copy(_REPO_ROOT / "Makefile", tmp_path / "Makefile")
    env = dict(os.environ)
    env.pop("BENCH_SAVE", None)
    env.update(env_overrides)
    proc = subprocess.run(
        [
            make,
            "-C",
            str(tmp_path),
            "_bench-with-server",
            "_run_target=help",
            "BENCH_FORCE_EPHEMERAL=1",
            "BENCH_STARTUP_TIMEOUT_S=1",
            "PYTHON_BIN=/nonexistent/python",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    kept = sorted((tmp_path / "benchmarks" / "results").glob("server-*.log"))
    return proc, kept


def test_a_failed_run_leaves_its_server_log_where_an_artifact_can_reach_it(
    tmp_path,
):
    proc, kept = _failed_bench_run(tmp_path, BENCH_SAVE="1")

    assert proc.returncode != 0
    assert "Preserving ephemeral server state at" in proc.stdout
    assert len(kept) == 1
    # Named after the data_dir the trace reports, so the two line up.
    assert f"Server log copied to benchmarks/results/{kept[0].name}" in proc.stdout
    # And it is the server's own error, not an empty placeholder.
    assert "/nonexistent/python" in kept[0].read_text(encoding="utf-8")


def test_preserving_the_log_does_not_change_what_the_run_exits_with(tmp_path):
    """A diagnostic that alters the verdict is not a diagnostic."""
    saving, kept = _failed_bench_run(tmp_path / "saving", BENCH_SAVE="1")
    plain, none_kept = _failed_bench_run(tmp_path / "plain")

    assert saving.returncode == plain.returncode != 0
    assert "did not become ready in 1s" in saving.stdout
    assert "did not become ready in 1s" in plain.stdout
    assert len(kept) == 1
    # Without BENCH_SAVE there is no results dir to put it in; local runs
    # read the $TMPDIR path the recipe already prints and keep the tree clean.
    assert none_kept == []
    assert not (tmp_path / "plain" / "benchmarks").exists()
