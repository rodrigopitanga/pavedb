# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Direct coverage of the real identity (BYOV) embedder.

The API-path tests run under conftest's `FakeEmbedder` monkeypatch, so they
never touch the real class. These exercise it directly: factory resolution,
float32 pass-through, dim-from-config, and the no-op `warm()` that keeps
`LocalStore.warm` from trying to encode the literal "warmup" string.
"""

import numpy as np
import pytest

from pave.config import Config
from pave.embedders.factory import _is_modelless, resolve_embedder_spec
from pave.embedders.identity import (
    IdentityEmbedder,
    build_embedder,
    validate_spec,
)


def test_factory_resolves_identity_from_config():
    spec = resolve_embedder_spec(
        Config(data={}),
        embedder_type="identity",
        embedder_config={"dim": 8},
    )
    assert spec["embedder_type"] == "identity"
    assert (spec.get("embedder_config") or {})["dim"] == 8
    assert _is_modelless("identity")


def test_encode_is_float32_passthrough():
    emb = build_embedder(Config(data={}), embedder_config={"dim": 3})
    assert emb.dim == 3
    out = emb.encode([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
    assert out.dtype == np.float32
    np.testing.assert_array_equal(
        out, np.array([[1, 2, 3], [4, 5, 6]], dtype=np.float32)
    )


def test_warm_is_noop():
    # Must not raise and must not try to encode text.
    assert IdentityEmbedder(dim=4).warm() is None


def test_validate_spec_rejects_bad_dim():
    assert validate_spec({"dim": "5"})["dim"] == 5
    for bad in ({}, {"dim": 0}, {"dim": -1}, {"dim": "x"}):
        with pytest.raises(ValueError):
            validate_spec(bad)


def test_missing_dim_is_rejected():
    with pytest.raises(RuntimeError):
        IdentityEmbedder(dim=None)
