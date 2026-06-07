# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import multiprocessing as mp
import traceback
from contextlib import suppress
from threading import Lock
from typing import Any

import numpy as np
from numpy.typing import NDArray

from ..config import CFG

_STARTUP_TIMEOUT_S = 300.0
_REQUEST_TIMEOUT_S = 300.0


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
            dim = int(model.get_sentence_embedding_dimension())
        except Exception:
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
    ) -> None:
        self.model_name = model_name or CFG.get(
            "embedder.sbert.model",
            "sentence-transformers/all-MiniLM-L6-v2",
        )
        self.device = str(
            device
            if device is not None
            else CFG.get("embedder.sbert.device", "auto")
        )
        self.batch_size = int(
            batch_size
            if batch_size is not None
            else CFG.get("embedder.sbert.batch_size", 64)
        )
        self._lock = Lock()
        self._proc: mp.Process | None = None
        self._conn: Any = None
        self._dim: int | None = None
        self._start_locked()

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

    def _shutdown_locked(self) -> None:
        proc = self._proc
        conn = self._conn
        self._proc = None
        self._conn = None

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

    def _start_locked(self) -> None:
        if self._proc is not None and self._conn is not None and self._proc.is_alive():
            return

        self._shutdown_locked()
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

        dim = int(payload)
        if self._dim is not None and self._dim != dim:
            with suppress(Exception):
                conn.close()
            with suppress(Exception):
                proc.terminate()
                proc.join(timeout=2)
            raise RuntimeError(
                "SBERT worker reported a different embedding dimension "
                f"after restart: {dim} != {self._dim}"
            )

        self._proc = proc
        self._conn = conn
        self._dim = dim

    def _restart_locked(self) -> None:
        self._shutdown_locked()
        self._start_locked()

    def _ensure_worker_locked(self) -> None:
        if self._proc is None or self._conn is None or not self._proc.is_alive():
            self._start_locked()

    @property
    def dim(self) -> int:
        with self._lock:
            if self._dim is None:
                self._start_locked()
            assert self._dim is not None
            return int(self._dim)

    def encode(self, texts: list[str]) -> NDArray[np.float32]:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)

        with self._lock:
            for attempt in range(2):
                self._ensure_worker_locked()
                assert self._conn is not None
                assert self._proc is not None
                try:
                    self._conn.send(("encode", texts))
                    if not self._conn.poll(_REQUEST_TIMEOUT_S):
                        raise TimeoutError(
                            "SBERT worker did not answer within "
                            f"{_REQUEST_TIMEOUT_S:.0f}s"
                        )
                    status, payload = self._conn.recv()
                except (BrokenPipeError, EOFError, OSError, TimeoutError):
                    if attempt == 0:
                        self._restart_locked()
                        continue
                    raise

                if status == "ok":
                    return np.asarray(payload, dtype=np.float32)
                if status == "error":
                    raise RuntimeError(f"SBERT worker encode failed: {payload}")
                raise RuntimeError(f"SBERT worker returned unknown status: {status}")

    def close(self) -> None:
        with self._lock:
            self._shutdown_locked()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
