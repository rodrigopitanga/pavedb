# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Identity (raw-vector / BYOV) embedder.

The anchor for bring-your-own-vector collections: it owns no model and
never calls out to encode text. `dim` rides in `embedder_config:{"dim":N}`
at create time, so the FAISS index and `vector_space_key` are established
from the declared dimensionality alone. On the hot path the store skips
the embed step entirely and hands raw vectors straight to the backend;
`encode` exists only to honor the `Embedder` seam as a float32 pass-through.
"""

from __future__ import annotations

import numpy as np


CONFIG_KEYS: tuple[str, ...] = ("dim",)
SPACE_KEYS: tuple[str, ...] = ("dim",)
# Identity owns no model; the vector space is pinned by `dim` alone. Signals
# the factory to skip the "no model configured" requirement for this type.
MODELLESS: bool = True


def validate_spec(resolved_config: dict) -> dict:
    """Enforce identity-specific config invariants at create-collection time.

    Called by `factory.resolve_embedder_spec`; coerces `dim` to a positive
    int so the catalog row and FAISS index carry the declared dimensionality.
    """
    raw_dim = resolved_config.get("dim")
    if raw_dim in (None, ""):
        raise ValueError("embedder.identity.dim must be configured")
    try:
        dim = int(raw_dim)
    except (TypeError, ValueError) as exc:
        raise ValueError("embedder.identity.dim must be an integer") from exc
    if dim <= 0:
        raise ValueError("embedder.identity.dim must be positive")
    resolved_config["dim"] = dim
    return resolved_config


def build_embedder(cfg, *, model=None, embedder_config=None, api_key=None):
    """Construct an IdentityEmbedder from the collection's `dim`.

    `model` and `api_key` are accepted for factory-call symmetry but
    unused: identity owns no model. `dim` is read straight from the
    (already validated) embedder_config.
    """
    spec = dict(embedder_config or {})
    return IdentityEmbedder(dim=spec.get("dim"))


class IdentityEmbedder:
    """Raw-vector `Embedder`: no model, no network, `encode(x) → x`.

    Established so BYOV collections have a stable `dim` and vector space
    without a text model. `warm()` is an explicit no-op so `LazyEmbedder.warm`
    takes the warm()-branch instead of encoding the literal "warmup" string.
    """
    def __init__(self, *, dim: int | str | None = None) -> None:
        if dim in (None, ""):
            raise RuntimeError(
                "embedder.identity.dim must be configured for IdentityEmbedder"
            )
        self._dim = int(dim)

    @property
    def dim(self) -> int:
        return self._dim

    def warm(self) -> None:
        """No-op: nothing to load, and there is no text to encode."""

    def encode(self, vectors) -> np.ndarray:
        return np.asarray(vectors, dtype=np.float32)
