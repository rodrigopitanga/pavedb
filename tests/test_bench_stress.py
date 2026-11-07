# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import asyncio
import os
import select
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import httpx
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


@pytest.fixture
def stress_client(monkeypatch):
    """Keep the real driver while controlling individual HTTP phases."""
    client_type = httpx.AsyncClient

    def install(handler):
        client = client_type(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(
            stress.httpx, "AsyncClient", lambda **kwargs: client
        )
        client.base_url = "http://fixture"
        return client

    return install


def test_setup_has_one_elapsed_budget(monkeypatch, stress_client, capsys):
    calls = []

    async def handler(request):
        calls.append((request.method, request.url.path))
        # Every request finishes; together they exceed the setup budget.
        await asyncio.sleep(0.03)
        return httpx.Response(200, json={})

    client = stress_client(handler)
    monkeypatch.setattr(stress, "SETUP_TIMEOUT_S", 0.15, raising=False)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="setup.*deadline"):
        asyncio.run(asyncio.wait_for(
            stress.run_stress("http://fixture", 0, 1), 1
        ))
    assert time.monotonic() - started < 0.8
    assert client.is_closed
    assert any(method == "DELETE" for method, _ in calls)
    assert "setup: seeding" in capsys.readouterr().out


@pytest.mark.parametrize("phase", ["setup", "workers"])
def test_endpoint_accepting_without_response_is_bounded(monkeypatch, capsys, phase):
    monkeypatch.setattr(stress, "SETUP_TIMEOUT_S", 0.1 if phase == "setup" else 1)
    monkeypatch.setattr(stress, "DRAIN_TIMEOUT_S", 0.03)

    async def stalled(client, world, stats):
        await client.get("/stall")

    monkeypatch.setitem(stress.SUITES, "fixture", [(stalled, "stalled", 1)])

    async def run():
        accepted = asyncio.Event()
        connections = set()

        async def silent(reader, writer):
            task = asyncio.current_task()
            connections.add(task)
            try:
                head = await reader.readuntil(b"\r\n\r\n")
                if phase == "setup" or head.startswith(b"GET /stall "):
                    accepted.set()
                    await asyncio.Event().wait()
                else:
                    for line in head.split(b"\r\n"):
                        if line.lower().startswith(b"content-length:"):
                            await reader.readexactly(int(line.split(b":", 1)[1]))
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                        b"Connection: close\r\n\r\n{}"
                    )
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                connections.remove(task)

        server = await asyncio.start_server(silent, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            with pytest.raises(RuntimeError, match=phase + ".*deadline"):
                await asyncio.wait_for(
                    stress.run_stress(
                        f"http://127.0.0.1:{port}", 0.1, 1, suite="fixture"
                    ), 2,
                )
            assert accepted.is_set()
        finally:
            server.close()
            for task in list(connections):
                task.cancel()
            await asyncio.gather(*list(connections), return_exceptions=True)
            await server.wait_closed()

    asyncio.run(run())
    output = capsys.readouterr().out
    assert "setup: header" in output
    assert "deadline" in output


def test_worker_exception_cannot_produce_a_success(monkeypatch, stress_client):
    stress_client(lambda request: httpx.Response(200, json={}))

    async def broken(client, world, stats):
        stats.record(stress.OpResult("broken", 0, True))
        raise RuntimeError("worker failed")

    monkeypatch.setitem(stress.SUITES, "fixture", [(broken, "broken", 1)])
    with pytest.raises(RuntimeError, match="worker failed"):
        asyncio.run(stress.run_stress("http://fixture", 0.01, 1, suite="fixture"))


def test_stalled_workers_are_joined_and_fail_before_coverage(
    monkeypatch, stress_client,
):
    client = stress_client(lambda request: httpx.Response(200, json={}))
    monkeypatch.setattr(stress, "DRAIN_TIMEOUT_S", 0.03, raising=False)
    joined = []

    async def stalled(client, world, stats):
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.01)
            joined.append(True)

    monkeypatch.setitem(stress.SUITES, "fixture", [(stalled, "stalled", 1)])
    with pytest.raises(RuntimeError, match="workers.*deadline"):
        asyncio.run(asyncio.wait_for(
            stress.run_stress("http://fixture", 0.01, 1, suite="fixture"), 1
        ))
    assert joined == [True]
    assert client.is_closed


