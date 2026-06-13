# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from typing import Protocol

import numpy as np
from numpy.typing import NDArray


class Embedder(Protocol):
    def encode(self, texts: list[str]) -> NDArray[np.float32]:
        """(N, dim) matrix of embedding vectors."""

    @property
    def dim(self) -> int:
        """Embedding dimensionality (e.g. 384)."""


class EmbedderUnavailableError(RuntimeError):
    """Raised when the embedder for a collection cannot be instantiated.

    Distinct from a search/ingest bug: signals a missing dependency
    (model file, API key, network), so the service layer can surface a
    dedicated `embedder_unavailable` error code instead of wrapping in
    the generic failure envelope.
    """


BaseEmbedder = Embedder  # deprecated alias
