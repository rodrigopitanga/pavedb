# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import json
import pytest
import pave.log as ops_log


@pytest.fixture(autouse=True)
def reset():
    """Reset ops_log singleton state before and after each test."""
    ops_log.configure(None)
    yield
    ops_log.configure(None)


def test_emit_noop_when_not_configured(capsys):
    ops_log.emit(op="search", tenant="t", collection="c",
                 latency_ms=1.0, status="ok")
    assert capsys.readouterr().out == ""


def test_configure_stdout_emits_json(capsys):
    ops_log.configure("stdout")
    ops_log.emit(op="search", tenant="t", collection="c",
                 k=5, hits=3, latency_ms=12.5, status="ok")
    data = json.loads(capsys.readouterr().out.strip())
    assert data["op"] == "search"
    assert data["tenant"] == "t"
    assert data["collection"] == "c"
    assert data["k"] == 5
    assert data["hits"] == 3
    assert data["status"] == "ok"
    assert "ts" in data


def test_none_fields_dropped(capsys):
    ops_log.configure("stdout")
    ops_log.emit(op="ingest", tenant="t", collection="c",
                 docid="d1", chunks=3, latency_ms=5.0, status="ok",
                 request_id=None, error_code=None)
    data = json.loads(capsys.readouterr().out.strip())
    assert "request_id" not in data
    assert "error_code" not in data
    assert data["chunks"] == 3


def test_configure_null_string_disables(capsys):
    ops_log.configure("stdout")
    ops_log.configure("null")
    ops_log.emit(op="search", tenant="t", collection="c",
                 latency_ms=1.0, status="ok")
    assert capsys.readouterr().out == ""


def test_configure_none_disables(capsys):
    ops_log.configure("stdout")
    ops_log.configure(None)
    ops_log.emit(op="search", tenant="t", collection="c",
                 latency_ms=1.0, status="ok")
    assert capsys.readouterr().out == ""


def test_configure_file(tmp_path):
    path = tmp_path / "ops.jsonl"
    ops_log.configure(str(path))
    ops_log.emit(op="ingest", tenant="t", collection="c",
                 docid="d1", chunks=2, latency_ms=100.0, status="ok")
    ops_log.close()
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["op"] == "ingest"
    assert data["chunks"] == 2
    assert data["status"] == "ok"


def test_configure_file_multiple_lines(tmp_path):
    path = tmp_path / "ops.jsonl"
    ops_log.configure(str(path))
    ops_log.emit(op="search", tenant="t", collection="c",
                 k=5, hits=3, latency_ms=10.0, status="ok")
    ops_log.emit(op="ingest", tenant="t", collection="c",
                 docid="d1", chunks=1, latency_ms=50.0, status="ok")
    ops_log.close()
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["op"] == "search"
    assert json.loads(lines[1])["op"] == "ingest"


def test_ts_format(capsys):
    ops_log.configure("stdout")
    ops_log.emit(op="search", tenant="t", collection="c",
                 latency_ms=1.0, status="ok")
    data = json.loads(capsys.readouterr().out.strip())
    ts = data["ts"]
    # ISO 8601 UTC with millisecond precision: "2026-02-26T23:55:55.123Z"
    assert ts.endswith("Z")
    assert len(ts) == 24


def test_close_noop_when_not_configured():
    ops_log.close()  # must not raise


def test_reconfigure_closes_previous_file(tmp_path):
    p1 = tmp_path / "first.jsonl"
    p2 = tmp_path / "second.jsonl"
    ops_log.configure(str(p1))
    ops_log.emit(op="search", tenant="t", collection="c",
                 latency_ms=1.0, status="ok")
    ops_log.configure(str(p2))
    ops_log.emit(op="ingest", tenant="t", collection="c",
                 docid="d1", chunks=1, latency_ms=2.0, status="ok")
    ops_log.close()
    assert len(p1.read_text().strip().splitlines()) == 1
    assert len(p2.read_text().strip().splitlines()) == 1


def test_ops_event_supports_callable_tenant_and_collection(capsys):
    ops_log.configure("stdout")

    @ops_log.ops_event(
        "search_common",
        tenant=lambda kw, r: "global",
        coll=lambda kw, r: "common",
        k="k",
        request_id="rid",
    )
    async def handler(*, k, rid):
        return {"ok": True}

    asyncio.run(handler(k=7, rid="req-1"))
    data = json.loads(capsys.readouterr().out.strip())
    assert data["op"] == "search_common"
    assert data["tenant"] == "global"
    assert data["collection"] == "common"
    assert data["k"] == 7
    assert data["request_id"] == "req-1"
    assert data["status"] == "ok"
