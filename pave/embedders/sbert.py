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


CONFIG_KEYS: tuple[str, ...] = ("runtime", "device", "batch_size")
SPACE_KEYS: tuple[str, ...] = ()


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
    backend_type = str(cfg.get("vector_store.type")).lower()

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

    kwargs = {
        "model_name": model_name,
        "device": None if device is None else str(device),
        "batch_size": None if batch_size is None else int(batch_size),
    }
    if runtime == "process":
        from .sbert_worker import ProcessSbertEmbedder
        return ProcessSbertEmbedder(**kwargs)
    return SbertEmbedder(**kwargs)


class SbertEmbedder:
    """In-process Sentence-Transformers `Embedder`.

    Loads a local SBERT model and encodes batches with
    `convert_to_numpy=True, normalize_embeddings=True`. `device`
    accepts `"auto"`, `"cpu"`, or any torch device string;
    `batch_size` controls the per-call batching inside ST.
    """
    def __init__(
        self,
        *,
        model_name: str | None = None,
        device: str | None = None,
        batch_size: int | None = None,
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
        if device is None:
            device = cfg.get("embedder.sbert.device")
        device = self._resolve_device(torch, device if device is not None else "auto")
        if batch_size is None:
            batch_size = cfg.get("embedder.sbert.batch_size")
        self.batch_size = int(batch_size if batch_size is not None else 64)
        self.model = SentenceTransformer(model_name, device=device)
        try:
            get_dim = getattr(
                self.model,
                "get_embedding_dimension",
                None,
            ) or getattr(self.model, "get_sentence_embedding_dimension", None)
            self._dim = int(get_dim()) if get_dim is not None else None
        except Exception:
            self._dim = None

    @staticmethod
    def _resolve_device(torch_mod, raw_device: object) -> str:
        device = str(raw_device or "auto").strip().lower()
        if device != "auto":
            return device
        if torch_mod.cuda.is_available():
            return "cuda"
        mps = getattr(torch_mod.backends, "mps", None)
        if mps is not None and mps.is_available():
            return "mps"
        return "cpu"

    @property
    def dim(self) -> int:
        if self._dim is None:
            probe = self.encode(["_"])
            self._dim = int(probe.shape[1])
        return int(self._dim)

    def encode(self, texts: list[str]) -> NDArray[np.float32]:
        vecs = self.model.encode(
            texts,
            batch_size=self.batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return vecs.astype(np.float32)
