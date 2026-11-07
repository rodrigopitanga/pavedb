# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import importlib
from threading import Lock

import numpy as np
import pytest

import pave.embedders.sbert_worker as worker_mod


class _FakeConn:
    def __init__(self, responses: list[object], *, polls=()):
        self._responses = list(responses)
        self._polls = list(polls)
        self.sent: list[object] = []
        self.closed = False

    def send(self, msg):
        self.sent.append(msg)

    def poll(self, timeout=None) -> bool:
        if self._polls:
            return self._polls.pop(0)
        return bool(self._responses)

    def recv(self):
        if not self._responses:
            raise EOFError()
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


class _FakeProc:
    def __init__(self):
        self.alive = True
        self.join_calls: list[float | None] = []
        self.terminated = 0

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self):
        self.terminated += 1
        self.alive = False

    def join(self, timeout=None):
        self.join_calls.append(timeout)


def test_process_sbert_restarts_after_connection_drop(monkeypatch):
    factory = importlib.reload(worker_mod)

    first_proc = _FakeProc()
    first_conn = _FakeConn([("ready", 3), EOFError()])
    second_proc = _FakeProc()
    second_conn = _FakeConn(
        [
            ("ready", 3),
            ("ok", np.array([[1.0, 2.0, 3.0]], dtype=np.float64)),
        ]
    )
    spawned: list[tuple[_FakeProc, _FakeConn]] = []

    def fake_spawn(self):
        pair = (first_proc, first_conn) if not spawned else (second_proc, second_conn)
        spawned.append(pair)
        return pair

    monkeypatch.setattr(
        factory.ProcessSbertEmbedder,
        "_spawn_worker_process",
        fake_spawn,
        raising=True,
    )

    emb = factory.ProcessSbertEmbedder(
        model_name="sentence-transformers/test-model",
        device="cpu",
        batch_size=8,
        workers=1,
    )
    out = emb.encode(["hello"])

    assert out.dtype == np.float32
    assert out.shape == (1, 3)
    assert emb.dim == 3
    assert len(spawned) == 2
    assert first_conn.sent == [("encode", ["hello"]), ("close",)]
    assert second_conn.sent == [("encode", ["hello"])]

    emb.close()
    assert second_conn.sent == [("encode", ["hello"]), ("close",)]
    assert second_conn.closed is True
    assert second_proc.join_calls


def test_process_sbert_auto_worker_count_is_bounded(monkeypatch):
    factory = importlib.reload(worker_mod)
    monkeypatch.setattr(factory.os, "cpu_count", lambda: 20)

    assert factory._resolve_worker_count("auto") == 2
    assert factory._resolve_worker_count(None) == 2
    assert factory._resolve_worker_count(2) == 2
    assert factory._resolve_worker_count(0) == 1


@pytest.mark.parametrize("method", ["encode", "encode_queries"])
@pytest.mark.parametrize("failure", [TimeoutError, EOFError, BrokenPipeError, OSError])
def test_terminal_failure_retires_pipe_before_next_request(
    monkeypatch, method, failure
):
    stale = ("ok", [[9.0, 9.0, 9.0]])
    failed = [
        (
            _FakeProc(),
            _FakeConn(
                [("ready", 3), stale]
                if failure is TimeoutError
                else [("ready", 3), failure(), stale],
                polls=[True, False] if failure is TimeoutError else (),
            ),
        )
        for _ in range(2)
    ]
    fresh_proc = _FakeProc()
    fresh_conn = _FakeConn([
        ("ready", 3),
        ("ok", [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]),
        ("ok", [[7.0, 8.0, 9.0]]),
    ])
    pairs = iter([*failed, (fresh_proc, fresh_conn)])
    monkeypatch.setattr(
        worker_mod.ProcessSbertEmbedder,
        "_spawn_worker_process",
        lambda self: next(pairs),
    )

    with worker_mod.ProcessSbertEmbedder(
        model_name="test-model", workers=1, query_prefix="query:"
    ) as emb:
        encode = getattr(emb, method)
        with pytest.raises(failure):
            encode(["tenant-a:slow"])

        for proc, conn in failed:
            assert conn.closed
            assert proc.terminated == 1
            assert proc.join_calls
            assert conn._responses == [stale]
        assert emb._slots[0].conn is None
        assert emb._slots[0].proc is None
        assert fresh_conn.sent == []

        out = encode(["tenant-b:first", "tenant-b:second"])
        assert out.dtype == np.float32
        np.testing.assert_array_equal(out, [[1, 2, 3], [4, 5, 6]])
        np.testing.assert_array_equal(encode(["tenant-a:next"]), [[7, 8, 9]])
        prefix = "query:" if method == "encode_queries" else ""
        assert fresh_conn.sent == [
            ("encode", [prefix + "tenant-b:first", prefix + "tenant-b:second"]),
            ("encode", [prefix + "tenant-a:next"]),
        ]

    assert fresh_conn.closed
    assert fresh_proc.terminated == 1