@pytest.mark.parametrize("phase", ["coverage", "cleanup", "client close"])
def test_post_workload_phases_are_bounded(
    monkeypatch, stress_client, phase, capsys,
):
    async def handler(request):
        if phase == "cleanup" and request.method == "DELETE":
            await asyncio.Event().wait()
        return httpx.Response(200, json={})

    client = stress_client(handler)

    async def operation(client, world, stats):
        if phase == "coverage":
            await asyncio.Event().wait()
        stats.record(stress.OpResult("fixture", 0, True))
        await asyncio.sleep(0.001)

    async def timed_operation(client, world, stats):
        await asyncio.sleep(0.001)

    async def stuck_close():
        await asyncio.Event().wait()

    monkeypatch.setitem(stress.SUITES, "fixture", [(operation, "fixture", 1)])
    monkeypatch.setattr(stress, "_pick_operation", lambda operations: timed_operation)
    for constant in ("COVERAGE_TIMEOUT_S", "CLEANUP_TIMEOUT_S", "CLOSE_TIMEOUT_S"):
        monkeypatch.setattr(stress, constant, 0.03, raising=False)
    if phase == "client close":
        monkeypatch.setattr(client, "aclose", stuck_close)
    with pytest.raises(RuntimeError, match=phase + ".*deadline"):
        asyncio.run(asyncio.wait_for(
            stress.run_stress("http://fixture", 0, 1, suite="fixture"), 1
        ))
    assert phase in capsys.readouterr().out
    if phase != "client close":
        assert client.is_closed


@pytest.mark.parametrize("suite", ["base", "critical", "full"])
def test_healthy_suites_run_all_operations(app, monkeypatch, suite, capsys):
    """Exercise the real HTTP routes/store with the suite's FakeEmbedder."""
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        stress.httpx, "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.ASGITransport(app), **kwargs),
    )
    # Pick a harmless read during the short timed part, then exercise every
    # remaining operation in the normal coverage pass without random races.
    monkeypatch.setattr(stress, "_pick_operation", lambda ops: stress.op_health_live)
    stats = asyncio.run(stress.run_stress(
        "http://localhost", 0.02, 2, max_error_pct=1, suite=suite,
    ))
    assert stats is not None, capsys.readouterr().out
    assert stats.results
    assert all(result.ok for result in stats.results)
    output = capsys.readouterr().out
    for _, name, _ in stress.SUITES[suite]:
        assert name in stats.summary() or f"<- coverage op: {name}" in output
    for phase in ("setup", "workload", "cleanup", "client close"):
        assert f"stress {phase}: completed in" in output


def test_setup_failure_survives_cleanup_failure(monkeypatch, stress_client, capsys):
    async def handler(request):
        if request.method == "DELETE":
            await asyncio.Event().wait()
        if request.url.path.endswith("/documents"):
            return httpx.Response(403, json={"error": "seed denied"})
        return httpx.Response(200, json={})

    client = stress_client(handler)
    monkeypatch.setattr(stress, "SETUP_TIMEOUT_S", 0.1)
    monkeypatch.setattr(stress, "CLEANUP_TIMEOUT_S", 0.03)
    with pytest.raises(RuntimeError, match="setup.*deadline"):
        asyncio.run(stress.run_stress("http://fixture", 0, 1))
    assert client.is_closed
    output = capsys.readouterr().out
    assert "cleanup: deadline" in output


def test_saved_stress_flushes_progress_and_preserves_failure(tmp_path):
    from test_bench_common import _gnu_make

    make = _gnu_make()
    if make is None:
        pytest.skip("GNU Make >= 4 required")
    root = Path(__file__).resolve().parents[1]
    shutil.copy(root / "Makefile", tmp_path / "Makefile")
    (tmp_path / "benchmarks").mkdir()
    # Drive the real CLI and make/tee pipeline with a stalled HTTP transport.
    (tmp_path / "benchmarks/stress.py").write_text(textwrap.dedent(f'''\
        import asyncio
        import sys
        sys.path.insert(0, {str(root / "benchmarks")!r})
        import httpx
        import stress

        async def handler(request):
            await asyncio.Event().wait()

        client_type = httpx.AsyncClient
        stress.httpx.AsyncClient = lambda **kw: client_type(
            transport=httpx.MockTransport(handler), **kw
        )
        stress.SETUP_TIMEOUT_S = 0.5
        stress.main()
    '''))
    proc = subprocess.Popen(
        [make, "--no-print-directory", "bench-stress-run",
         f"PYTHON={sys.executable}", f"PYTHON_BIN={sys.executable}",
         "BENCH_SAVE=1", "STR_LENGTH=30", "STR_CONCUR=4", "STR_MAX_ERROR_PCT=1"],
        cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=dict(os.environ, TMPDIR=str(tmp_path)),
    )
    try:
        output = b""
        deadline = time.monotonic() + 4
        while b"stress setup: started" not in output:
            assert time.monotonic() < deadline, output
            readable, _, _ = select.select([proc.stdout], [], [], 0.1)
            if readable:
                chunk = os.read(proc.stdout.fileno(), 65536)
                assert chunk, output
                output += chunk
        # Progress is observable while Python is still waiting, even under tee.
        assert proc.poll() is None
        rest, _ = proc.communicate(timeout=4)
        output += rest
        assert proc.returncode != 0, output
        (saved,) = (tmp_path / "benchmarks/results").glob("stress-*.txt")
        report = saved.read_text()
        assert "stress setup: deadline 0.5s exceeded" in report
        assert "RuntimeError:" in report  # stderr is saved as well as stdout.
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=3)
