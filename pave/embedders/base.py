# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Embedder protocol.

Defines `Embedder`, the runtime-checkable shape every embedder
honors, plus the `EmbedderConfig` alias and the
`EmbedderUnavailableError` sentinel. The frozen v1.0 contract
lives in `docs/reference/protocols.md`.
"""

from __future__ import annotations

from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray


EmbedderConfig = dict[str, Any]


class Embedder(Protocol):
    """Frozen v1.0 embedder seam.

    Implementations encode batches of text into `(N, dim)` float32
    matrices and expose a stable `dim` property. They own model
    lifecycle and are responsible for raising
    `EmbedderUnavailableError` from `encode` or `dim` when the
    underlying model / API is unreachable.
    """
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
