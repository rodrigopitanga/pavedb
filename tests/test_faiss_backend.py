# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import faiss
import numpy as np
import pytest

from pave.backends import FilteredSearchBackend
from pave.backends.faiss import FaissBackend


def _v(*rows: list[float]) -> np.ndarray:
    return np.array(rows, dtype=np.float32)


def test_search_empty_index_returns_empty(tmp_path):
    backend = FaissBackend(4, storage_dir=tmp_path / "idx")
    assert isinstance(backend, FilteredSearchBackend)
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
    filtered_after = backend.search_filtered(
        _v([1, 0, 0, 0])[0],
        5,
        {"r2"},
    )
    assert [rid for rid, _ in filtered_after] == ["r2"]


def test_filtered_search_scores_only_allowed_rids(tmp_path):
    backend = FaissBackend(4, storage_dir=tmp_path / "idx")
    backend.add(
        ["r1", "r2", "r3"],
        _v([1, 0, 0, 0], [0.8, 0.2, 0, 0], [0, 1, 0, 0]),
    )

    hits = backend.search_filtered(
        _v([1, 0, 0, 0])[0],
        2,
        {"r2", "r3", "missing"},
    )

    assert [rid for rid, _ in hits] == ["r2", "r3"]
    assert all(rid != "r1" for rid, _ in hits)
    regular_scores = dict(backend.search(_v([1, 0, 0, 0])[0], 3))
    assert [score for _rid, score in hits] == pytest.approx(
        [regular_scores["r2"], regular_scores["r3"]]
    )


def test_filtered_search_empty_selection_returns_empty(tmp_path):
    backend = FaissBackend(4, storage_dir=tmp_path / "idx")
    backend.add(["r1"], _v([1, 0, 0, 0]))

    assert backend.search_filtered(_v([1, 0, 0, 0])[0], 5, set()) == []


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
    filtered = b2.search_filtered(
        _v([1, 0, 0, 0])[0],
        10,
        {"r2"},
    )
    assert [rid for rid, _ in filtered] == ["r2"]

    b2.add(["r3"], _v([0, 0, 1, 0]))
    hits2 = b2.search(_v([0, 0, 1, 0])[0], 10)
    assert hits2[0][0] == "r3"


def test_flush_refuses_to_erase_index_after_partial_load(tmp_path, caplog):
    storage = tmp_path / "idx"
    b1 = FaissBackend(4, storage_dir=storage)
    b1.add(["r1", "r2"], _v([1, 0, 0, 0], [0, 1, 0, 0]))
    b1.flush()

    (storage / "id_map.json").unlink()

    b2 = FaissBackend(4, storage_dir=storage)
    b2.initialize()
    assert int(b2._index.ntotal) == 0

    with caplog.at_level("ERROR"):
        with pytest.raises(RuntimeError, match="refusing to persist"):
            b2.flush()
    assert "would overwrite 2 vectors" in caplog.text

    survived = faiss.read_index(str(storage / "faiss.index"))
    assert int(survived.ntotal) == 2


def test_flush_persists_an_index_emptied_by_deletes(tmp_path):
    storage = tmp_path / "idx"
    backend = FaissBackend(4, storage_dir=storage)
    backend.add(["r1"], _v([1, 0, 0, 0]))
    backend.flush()

    backend.delete(["r1"])
    backend.flush()

    reopened = FaissBackend(4, storage_dir=storage)
    reopened.initialize()
    assert int(reopened._index.ntotal) == 0


def test_initialize_raises_on_dimension_mismatch(tmp_path):
    storage = tmp_path / "idx"
    b1 = FaissBackend(4, storage_dir=storage)
    b1.add(["r1"], _v([1, 0, 0, 0]))
    b1.flush()

    b2 = FaissBackend(8, storage_dir=storage)
    with pytest.raises(ValueError, match="dimension mismatch"):
        b2.initialize()


def _flushed(storage, rows):
    backend = FaissBackend(4, storage_dir=storage)
    backend.add([f"r{i}" for i in range(len(rows))], _v(*rows))
    backend.flush()
    return backend


def test_flush_leaves_no_staging_files(tmp_path):
    storage = tmp_path / "idx"
    _flushed(storage, [[1, 0, 0, 0]])
    assert sorted(p.name for p in storage.iterdir()) == [
        "faiss.index",
        "id_map.json",
    ]


@pytest.mark.parametrize("name", ["faiss.index", "id_map.json"])
def test_initialize_rejects_truncated_file(tmp_path, name):
    storage = tmp_path / "idx"
    _flushed(storage, [[1, 0, 0, 0], [0, 1, 0, 0]])
    target = storage / name
    target.write_bytes(target.read_bytes()[:10])
    with pytest.raises(Exception):
        FaissBackend(4, storage_dir=storage).initialize()


def test_initialize_rejects_current_index_with_stale_map(tmp_path):
    storage = tmp_path / "idx"
    backend = _flushed(storage, [[1, 0, 0, 0]])
    stale_map = (storage / "id_map.json").read_bytes()
    backend.add(["r9"], _v([0, 0, 1, 0]))
    backend.flush()
    (storage / "id_map.json").write_bytes(stale_map)
    with pytest.raises(ValueError, match="inconsistent"):
        FaissBackend(4, storage_dir=storage).initialize()


def test_initialize_completes_a_flush_cut_between_renames(tmp_path):
    storage = tmp_path / "idx"
    backend = _flushed(storage, [[1, 0, 0, 0]])
    old_map = (storage / "id_map.json").read_bytes()
    backend.add(["r9"], _v([0, 0, 1, 0]))
    backend.flush()
    # Crash after the index rename, before the map rename.
    (storage / "id_map.json").rename(storage / "id_map.json.tmp")
    (storage / "id_map.json").write_bytes(old_map)

    reloaded = FaissBackend(4, storage_dir=storage)
    reloaded.initialize()
    assert reloaded.count() == 2
    assert not (storage / "id_map.json.tmp").exists()


def test_initialize_discards_a_flush_cut_before_any_rename(tmp_path):
    storage = tmp_path / "idx"
    _flushed(storage, [[1, 0, 0, 0]])
    (storage / "faiss.index.tmp").write_bytes(b"partial")
    (storage / "id_map.json.tmp").write_bytes(b"{")

    reloaded = FaissBackend(4, storage_dir=storage)
    reloaded.initialize()
    assert reloaded.count() == 1
    assert sorted(p.name for p in storage.iterdir()) == [
        "faiss.index",
        "id_map.json",
    ]
