# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from ..config import CFG


CONFIG_KEYS: tuple[str, ...] = ("runtime", "device", "batch_size")
SPACE_KEYS: tuple[str, ...] = ()


class SbertEmbedder:
    def __init__(
        self,
        *,
        model_name: str | None = None,
        device: str | None = None,
        batch_size: int | None = None,
    ) -> None:
        import torch
        from sentence_transformers import SentenceTransformer

        model_name = str(
            model_name if model_name is not None
            else CFG.get("embedder.sbert.model")
            or ""
        ).strip()
        if not model_name:
            raise RuntimeError("embedder.sbert.model must be configured")
        device = self._resolve_device(
            torch,
            device if device is not None else CFG.get(
                "embedder.sbert.device",
                "auto",
            ),
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