@pytest.mark.parametrize("response", [
    None,
    ("ok",),
    ("unexpected", "payload"),
    ("ok", [1.0, 2.0, 3.0]),
    ("ok", [[1.0, 2.0]]),
    ("ok", [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]),
    ("ok", [["invalid", 2.0, 3.0]]),
])
def test_invalid_response_retires_worker(monkeypatch, response):
    bad_proc = _FakeProc()
    bad_conn = _FakeConn([("ready", 3), response, ("ok", [[9, 9, 9]])])
    good_conn = _FakeConn([("ready", 3), ("ok", [[1, 2, 3]])])
    pairs = iter([(bad_proc, bad_conn), (_FakeProc(), good_conn)])
    monkeypatch.setattr(
        worker_mod.ProcessSbertEmbedder,
        "_spawn_worker_process",
        lambda self: next(pairs),
    )

    with worker_mod.ProcessSbertEmbedder(model_name="test-model", workers=1) as emb:
        with pytest.raises(RuntimeError, match="invalid response"):
            emb.encode(["first"])
        assert bad_conn.closed
        assert bad_proc.terminated == 1
        np.testing.assert_array_equal(emb.encode(["second"]), [[1, 2, 3]])
        assert good_conn.sent == [("encode", ["second"])]


def test_worker_error_response_keeps_pipe_synchronized(monkeypatch):
    proc = _FakeProc()
    conn = _FakeConn([
        ("ready", 3), ("error", "inference failed"), ("ok", [[1, 2, 3]])
    ])
    monkeypatch.setattr(
        worker_mod.ProcessSbertEmbedder,
        "_spawn_worker_process",
        lambda self: (proc, conn),
    )

    with worker_mod.ProcessSbertEmbedder(model_name="test-model", workers=1) as emb:
        with pytest.raises(RuntimeError, match="encode failed: inference failed"):
            emb.encode(["first"])
        assert not conn.closed
        assert proc.terminated == 0
        np.testing.assert_array_equal(emb.encode(["second"]), [[1, 2, 3]])


def test_process_sbert_distributes_encode_calls_across_workers(monkeypatch):
    factory = importlib.reload(worker_mod)

    first_proc = _FakeProc()
    first_conn = _FakeConn(
        [
            ("ready", 3),
            ("ok", np.array([[1.0, 0.0, 0.0]], dtype=np.float32)),
            ("ok", np.array([[3.0, 0.0, 0.0]], dtype=np.float32)),
        ]
    )
    second_proc = _FakeProc()
    second_conn = _FakeConn(
        [
            ("ready", 3),
            ("ok", np.array([[2.0, 0.0, 0.0]], dtype=np.float32)),
            ("ok", np.array([[4.0, 0.0, 0.0]], dtype=np.float32)),
        ]
    )
    pairs = [(first_proc, first_conn), (second_proc, second_conn)]
    spawned: list[tuple[_FakeProc, _FakeConn]] = []

    def fake_spawn(self):
        pair = pairs[len(spawned)]
        spawned.append(pair)
        return pair

    monkeypatch.setattr(
        factory.ProcessSbertEmbedder,
        "_spawn_worker_process",
        fake_spawn,
        raising=True,
    )

    emb = factory.ProcessSbertEmbedder(
        model_name="sentence-transformers/test-model",
        device="cpu",
        batch_size=8,
        workers=2,
    )

    assert len(spawned) == 1
    assert emb.encode(["a"])[0, 0] == 1.0
    with emb._slots[0].lock:
        assert emb.encode(["b"])[0, 0] == 2.0
    assert len(spawned) == 2
    assert emb.encode(["c"])[0, 0] == 3.0
    assert emb.encode(["d"])[0, 0] == 4.0
    assert first_conn.sent == [("encode", ["a"]), ("encode", ["c"])]
    assert second_conn.sent == [("encode", ["b"]), ("encode", ["d"])]

    emb.close()


def test_process_sbert_warm_starts_and_encodes_all_workers(monkeypatch):
    factory = importlib.reload(worker_mod)

    pairs = [
        (
            _FakeProc(),
            _FakeConn([
                ("ready", 3),
                ("ok", np.array([[float(index), 0.0, 0.0]], dtype=np.float32)),
            ]),
        )
        for index in range(3)
    ]
    spawned: list[tuple[_FakeProc, _FakeConn]] = []
    spawn_lock = Lock()

    def fake_spawn(self):
        with spawn_lock:
            pair = pairs[len(spawned)]
            spawned.append(pair)
            return pair

    monkeypatch.setattr(
        factory.ProcessSbertEmbedder,
        "_spawn_worker_process",
        fake_spawn,
        raising=True,
    )

    emb = factory.ProcessSbertEmbedder(
        model_name="sentence-transformers/test-model",
        device="cpu",
        batch_size=8,
        workers=3,
    )

    assert len(spawned) == 1
    emb.warm()

    assert len(spawned) == 3
    for _proc, conn in pairs:
        assert conn.sent == [("encode", ["warmup"])]

    emb.close()
