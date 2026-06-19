# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Text embedders.

An embedder turns a batch of texts into a `(N, dim)` matrix of
float32 vectors. It owns model lifecycle (loading, warmup) and
batch encoding; it knows nothing about collections, tenants, or
vector storage.

Each embedder ships its own `CONFIG_KEYS` / `SPACE_KEYS` tuples so
the factory in `pave.embedders.factory` can resolve, validate, and
identify vector-space compatibility without a central registry of
fields. See `docs/reference/protocols.md` for the frozen v1.0
contract.
"""

from .base import Embedder, EmbedderConfig, EmbedderUnavailableError
from .factory import LazyEmbedder, get_embedder

__all__ = [
    "Embedder",
    "EmbedderConfig",
    "EmbedderUnavailableError",
    "LazyEmbedder",
    "get_embedder",
]
