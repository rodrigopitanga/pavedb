# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import json
import logging
import logging.config
import os
import stat
import pytest
import pave.log as ops_log
from pave.auth import AuthContext
from pave.main import _uvicorn_access_log_config


def test_exception_diagnostic_omits_traceback_filename_and_message():
    code = compile(
        "raise PermissionError(13, 'provider secret')",
        "/srv/private/sk-test-secret/provider.py",
        "exec",
    )
    with pytest.raises(PermissionError) as raised:
        exec(code, {})

    detail = ops_log.exception_diagnostic(raised.value)
    assert "PermissionError errno=13" in detail
    assert "frame 2 line 1" in detail
    assert "private" not in detail
    assert "sk-test-secret" not in detail
    assert "provider secret" not in detail


@pytest.mark.parametrize("destination", ["file", "stdout"])
@pytest.mark.parametrize(
    "target",
    ["/v1/search", "/v1/search?q=private+search&token=secret",
     "/v1/search%3Fpath?q=private%20search"],
)
def test_access_log_omits_query_string(destination, target, tmp_path, capsys):
    path = tmp_path / "access.log"
    config = _uvicorn_access_log_config(str(path) if destination == "file" else None)
    configurator = logging.config.DictConfigurator(config)
    # Resolve the configured classes just as dictConfig does, without changing
    # the process-wide loggers used by the rest of the suite.
    config = configurator.config
    config["formatters"]["access"] = configurator.configure_formatter(
        config["formatters"]["access"]
    )
    handler = configurator.configure_handler(config["handlers"]["access"])
    args = ("127.0.0.1:1234", "GET", target, "1.1", 200)
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0,
        '%s - "%s %s HTTP/%s" %d', args, None,
    )
    try:
        handler.handle(record)
        handler.flush()
    finally:
        handler.close()
    output = path.read_text() if destination == "file" else capsys.readouterr().out
    assert f'GET {target.split("?", 1)[0]} HTTP/1.1' in output
    assert "127.0.0.1:1234" in output
    assert "200 OK" in output
    assert "?" not in output
    assert "private" not in output
    assert "secret" not in output
    assert record.args == args


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions")
@pytest.mark.parametrize("existing", [False, True])
def test_access_log_is_owner_only_and_appends(existing, tmp_path):
    path = tmp_path / "access.log"
    if existing:
        path.write_text("existing entry\n")
        path.chmod(0o666)
    old_umask = os.umask(0)
    try:
        handler = ops_log.AccessFileHandler(path, encoding="utf-8")
    finally:
        os.umask(old_umask)
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        handler.handle(logging.makeLogRecord({"msg": "new entry"}))
    finally:
        handler.close()
    expected = "existing entry\nnew entry\n" if existing else "new entry\n"
    assert path.read_text() == expected


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="requires O_NOFOLLOW")
def test_access_log_rejects_symlink(tmp_path):
    target = tmp_path / "target"
    target.write_text("untouched")
    target.chmod(0o644)
    path = tmp_path / "access.log"
    path.symlink_to(target)
    with pytest.raises(OSError):
        ops_log.AccessFileHandler(path)
    assert target.read_text() == "untouched"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


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


def test_configure_file_creates_parent_dirs(tmp_path):
    path = tmp_path / "nested" / "logs" / "ops.jsonl"
    ops_log.configure(str(path))
    ops_log.emit(op="search", tenant="t", collection="c", status="ok")
    ops_log.close()

    assert path.is_file()


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


def test_ops_event_infers_actor_from_ctx(capsys):
    ops_log.configure("stdout")

    @ops_log.ops_event("search", request_id="rid")
    async def handler(*, tenant, name, rid, ctx):
        return {"ok": True}

    asyncio.run(
        handler(
            tenant="acme",
            name="docs",
            rid="req-2",
            ctx=AuthContext(tenant="acme", is_admin=False),
        )
    )
    data = json.loads(capsys.readouterr().out.strip())
    assert data["actor"] == "tenant:acme"
    assert data["tenant"] == "acme"
    assert data["collection"] == "docs"


def test_configure_file_creates_owner_group_only_file(tmp_path):
    import stat

    old = os.umask(0o022)
    try:
        target = tmp_path / "logs" / "ops.jsonl"
        ops_log.configure(str(target))
        ops_log.emit(op="search", tenant="t", collection="c")
    finally:
        os.umask(old)
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o750
    line = target.read_text(encoding="utf-8").splitlines()[0]
    assert json.loads(line)["op"] == "search"


@pytest.mark.parametrize(
    "dev,expected_debug",
    [("no", False), ("off", False), ("disabled", False), ("", False),
     ("on", True), ("yes", True), (True, True), (False, False)],
)
def test_dev_stream_level_parses_the_dev_flag(dev, expected_debug, monkeypatch):
    """Only cfg_bool's vocabulary may force the pave stream to DEBUG: the env
    overlay leaves PAVEDB_DEV=no as the string "no", which is truthy."""
    import logging

    from pave.config import get_cfg

    monkeypatch.delenv("PAVEDB_LOG__LEVEL", raising=False)
    cfg = get_cfg()
    cfg.set("dev", dev)
    cfg.set("log.level", "INFO")

    # _init_logger() rebuilds the root handlers in place; put them back so the
    # rest of the suite (and caplog) keeps the configuration it started with.
    root = logging.getLogger()
    pave_logger = logging.getLogger("pave")
    saved_handlers = root.handlers[:]
    saved_root_level = root.level
    saved_pave_level = pave_logger.level
    try:
        level = ops_log._init_logger().level
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_root_level)
        pave_logger.setLevel(saved_pave_level)

    assert level == (logging.DEBUG if expected_debug else logging.INFO)
