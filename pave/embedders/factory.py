# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from collections.abc import Callable
import sys
from threading import Lock

from .base import Embedder
from ..config import CFG


class LazyEmbedder:
    def __init__(self, factory: Callable[[], Embedder]) -> None:
        self._factory = factory
        self._embedder: Embedder | None = None
        self._lock = Lock()

    def _get(self) -> Embedder:
        if self._embedder is None:
            with self._lock:
                if self._embedder is None:
                    self._embedder = self._factory()
        return self._embedder

    @property
    def dim(self) -> int:
        return self._get().dim

    def encode(self, texts: list[str]):
        return self._get().encode(texts)


def get_embedder(cfg: CFG = CFG) -> Embedder:
    etype = (cfg.get("embedder.type", "sbert") or "sbert").lower()
    match etype:
        case "sbert":
            runtime = str(
                cfg.get(
                    "embedder.sbert.runtime",
                    cfg.get("embedder.sbert.mode", "auto"),
                )
                or "auto"
            ).lower()
            backend_type = str(cfg.get("vector_store.type", "faiss")).lower()
            if runtime == "process":
                from .sbert_worker import ProcessSbertEmbedder

                return ProcessSbertEmbedder()
            if runtime in {"direct", "none"}:
                from .sbert import SbertEmbedder

                return SbertEmbedder()
            if runtime != "auto":
                raise RuntimeError(f"Unknown embedder.sbert.runtime: {runtime}")
            if sys.platform == "darwin" and backend_type == "faiss":
                from .sbert_worker import ProcessSbertEmbedder

                return ProcessSbertEmbedder()
            from .sbert import SbertEmbedder

            return SbertEmbedder()
        case "openai":
            from .openai import OpenAIEmbedder

            return OpenAIEmbedder()
        case _:
            raise RuntimeError(f"Unknown embedder.type: {etype}")
