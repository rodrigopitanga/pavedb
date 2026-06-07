# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import importlib

import numpy as np

import pave.embedders.sbert_worker as worker_mod


class _FakeConn:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.sent: list[object] = []
        self.closed = False

    def send(self, msg) -> None:
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

    def close(self) -> None:
        self.closed = True


class _FakeProc:
    def __init__(self) -> None:
        self.alive = True
        self.join_calls: list[float | None] = []
        self.terminated = 0

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.terminated += 1
        self.alive = False

    def join(self, timeout=None) -> None:
        self.join_calls.append(timeout)


def test_process_sbert_restarts_after_connection_drop(monkeypatch) -> None:
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
