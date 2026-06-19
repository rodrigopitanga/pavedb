# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Qdrant backend stub.

Placeholder kept to preserve the `VectorBackend` shape across
remote backends. No production code path constructs this class.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from .base import SearchHit


class QdrantVectorBackend:
    """Stub for Qdrant vector backend. Not yet implemented."""

    def __init__(
        self,
        *,
        url: str,
        collection: str,
        api_key: str | None = None,
    ) -> None:
        self._url = url
        self._collection = collection
        self._api_key = api_key

    def initialize(self) -> None:
        raise NotImplementedError("QdrantVectorBackend")

    def add(self, rids: list[str], vectors: NDArray[np.float32]) -> None:
        raise NotImplementedError("QdrantVectorBackend")

    def search(
        self,
        vector: NDArray[np.float32],
        k: int,
    ) -> list[SearchHit]:
        raise NotImplementedError("QdrantVectorBackend")

    def delete(self, rids: list[str]) -> None:
        raise NotImplementedError("QdrantVectorBackend")

    def flush(self) -> None:
        raise NotImplementedError("QdrantVectorBackend")

    def close(self) -> None:
        raise NotImplementedError("QdrantVectorBackend")
