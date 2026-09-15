# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bounded worker pools for clone-safe embedders.

Remote embedder clients are cheap to clone but should not receive an
unbounded number of concurrent calls. ``ThreadEmbedderPool`` keeps one
client per slot and lends slots to caller threads in round-robin order.
The wrapped objects still expose the frozen ``Embedder`` protocol.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from queue import Queue
from threading import Lock

from .base import Embedder


_MAX_AUTO_WORKERS = 4


def _close_embedder(embedder: Embedder | None) -> None:
    close = getattr(embedder, "close", None)
    if callable(close):
        with suppress(Exception):
            close()


def resolve_worker_count(
    raw_workers: object,
    *,
    max_auto_workers: int = _MAX_AUTO_WORKERS,
    auto_cpu_divisor: int = 1,
    available_cpus: int | None = None,
) -> int:
    """Resolve an explicit or conservative automatic worker count."""
    if raw_workers is None:
        raw_workers = "auto"
    value = str(raw_workers).strip().lower()
    if value in {"", "auto"}:
        cpus = available_cpus or os.cpu_count() or 1
        auto_workers = cpus // max(1, auto_cpu_divisor) or 1
        return max(1, min(max_auto_workers, auto_workers))
    return max(1, int(raw_workers))


class ThreadEmbedderPool:
    """Run clone-safe embedders behind a bounded round-robin pool.

    Calls execute on their existing request threads. A slot is exclusively
    leased for each call, so at most ``workers`` calls reach the provider at
    once and no client instance is used concurrently. Additional callers
    wait for the next available slot.
    """

    def __init__(
        self,
        factory: Callable[[], Embedder],
        *,
        workers: object = "auto",
        initial_embedder: Embedder | None = None,
    ) -> None:
        try:
            self.workers = resolve_worker_count(workers)
        except Exception:
            _close_embedder(initial_embedder)
            raise
        self._factory = factory
        self._embedders: list[Embedder | None] = [None] * self.workers
        self._embedders[0] = initial_embedder
        self._available: Queue[int] = Queue(maxsize=self.workers)
        for index in range(self.workers):
            self._available.put(index)
        self._state_lock = Lock()
        self._closed = False

    def __enter__(self) -> "ThreadEmbedderPool":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def _embedder_for_slot(self, index: int) -> Embedder:
        embedder = self._embedders[index]
        if embedder is None:
            embedder = self._factory()
            self._embedders[index] = embedder
        return embedder

    def _assert_open(self) -> None:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("embedder worker pool is closed")

    def _call(self, method: str, *args):
        self._assert_open()
        index = self._available.get()
        try:
            self._assert_open()
            embedder = self._embedder_for_slot(index)
            call = getattr(embedder, method, None)
            if call is None and method == "encode_queries":
                call = embedder.encode
            if not callable(call):
                raise AttributeError(
                    f"embedder does not provide callable {method}"
                )
            return call(*args)
        finally:
            self._available.put(index)

    @property
    def dim(self) -> int:
        self._assert_open()
        index = self._available.get()
        try:
            self._assert_open()
            return int(self._embedder_for_slot(index).dim)
        finally:
            self._available.put(index)

    def encode(self, texts: list[str]):
        return self._call("encode", texts)

    def encode_queries(self, texts: list[str]):
        """Route the optional query-side capability through the same pool."""
        return self._call("encode_queries", texts)

    def warm(self) -> None:
        """Construct and warm every worker before returning."""
        self._assert_open()
        indices = [self._available.get() for _ in range(self.workers)]
        try:
            def warm_slot(index: int) -> None:
                embedder = self._embedder_for_slot(index)
                warm = getattr(embedder, "warm", None)
                if callable(warm):
                    warm()
                else:
                    embedder.encode(["warmup"])

            with ThreadPoolExecutor(max_workers=self.workers) as executor:
                futures = [executor.submit(warm_slot, index) for index in indices]
                for future in futures:
                    future.result()
        finally:
            for index in indices:
                self._available.put(index)

    def close(self) -> None:
        """Close every constructed worker once."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            embedders = list(self._embedders)
        for embedder in embedders:
            _close_embedder(embedder)
