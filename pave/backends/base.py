# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Vector-backend protocol.

Defines `VectorBackend`, the runtime-checkable shape every backend
implementation honors. The frozen v1.0 contract — per-method
semantics, ID stability, persistence guarantees — lives in
`docs/reference/protocols.md`.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Protocol, runtime_checkable

import numpy as np
from numpy.typing import NDArray


SearchHit = tuple[str, float]


class VectorBackend(Protocol):
    """Frozen v1.0 vector-backend seam.

    Stores embedding vectors keyed by stable string record IDs and
    serves top-k similarity queries. Persistence under a storage
    directory is mandatory; concurrency is the caller's
    responsibility. See `docs/reference/protocols.md` for the full
    contract.
    """
    def initialize(self) -> None: ...

    def add(self, rids: list[str], vectors: NDArray[np.float32]) -> None: ...

    def search(
        self,
        vector: NDArray[np.float32],
        k: int,
    ) -> list[SearchHit]: ...

    def delete(self, rids: list[str]) -> None: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class FilteredSearchBackend(Protocol):
    """Optional backend capability for an allowed-record search.

    This stays separate from the frozen ``VectorBackend`` contract. Backends
    opt in when they can score only the supplied record IDs without building
    a request-local index.
    """

    def search_filtered(
        self,
        vector: NDArray[np.float32],
        k: int,
        allowed_rids: Collection[str],
    ) -> list[SearchHit]: ...
