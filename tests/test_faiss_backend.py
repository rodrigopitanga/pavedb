# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import numpy as np
import pytest

from pave.backends.faiss import FaissBackend


def _v(*rows: list[float]) -> np.ndarray:
    return np.array(rows, dtype=np.float32)


def test_search_empty_index_returns_empty(tmp_path):
    backend = FaissBackend(4, storage_dir=tmp_path / "idx")
    hits = backend.search(_v([1, 0, 0, 0])[0], 5)
    assert hits == []


def test_add_search_delete_roundtrip(tmp_path):
    backend = FaissBackend(4, storage_dir=tmp_path / "idx")
    backend.add(["r1", "r2"], _v([1, 0, 0, 0], [0, 1, 0, 0]))

    hits = backend.search(_v([0.8, 0.2, 0, 0])[0], 5)
    assert [rid for rid, _ in hits] == ["r1", "r2"]

    backend.delete(["r1"])
    hits_after = backend.search(_v([1, 0, 0, 0])[0], 5)
    assert [rid for rid, _ in hits_after] == ["r2"]


def test_add_reindex_keeps_unique_rids(tmp_path):
    backend = FaissBackend(4, storage_dir=tmp_path / "idx")
    backend.add(["same", "other"], _v([1, 0, 0, 0], [0, 0, 1, 0]))
    assert int(backend._index.ntotal) == 2

    backend.add(["same"], _v([0, 1, 0, 0]))
    assert int(backend._index.ntotal) == 2

    hits = backend.search(_v([0, 1, 0, 0])[0], 5)
    assert hits[0][0] == "same"


def test_add_does_not_mutate_input_vectors(tmp_path):
    backend = FaissBackend(3, storage_dir=tmp_path / "idx")
    vectors = _v([3, 4, 0], [0, 5, 12])
    before = vectors.copy()

    backend.add(["a", "b"], vectors)
    assert np.array_equal(vectors, before)


def test_flush_and_initialize_restore_index_state(tmp_path):
    storage = tmp_path / "idx"

    b1 = FaissBackend(4, storage_dir=storage)
    b1.add(["r1", "r2"], _v([1, 0, 0, 0], [0, 1, 0, 0]))
    b1.flush()

    assert (storage / "faiss.index").is_file()
    assert (storage / "id_map.json").is_file()

    b2 = FaissBackend(4, storage_dir=storage)
    b2.initialize()

    hits = b2.search(_v([1, 0, 0, 0])[0], 10)
    assert [rid for rid, _ in hits] == ["r1", "r2"]

    b2.add(["r3"], _v([0, 0, 1, 0]))
    hits2 = b2.search(_v([0, 0, 1, 0])[0], 10)
    assert hits2[0][0] == "r3"


def test_initialize_raises_on_dimension_mismatch(tmp_path):
    storage = tmp_path / "idx"
    b1 = FaissBackend(4, storage_dir=storage)
    b1.add(["r1"], _v([1, 0, 0, 0]))
    b1.flush()

    b2 = FaissBackend(8, storage_dir=storage)
    with pytest.raises(ValueError, match="dimension mismatch"):
        b2.initialize()
