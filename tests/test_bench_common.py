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
import json
import os
import re
import shutil
import signal
import socket
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
    env["TMPDIR"] = str(tmp_path)
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
        timeout=10,
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
    assert "exited before becoming ready (status 127)" in saving.stdout
    assert "exited before becoming ready (status 127)" in plain.stdout
    assert len(kept) == 1
    # Without BENCH_SAVE there is no results dir to put it in; local runs
    # read the $TMPDIR path the recipe already prints and keep the tree clean.
    assert none_kept == []
    assert not (tmp_path / "plain" / "benchmarks").exists()


# Exercise the real recipe with a stdlib server: no service import or model load.
@pytest.fixture
def bench_harness(tmp_path):
    make = _gnu_make()
    if make is None or shutil.which("curl") is None:
        pytest.skip("GNU Make >= 4 and curl required")
    recipe = (_REPO_ROOT / "Makefile").read_text()
    recipe += "\n_fixture-run bench-latency-run bench-stress-run:\n"
    recipe += '\t@echo "fixture-run $@ $(_filt)"\n'
    recipe += "\t@exit $(FIXTURE_STATUS)\n"
    (tmp_path / "Makefile").write_text(recipe)
    server = tmp_path / "server.py"
    server.write_text(textwrap.dedent('''\
        import json
        import os
        import signal
        import time
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from pathlib import Path

        def step_clock(phase):
            if os.environ.get("FIXTURE_STEP_PHASE") == phase:
                Path(os.environ["FIXTURE_CLOCK_FILE"]).write_text(
                    os.environ["FIXTURE_CLOCK_STEP"]
                )

        def ignore_term(*_):
            step_clock("cleanup")

        if os.environ.get("FIXTURE_IGNORE_TERM") == "1":
            signal.signal(signal.SIGTERM, ignore_term)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if os.environ.get("FIXTURE_HANG") == "1":
                    print("accepted health request", flush=True)
                    time.sleep(60)
                self.send_response(200)
                self.end_headers()

        server = ThreadingHTTPServer(
            ("127.0.0.1", int(os.environ["PAVEDB_SERVER__PORT"])), Handler
        )
        print("fixture server started", flush=True)
        Path("server-env.json").write_text(json.dumps({
            key: value for key, value in os.environ.items()
            if key.startswith("PAVEDB_")
        }))
        Path("server.pid").write_text(str(os.getpid()))
        step_clock("startup")
        time.sleep(float(os.environ.get("FIXTURE_READY_DELAY", "0")))
        server.serve_forever()
    '''))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, TMPDIR=str(tmp_path), NO_PROXY="127.0.0.1")
    groups = []

    def kill_group(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def run(target="_bench-with-server", **variables):
        options = {
            "PYTHON": sys.executable,
            "PYTHON_BIN": f"{sys.executable} {server}",
            "BENCH_FORCE_EPHEMERAL": "1",
            "BENCH_STARTUP_TIMEOUT_S": "3",
            "BENCH_SAVE": "1",
            "_ephemeral_port": str(port),
            "_run_target": "_fixture-run",
            "FIXTURE_STATUS": "0",
        }
        options.update(variables)
        command = [make, "--no-print-directory", "-o", "install-dev", target]
        command += [f"{key}={value}" for key, value in options.items()]
        started = time.monotonic()
        proc = subprocess.Popen(
            command, cwd=tmp_path, env=env, start_new_session=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        groups.append(proc.pid)
        try:
            output, _ = proc.communicate(timeout=12)
        except subprocess.TimeoutExpired:
            kill_group(proc.pid)
            proc.communicate(timeout=3)
            pytest.fail("benchmark harness exceeded the 12s fixture watchdog")
        return proc.returncode, output, time.monotonic() - started

    yield run, env, port, server
    # Clean up after assertions, so a leaked server cannot pass the death checks.
    for pid in groups:
        kill_group(pid)


def test_rate_limit_variant_configures_a_non_admin_tenant(bench_harness, tmp_path):
    run, _env, _port, _server = bench_harness
    code, output, _elapsed = run(
        "bench-latency-rate-limited", LAT_LENGTH="200", LAT_FILTERS="none",
    )
    assert code == 0, output
    config = json.loads((tmp_path / "server-env.json").read_text())
    assert config["PAVEDB_AUTH__MODE"] == "static"
    assert json.loads(config["PAVEDB_AUTH__API_KEYS"]) == {
        "bench": "bench-rate-limit-token",
    }
    assert config["PAVEDB_AUTH__GLOBAL_KEY"] != "bench-rate-limit-token"
    assert config["PAVEDB_TENANTS__DEFAULT_MAX_RPM"] == "300"
    assert config["PAVEDB_TENANTS__DEFAULT_MAX_RPH"] == "0"
    assert config["PAVEDB_TENANTS__DEFAULT_MAX_CONCURRENT"] == "0"


@pytest.fixture(params=[0, 300, -300], ids=["steady", "forward", "backward"])
def bench_clock_step(request, bench_harness, tmp_path):
    """Step process realtime only; the kernel and monotonic clock stay intact.

    Bash derives SECONDS from gettimeofday, not CLOCK_MONOTONIC. Interpose the
    realtime libc calls so the actual recipe sees the same discontinuity a
    guest clock correction would cause, without privileges or a host change.
    """
    step = request.param
    _, env, _, _ = bench_harness
    env.update(
        FIXTURE_CLOCK_FILE=str(tmp_path / "clock-step"),
        FIXTURE_CLOCK_STEP=str(step), FIXTURE_STEP_PHASE="startup",
    )
    if not step:
        return step
    compiler = shutil.which("cc")
    if sys.platform != "linux" or compiler is None:
        pytest.skip("clock interposition requires Linux and a C compiler")
    source = tmp_path / "clock.c"
    library = tmp_path / "clock.so"
    source.write_text(textwrap.dedent('''\
        #define _GNU_SOURCE
        #include <dlfcn.h>
        #include <stdio.h>
        #include <stdlib.h>
        #include <sys/time.h>
        #include <time.h>

        static long offset(void) {
            long seconds = 0;
            FILE *file = fopen(getenv("FIXTURE_CLOCK_FILE"), "r");
            if (file) {
                if (fscanf(file, "%ld", &seconds) != 1) seconds = 0;
                fclose(file);
            }
            return seconds;
        }
        int gettimeofday(struct timeval *tv, void *tz) {
            int (*real_get)(struct timeval *, void *) =
                dlsym(RTLD_NEXT, "gettimeofday");
            int result = real_get(tv, tz);
            if (result == 0) tv->tv_sec += offset();
            return result;
        }
        time_t time(time_t *out) {
            time_t (*real_time)(time_t *) = dlsym(RTLD_NEXT, "time");
            time_t result = real_time(NULL) + offset();
            if (out) *out = result;
            return result;
        }
        int clock_gettime(clockid_t id, struct timespec *ts) {
            int (*real_clock)(clockid_t, struct timespec *) =
                dlsym(RTLD_NEXT, "clock_gettime");
            int result = real_clock(id, ts);
            if (result == 0 && id == CLOCK_REALTIME) ts->tv_sec += offset();
            return result;
        }
    '''))
    subprocess.run(
        [compiler, "-shared", "-fPIC", str(source), "-o", str(library), "-ldl"],
        check=True, capture_output=True, text=True,
    )
    env["LD_PRELOAD"] = str(library)
    return step


@pytest.mark.parametrize("ignore_term", [False, True])
def test_nonresponding_managed_health_is_bounded_and_preserves_logs(
    bench_harness, tmp_path, ignore_term, bench_clock_step,
):
    run, env, _, _ = bench_harness
    env["FIXTURE_HANG"] = "1"
    env["FIXTURE_IGNORE_TERM"] = "1" if ignore_term else "0"
    status, output, elapsed = run()

    assert status != 0
    allowance = 6 if ignore_term else 0
    assert elapsed < 4.5 + allowance, output  # 3s startup and scheduling slack.
    assert "did not become ready in 3s" in output
    assert elapsed >= 3, output
    assert re.search(r"_bench-with-server\] Error 1\b", output)
    assert "fixture-run" not in output
    (log,) = (tmp_path / "benchmarks/results").glob("server-*.log")
    assert "accepted health request" in log.read_text()
    pid = int((tmp_path / "server.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize("failure", ["0", "23"])
def test_term_resistant_server_has_bounded_cleanup(
    bench_harness, tmp_path, failure, bench_clock_step,
):
    run, env, _, _ = bench_harness
    env["FIXTURE_IGNORE_TERM"] = "1"
    env["FIXTURE_STEP_PHASE"] = "cleanup"
    status, output, elapsed = run(FIXTURE_STATUS=failure)

    assert status != 0
    assert elapsed < 9, output  # Startup, 5s TERM grace, 1s KILL grace, slack.
    assert "sending KILL" in output
    assert elapsed >= 5, output  # A forward clock step cannot cut the TERM grace.
    # GNU make maps the inner target's failure to 2; cleanup must retain it.
    expected = "1" if failure == "0" else "2"
    assert re.search(r"_bench-with-server\] Error " + expected + r"\b", output)
    (log,) = (tmp_path / "benchmarks/results").glob("server-*.log")
    assert "fixture server started" in log.read_text()
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "server.pid").read_text()), 0)


def test_delayed_healthy_server_survives_clock_steps(
    bench_harness, tmp_path, bench_clock_step,
):
    run, env, _, _ = bench_harness
    env["FIXTURE_READY_DELAY"] = "1.5"
    status, output, elapsed = run()

    assert status == 0, output
    assert 1.5 <= elapsed < 4.5, output
    assert "fixture-run _fixture-run" in output
    assert "benchmark ready:" in output
    assert not list(tmp_path.glob("pavedb-bench.*"))
    # Compare clocks from inside the job, independently of trace timestamps.
    ready = re.search(
        r"benchmark ready: elapsed_ms=(\d+) wall_elapsed_ms=(-?\d+)", output
    )
    monotonic_ms, wall_ms = map(int, ready.groups())
    assert abs(wall_ms - monotonic_ms - bench_clock_step * 1000) < 100


def test_healthy_benchmark_keeps_all_variants_and_cleans_up(bench_harness, tmp_path):
    run, _, _, _ = bench_harness
    status, output, _ = run(target="benchmark")

    assert status == 0, output
    for variant in ("none", "exact", "wildcard", "mixed"):
        assert f"fixture-run bench-latency-run {variant}" in output
    assert "fixture-run bench-stress-run" in output
    assert not list(tmp_path.glob("pavedb-bench.*"))
    assert not list((tmp_path / "benchmarks/results").glob("server-*.log"))
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "server.pid").read_text()), 0)


