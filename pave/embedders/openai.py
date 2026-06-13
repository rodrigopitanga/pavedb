# (C) 2025 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations
import os

import numpy as np

from ..config import CFG


CONFIG_KEYS: tuple[str, ...] = ("dim", "base_url", "batch_size")
SPACE_KEYS: tuple[str, ...] = ("dim",)


class OpenAIEmbedder:
    def __init__(
        self,
        *,
        model: str | None = None,
        batch_size: int | None = None,
        dim: int | str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        from openai import OpenAI

        self.model = str(
            model if model is not None else CFG.get("embedder.openai.model")
            or ""
        ).strip()
        if not self.model:
            raise RuntimeError("embedder.openai.model must be configured")
        self.batch_size = int(
            batch_size
            if batch_size is not None
            else CFG.get("embedder.openai.batch_size", 256)
        )
        self._dim = dim if dim is not None else CFG.get("embedder.openai.dim")
        resolved_key = (
            api_key
            or CFG.get("embedder.openai.api_key")
            or os.environ.get("OPENAI_API_KEY")
        )
        if not resolved_key:
            raise RuntimeError("OpenAI API key not configured")
        client_kwargs = {"api_key": resolved_key}
        resolved_base_url = str(
            base_url
            if base_url is not None
            else CFG.get("embedder.openai.base_url", "")
            or ""
        ).strip()
        if resolved_base_url:
            client_kwargs["base_url"] = resolved_base_url
        self.client = OpenAI(**client_kwargs)

    @property
    def dim(self) -> int:
        try:
            if self._dim is None:
                raise RuntimeError(
                    "embedder.openai.dim must be configured for OpenAIEmbedder"
                )
            return int(self._dim)
        except Exception:
            raise RuntimeError(
                "embedder.openai.dim must be configured for OpenAIEmbedder"
            )

    def encode(self, texts: list[str]) -> np.ndarray:
        kwargs = {"model": self.model, "input": texts}
        if self._dim is not None:
            kwargs["dimensions"] = int(self._dim)
        res = self.client.embeddings.create(**kwargs)
        return np.array([d.embedding for d in res.data], dtype=np.float32)
