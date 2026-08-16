# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Sentence-Transformers embedder.

In-process `SbertEmbedder` (used in tests, library mode, and CPU
servers) plus the subprocess `ProcessSbertEmbedder` (default on
macOS to isolate the torch fork-safety hazard from the API
process). `build_embedder` dispatches on the `runtime` setting.
"""

from __future__ import annotations

import sys

import numpy as np
from numpy.typing import NDArray

from ..config import get_cfg
from ..metrics import inc_labeled
from ._device import resolve_device


CONFIG_KEYS: tuple[str, ...] = (
    "runtime",
    "device",
    "batch_size",
    "workers",
    "trust_remote_code",
    "endpoint",
    "query_prefix",
    "passage_prefix",
)
# Task prefixes change the produced vectors, so they are part of the
# vector-space identity; runtime/device/endpoint/trust_remote_code are
# execution details and are not.
SPACE_KEYS: tuple[str, ...] = ("query_prefix", "passage_prefix")


def _prefix(texts: list[str], prefix: str | None) -> list[str]:
    if not prefix:
        return texts
    return [f"{prefix}{text}" for text in texts]


def build_embedder(cfg, *, model=None, embedder_config=None, api_key=None):
    """Construct an sbert-family embedder; dispatches between in-process
    SbertEmbedder and subprocess ProcessSbertEmbedder per `runtime`.

    `api_key` is accepted for a uniform factory signature and ignored; local
    SBERT models need no credential.
    """
    from .factory import resolve_setting

    spec = dict(embedder_config or {})
    model_name = str(
        model if model is not None else cfg.get("embedder.sbert.model") or ""
    ).strip()
    if not model_name:
        raise RuntimeError("embedder.sbert.model must be configured")

    runtime_value = resolve_setting(cfg, "sbert", "runtime", spec, None)
    if runtime_value is None:
        raise RuntimeError("embedder.sbert.runtime must be configured")
    runtime = str(runtime_value).lower()
    device = resolve_setting(cfg, "sbert", "device", spec, None)
    batch_size = resolve_setting(cfg, "sbert", "batch_size", spec, None)
    workers = resolve_setting(cfg, "sbert", "workers", spec, None)
    trust_remote_code = bool(
        resolve_setting(cfg, "sbert", "trust_remote_code", spec, False)
    )
    endpoint = resolve_setting(cfg, "sbert", "endpoint", spec, None)
    query_prefix = resolve_setting(cfg, "sbert", "query_prefix", spec, None)
    passage_prefix = resolve_setting(
        cfg, "sbert", "passage_prefix", spec, None
    )
    backend_type = str(cfg.get("vector_store.type")).lower()

    if runtime == "remote":
        if not endpoint:
            raise RuntimeError(
                "embedder.sbert.runtime=remote requires embedder.sbert."
                "endpoint (a TEI-compatible /embed server)"
            )
        return RemoteSbertEmbedder(
            endpoint=str(endpoint),
            model_name=model_name,
            query_prefix=None if query_prefix is None else str(query_prefix),
            passage_prefix=(
                None if passage_prefix is None else str(passage_prefix)
            ),
        )

    if runtime == "auto":
        runtime = (
            "process"
            if (sys.platform == "darwin" and backend_type == "faiss")
            else "direct"
        )
    elif runtime == "none":
        runtime = "direct"
    elif runtime not in {"process", "direct"}:
        raise RuntimeError(f"Unknown embedder.sbert.runtime: {runtime}")
    if runtime == "direct" and sys.platform == "darwin" and backend_type == "faiss":
        raise RuntimeError(
            "embedder.sbert.runtime=direct runs torch in-process, which "
            "crashes on macOS alongside the FAISS backend. Use "
            "runtime=auto or runtime=process."
        )

    kwargs = {
        "model_name": model_name,
        "device": None if device is None else str(device),
        "batch_size": None if batch_size is None else int(batch_size),
        "trust_remote_code": trust_remote_code,
        "query_prefix": None if query_prefix is None else str(query_prefix),
        "passage_prefix": (
            None if passage_prefix is None else str(passage_prefix)
        ),
    }
    if runtime == "process":
        from .sbert_worker import ProcessSbertEmbedder
        kwargs["workers"] = workers
        return ProcessSbertEmbedder(**kwargs)
    return SbertEmbedder(**kwargs)


class SbertEmbedder:
    """In-process Sentence-Transformers `Embedder`.

    Loads a local SBERT model and returns raw float32 vectors.
    `device` accepts `"auto"`, `"cpu"`, or any torch device string;
    `batch_size` controls the per-call batching inside ST.
    """
    def __init__(
        self,
        *,
        model_name: str | None = None,
        device: str | None = None,
        batch_size: int | None = None,
        trust_remote_code: bool = False,
        query_prefix: str | None = None,
        passage_prefix: str | None = None,
    ) -> None:
        import torch
        from sentence_transformers import SentenceTransformer

        cfg = get_cfg()
        model_name = str(
            model_name if model_name is not None
            else cfg.get("embedder.sbert.model")
            or ""
        ).strip()
        if not model_name:
            raise RuntimeError("embedder.sbert.model must be configured")
        self.model_name = model_name
        if device is None:
            device = cfg.get("embedder.sbert.device")
        device = resolve_device(device, torch)
        if batch_size is None:
            batch_size = cfg.get("embedder.sbert.batch_size")
        self.batch_size = int(batch_size if batch_size is not None else 64)
        self.query_prefix = query_prefix
        self.passage_prefix = passage_prefix
        # Passed only when enabled so fakes/older ST builds keep working.
        extra = {"trust_remote_code": True} if trust_remote_code else {}
        self.model = SentenceTransformer(model_name, device=device, **extra)
        try:
            get_dim = getattr(
                self.model,
                "get_embedding_dimension",
                None,
            ) or getattr(self.model, "get_sentence_embedding_dimension", None)
            self._dim = int(get_dim()) if get_dim is not None else None
        except Exception:
            self._dim = None

    @property
    def dim(self) -> int:
        if self._dim is None:
            probe = self.encode(["_"])
            self._dim = int(probe.shape[1])
        return int(self._dim)

    def warm(self) -> None:
        self.encode(["warmup"])

    def encode(self, texts: list[str]) -> NDArray[np.float32]:
        vecs = self.model.encode(
            _prefix(texts, self.passage_prefix),
            batch_size=self.batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        inc_labeled("embedder_embeddings_total", {"model": self.model_name})
        inc_labeled(
            "embedder_embeddings_samples_total",
            {"model": self.model_name},
            float(len(texts)),
        )
        return vecs.astype(np.float32)


    def encode_queries(self, texts: list[str]) -> NDArray[np.float32]:
        """Query-side encode: applies `query_prefix` (e5-style models).

        Optional capability discovered via `getattr`; the frozen
        `Embedder` protocol is untouched. With no prefix configured this
        is identical to `encode`.
        """
        if not self.query_prefix:
            return self.encode(texts)
        vecs = self.model.encode(
            _prefix(texts, self.query_prefix),
            batch_size=self.batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        inc_labeled("embedder_embeddings_total", {"model": self.model_name})
        inc_labeled(
            "embedder_embeddings_samples_total",
            {"model": self.model_name},
            float(len(texts)),
        )
        return vecs.astype(np.float32)


class RemoteSbertEmbedder:
    """Sbert served by a remote TEI-compatible endpoint (same model, same
    vector space as the local runtimes).

    The remote server MUST run the identical model in float32 (or accept
    the documented ~1e-4 fp16 drift as within-space noise); the
    `vector_space_key` does not include the endpoint, so numerical
    equivalence with local runtimes is the operator's contract.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        model_name: str,
        query_prefix: str | None = None,
        passage_prefix: str | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model_name = model_name
        self.query_prefix = query_prefix
        self.passage_prefix = passage_prefix
        self.timeout_s = float(timeout_s)
        self._dim: int | None = None

    def _post(self, texts: list[str]) -> NDArray[np.float32]:
        import httpx

        from .base import EmbedderUnavailableError

        try:
            response = httpx.post(
                f"{self.endpoint}/embed",
                json={"inputs": texts},
                timeout=self.timeout_s,
            )
        except httpx.HTTPError as exc:
            raise EmbedderUnavailableError(
                f"remote sbert endpoint unreachable: {exc}"
            ) from exc
        if response.status_code != 200:
            raise EmbedderUnavailableError(
                "remote sbert endpoint returned "
                f"{response.status_code}: {response.text[:200]}"
            )
        vectors = np.asarray(response.json(), dtype=np.float32)
        if vectors.ndim != 2:
            raise EmbedderUnavailableError(
                "remote sbert endpoint returned a non-matrix payload"
            )
        inc_labeled("embedder_embeddings_total", {"model": self.model_name})
        inc_labeled(
            "embedder_embeddings_samples_total",
            {"model": self.model_name},
            float(len(texts)),
        )
        return vectors

    def encode(self, texts: list[str]) -> NDArray[np.float32]:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return self._post(_prefix(texts, self.passage_prefix))

    def encode_queries(self, texts: list[str]) -> NDArray[np.float32]:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return self._post(_prefix(texts, self.query_prefix))

    @property
    def dim(self) -> int:
        if self._dim is None:
            probe = self._post(["_"])
            self._dim = int(probe.shape[1])
        return int(self._dim)

    def warm(self) -> None:
        self.encode(["warmup"])
