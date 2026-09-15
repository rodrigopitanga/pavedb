# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

from pave.embedders.worker_pool import (
    ThreadEmbedderPool,
    resolve_worker_count,
)


class _FakeEmbedder:
    def __init__(self, name: str, barrier: Barrier | None = None) -> None:
        self.name = name
        self.barrier = barrier
        self.calls: list[tuple[str, list[str]]] = []
        self.closed = False

    @property
    def dim(self) -> int:
        return 3

    def encode(self, texts: list[str]):
        self.calls.append(("encode", texts))
        if self.barrier is not None:
            self.barrier.wait(timeout=5)
        return self.name

    def warm(self) -> None:
        self.calls.append(("warm", []))

    def close(self) -> None:
        self.closed = True


def test_auto_worker_count_is_conservatively_bounded(monkeypatch):
    monkeypatch.setattr("pave.embedders.worker_pool.os.cpu_count", lambda: 32)

    assert resolve_worker_count(None) == 4
    assert resolve_worker_count("auto") == 4
    assert resolve_worker_count(2) == 2
    assert resolve_worker_count(0) == 1


def test_pool_clones_lazily_and_routes_round_robin():
    first = _FakeEmbedder("first")
    built: list[_FakeEmbedder] = []

    def build():
        embedder = _FakeEmbedder(f"clone-{len(built)}")
        built.append(embedder)
        return embedder

    pool = ThreadEmbedderPool(
        build,
        workers=2,
        initial_embedder=first,
    )

    assert pool.encode(["a"]) == "first"
    assert pool.encode(["b"]) == "clone-0"
    assert pool.encode(["c"]) == "first"
    assert len(built) == 1


def test_pool_uses_distinct_workers_for_concurrent_calls():
    barrier = Barrier(2)
    first = _FakeEmbedder("first", barrier)
    built: list[_FakeEmbedder] = []
    build_lock = Lock()

    def build():
        with build_lock:
            embedder = _FakeEmbedder(f"clone-{len(built)}", barrier)
            built.append(embedder)
            return embedder

    pool = ThreadEmbedderPool(
        build,
        workers=2,
        initial_embedder=first,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(pool.encode, [["a"], ["b"]]))

    assert set(results) == {"first", "clone-0"}
    assert len(built) == 1


def test_pool_warms_and_closes_every_worker():
    first = _FakeEmbedder("first")
    built: list[_FakeEmbedder] = []

    def build():
        embedder = _FakeEmbedder(f"clone-{len(built)}")
        built.append(embedder)
        return embedder

    pool = ThreadEmbedderPool(
        build,
        workers=3,
        initial_embedder=first,
    )
    pool.warm()
    pool.close()
    pool.close()

    assert len(built) == 2
    assert all(worker.calls == [("warm", [])] for worker in [first, *built])
    assert all(worker.closed for worker in [first, *built])


def test_pool_falls_back_to_encode_for_query_calls():
    first = _FakeEmbedder("first")
    pool = ThreadEmbedderPool(
        lambda: _FakeEmbedder("clone"),
        workers=1,
        initial_embedder=first,
    )

    assert pool.encode_queries(["q"]) == "first"
    assert first.calls == [("encode", ["q"])]
