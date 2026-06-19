# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Vector backends.

A vector backend owns the ANN index for one collection: adding
vectors with stable record IDs, returning top-k neighbours for a
query vector, and persisting / loading its own state under a
storage directory. It knows nothing about embedders, tenants, or
metadata filters — those are upstream concerns owned by the store.

`VectorBackend` is the frozen v1.0 seam (see
`docs/reference/protocols.md`); `FaissBackend` is the in-tree
implementation; `QdrantVectorBackend` is a stub kept for shape.
"""

from .base import SearchHit, VectorBackend
from .faiss import FaissBackend
from .qdrant import QdrantVectorBackend

__all__ = [
    "FaissBackend",
    "QdrantVectorBackend",
    "SearchHit",
    "VectorBackend",
]