@pytest.mark.parametrize("command, code", [("/nonexistent/python", 127), ("true", 0)])
def test_latency_startup_failure_reaches_the_benchmark_caller(
    bench_harness, command, code,
):
    run, _, _, _ = bench_harness
    status, output, elapsed = run(
        target="benchmark", PYTHON_BIN=command,
        BENCH_STARTUP_TIMEOUT_S="30",
    )

    assert status != 0
    assert elapsed < 3, output
    assert f"exited before becoming ready (status {code})" in output
    assert "fixture-run" not in output


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("hang", [False, True])
def test_external_probes_are_bounded_and_leave_server_owned_by_caller(
    bench_harness, tmp_path, configured, hang,
):
    run, env, port, server = bench_harness
    external_env = dict(env, PAVEDB_SERVER__PORT=str(port))
    external_env["FIXTURE_HANG"] = "1" if hang else "0"
    external = subprocess.Popen(
        [sys.executable, str(server)], cwd=tmp_path, env=external_env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 3
        while not (tmp_path / "server.pid").exists():
            assert external.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        url = f"http://127.0.0.1:{port}"
        variables = {"BENCH_SERVER_URL" if configured else "_default_url": url}
        status, output, elapsed = run(
            BENCH_FORCE_EPHEMERAL="0", PYTHON_BIN="/nonexistent/python",
            **variables,
        )
        assert (status != 0) == hang, output
        assert elapsed < 4.5, output
        assert external.poll() is None
        assert ("fixture-run _fixture-run" in output) != hang
        if configured or not hang:
            assert "starting ephemeral" not in output
    finally:
        external.kill()
        external.wait(timeout=3)
