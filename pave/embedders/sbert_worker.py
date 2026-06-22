# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Subprocess SBERT worker.

Hosts `SbertEmbedder` in a separate Python process so the API
process never imports `torch` / `sentence-transformers`. Used on
macOS to sidestep the torch fork-safety hazard.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import traceback
from contextlib import suppress
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

import numpy as np
from numpy.typing import NDArray

from ..config import get_cfg

_STARTUP_TIMEOUT_S = 300.0
_REQUEST_TIMEOUT_S = 300.0
_MAX_AUTO_WORKERS = 8


@dataclass
class _WorkerSlot:
    lock: Lock = field(default_factory=Lock)
    proc: mp.Process | None = None
    conn: Any = None


def _resolve_device(raw_device: object) -> str:
    device = str(raw_device or "auto").strip().lower()
    if device != "auto":
        return device

    import torch

    if torch.cuda.is_available():
        return "cuda"
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return "mps"
    return "cpu"


def _worker_main(conn, model_name: str, raw_device: str, batch_size: int) -> None:
    try:
        from sentence_transformers import SentenceTransformer

        device = _resolve_device(raw_device)
        model = SentenceTransformer(model_name, device=device)
        try:
            get_dim = getattr(
                model,
                "get_embedding_dimension",
                None,
            ) or getattr(model, "get_sentence_embedding_dimension", None)
            dim = int(get_dim()) if get_dim is not None else None
        except Exception:
            dim = None
        if dim is None:
            probe = model.encode(
                ["_"],
                batch_size=batch_size,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
            dim = int(probe.shape[1])
        conn.send(("ready", dim))
    except Exception:
        with suppress(Exception):
            conn.send(("error", traceback.format_exc()))
        with suppress(Exception):
            conn.close()
        return

    try:
        while True:
            try:
                msg = conn.recv()
            except EOFError:
                return
            if not msg:
                continue
            op = msg[0]
            if op == "close":
                return
            if op != "encode":
                conn.send(("error", f"unknown worker op: {op}"))
                continue
            texts = msg[1]
            try:
                vecs = model.encode(
                    texts,
                    batch_size=batch_size,
                    show_progress_bar=False,
                    convert_to_numpy=True,
                )
                conn.send(("ok", np.asarray(vecs, dtype=np.float32)))
            except Exception:
                conn.send(("error", traceback.format_exc()))
    finally:
        with suppress(Exception):
            conn.close()


def _resolve_worker_count(raw_workers: object) -> int:
    if raw_workers is None:
        raw_workers = "auto"
    value = str(raw_workers).strip().lower()
    if value in {"", "auto"}:
        cpus = os.cpu_count() or 1
        return max(1, min(_MAX_AUTO_WORKERS, cpus // 2 or 1))
    return max(1, int(raw_workers))


class ProcessSbertEmbedder:
    """Run sentence-transformers in a dedicated subprocess.

    On macOS, PyTorch/sentence-transformers and FAISS can load conflicting
    OpenMP runtimes in the same process. Keeping SBERT out of the API server
    process avoids that native-library collision and lets the worker be
    restarted independently if it dies.
    """

    def __init__(
        self,
        *,
        model_name: str | None = None,
        device: str | None = None,
        batch_size: int | None = None,
        workers: int | None = None,
    ) -> None:
        cfg = get_cfg()
        self.model_name = str(
            model_name if model_name is not None
            else cfg.get("embedder.sbert.model")
            or ""
        ).strip()
        if not self.model_name:
            raise RuntimeError("embedder.sbert.model must be configured")
        if device is None:
            device = cfg.get("embedder.sbert.device")
        self.device = str(device if device is not None else "auto")
        if batch_size is None:
            batch_size = cfg.get("embedder.sbert.batch_size")
        self.batch_size = int(batch_size if batch_size is not None else 64)
        if workers is None:
            workers = cfg.get("embedder.sbert.workers")
        self.workers = _resolve_worker_count(workers)
        self._slots = [_WorkerSlot() for _ in range(self.workers)]
        self._select_lock = Lock()
        self._dim_lock = Lock()
        self._next_slot = 0
        self._dim: int | None = None
        with self._slots[0].lock:
            self._start_slot_locked(self._slots[0])

    def __enter__(self) -> "ProcessSbertEmbedder":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def _spawn_worker_process(self) -> tuple[mp.Process, Any]:
        ctx = mp.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe()
        proc = ctx.Process(
            target=_worker_main,
            args=(child_conn, self.model_name, self.device, self.batch_size),
            daemon=True,
        )
        proc.start()
        child_conn.close()
        return proc, parent_conn

    def _shutdown_slot_locked(self, slot: _WorkerSlot) -> None:
        proc = slot.proc
        conn = slot.conn
        slot.proc = None
        slot.conn = None

        if conn is not None:
            with suppress(Exception):
                conn.send(("close",))
            with suppress(Exception):
                conn.close()

        if proc is not None:
            proc.join(timeout=5)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=2)

    def _record_dim(self, dim: int) -> None:
        with self._dim_lock:
            if self._dim is None:
                self._dim = dim
                return
            if self._dim != dim:
                raise RuntimeError(
                    "SBERT worker reported a different embedding dimension "
                    f"after restart: {dim} != {self._dim}"
                )

    def _start_slot_locked(self, slot: _WorkerSlot) -> None:
        if (
            slot.proc is not None
            and slot.conn is not None
            and slot.proc.is_alive()
        ):
            return

        self._shutdown_slot_locked(slot)
        proc, conn = self._spawn_worker_process()

        try:
            if not conn.poll(_STARTUP_TIMEOUT_S):
                raise TimeoutError(
                    "SBERT worker did not report readiness within "
                    f"{_STARTUP_TIMEOUT_S:.0f}s"
                )
            status, payload = conn.recv()
        except Exception:
            with suppress(Exception):
                conn.close()
            with suppress(Exception):
                proc.terminate()
                proc.join(timeout=2)
            raise

        if status != "ready":
            with suppress(Exception):
                conn.close()
            with suppress(Exception):
                proc.terminate()
                proc.join(timeout=2)
            raise RuntimeError(f"SBERT worker failed to start: {payload}")

        try:
            self._record_dim(int(payload))
        except RuntimeError:
            with suppress(Exception):
                conn.close()
            with suppress(Exception):
                proc.terminate()
                proc.join(timeout=2)
            raise

        slot.proc = proc
        slot.conn = conn

    def _restart_slot_locked(self, slot: _WorkerSlot) -> None:
        self._shutdown_slot_locked(slot)
        self._start_slot_locked(slot)

    def _ensure_slot_locked(self, slot: _WorkerSlot) -> None:
        if slot.proc is None or slot.conn is None or not slot.proc.is_alive():
            self._start_slot_locked(slot)

    @staticmethod
    def _slot_alive(slot: _WorkerSlot) -> bool:
        return (
            slot.proc is not None
            and slot.conn is not None
            and slot.proc.is_alive()
        )

    def _choose_slot(self) -> _WorkerSlot:
        with self._select_lock:
            total = len(self._slots)
            for offset in range(total):
                index = (self._next_slot + offset) % total
                slot = self._slots[index]
                if self._slot_alive(slot) and not slot.lock.locked():
                    self._next_slot = index + 1
                    return slot
            for offset in range(total):
                index = (self._next_slot + offset) % total
                slot = self._slots[index]
                if not self._slot_alive(slot):
                    self._next_slot = index + 1
                    return slot
            slot = self._slots[self._next_slot % total]
            self._next_slot += 1
            return slot

    @property
    def dim(self) -> int:
        with self._dim_lock:
            assert self._dim is not None
            return int(self._dim)

    def encode(self, texts: list[str]) -> NDArray[np.float32]:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)

        slot = self._choose_slot()
        with slot.lock:
            for attempt in range(2):
                self._ensure_slot_locked(slot)
                assert slot.conn is not None
                assert slot.proc is not None
                try:
                    slot.conn.send(("encode", texts))
                    if not slot.conn.poll(_REQUEST_TIMEOUT_S):
                        raise TimeoutError(
                            "SBERT worker did not answer within "
                            f"{_REQUEST_TIMEOUT_S:.0f}s"
                        )
                    status, payload = slot.conn.recv()
                except (BrokenPipeError, EOFError, OSError, TimeoutError):
                    if attempt == 0:
                        self._restart_slot_locked(slot)
                        continue
                    raise

                if status == "ok":
                    return np.asarray(payload, dtype=np.float32)
                if status == "error":
                    raise RuntimeError(f"SBERT worker encode failed: {payload}")
                raise RuntimeError(f"SBERT worker returned unknown status: {status}")

    def close(self) -> None:
        for slot in self._slots:
            with slot.lock:
                self._shutdown_slot_locked(slot)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
