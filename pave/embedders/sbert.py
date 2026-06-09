# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import numpy as np
import torch
from numpy.typing import NDArray
from sentence_transformers import SentenceTransformer

from ..config import CFG


class SbertEmbedder:
    @staticmethod
    def _resolve_device(raw_device: object) -> str:
        device = str(raw_device or "auto").strip().lower()
        if device != "auto":
            return device
        if torch.cuda.is_available():
            return "cuda"
        mps = getattr(torch.backends, "mps", None)
        if mps is not None and mps.is_available():
            return "mps"
        return "cpu"

    def __init__(
        self,
        *,
        model_name: str | None = None,
        device: str | None = None,
        batch_size: int | None = None,
    ) -> None:
        model_name = model_name or CFG.get(
            "embedder.sbert.model",
            "sentence-transformers/all-MiniLM-L6-v2",
        )
        device = self._resolve_device(
            device if device is not None else CFG.get(
                "embedder.sbert.device",
                "auto",
            )
        )
        self.batch_size = int(
            batch_size
            if batch_size is not None
            else CFG.get("embedder.sbert.batch_size", 64)
        )
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
