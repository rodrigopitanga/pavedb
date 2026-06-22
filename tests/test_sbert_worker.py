# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import importlib
from threading import Lock

import numpy as np

import pave.embedders.sbert_worker as worker_mod


class _FakeConn:
    def __init__(self, responses: list[object]):
        self._responses = list(responses)
        self.sent: list[object] = []
        self.closed = False

    def send(self, msg):
        self.sent.append(msg)

    def poll(self, timeout=None) -> bool:
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
