# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""FAISS vector backend.

`FaissBackend` is the in-tree `VectorBackend` implementation. It
wraps `faiss.IndexIDMap2` over `IndexFlatIP` (cosine similarity
on normalized vectors) and persists the index plus an
`rid → faiss_id` mapping under its storage directory.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Collection
from pathlib import Path

import faiss
import numpy as np
from numpy.typing import NDArray

from .base import SearchHit

log = logging.getLogger(__name__)


class FaissBackend:
    """FAISS-backed `VectorBackend` for cosine similarity search.

    Uses `IndexFlatIP` (inner product) over normalized vectors.
    Persists index state and the string-id mapping under
    `storage_dir`. Single-process: callers serialize writes.
    """
    def __init__(
        self,
        dimension: int,
        *,
        storage_dir: Path,
    ) -> None:
        self._dimension = int(dimension)
        self._storage_dir = storage_dir
        self._index = faiss.IndexIDMap2(faiss.IndexFlatIP(self._dimension))
        self._rid_to_id: dict[str, int] = {}
        self._id_to_rid: dict[int, str] = {}
        self._id_to_position: dict[int, int] = {}
        self._next_id = 0
        self._dirty = False
        self._discarded = False

    @property
    def _index_file(self) -> Path:
        return self._storage_dir / "faiss.index"

    @property
    def _map_file(self) -> Path:
        return self._storage_dir / "id_map.json"

    def initialize(self) -> None:
        if not self._index_file.is_file() or not self._map_file.is_file():
            return

        loaded = faiss.read_index(str(self._index_file))
        loaded_dim = int(getattr(loaded, "d", -1))
        if loaded_dim != self._dimension:
            raise ValueError(
                "FAISS index dimension mismatch: "
                f"expected {self._dimension}, found {loaded_dim}"
            )
        data = json.loads(self._map_file.read_text(encoding="utf-8"))
        rid_to_id_raw = data.get("rid_to_id", {})
        id_to_rid_raw = data.get("id_to_rid", {})
        rid_to_id = {
            str(rid): int(iid)
            for rid, iid in rid_to_id_raw.items()
        }
        id_to_rid = {
            int(iid): str(rid)
            for iid, rid in id_to_rid_raw.items()
        }
        next_id = int(data.get("next_id", 0))
        index_ids = {
            int(iid) for iid in faiss.vector_to_array(loaded.id_map)
        }
        if (
            loaded.metric_type != faiss.METRIC_INNER_PRODUCT
            or len(rid_to_id) != int(loaded.ntotal)
            or len(id_to_rid) != int(loaded.ntotal)
            or {iid: rid for rid, iid in rid_to_id.items()} != id_to_rid
            or index_ids != set(id_to_rid)
            or next_id < 0
            or next_id <= max(id_to_rid, default=-1)
        ):
            raise ValueError("FAISS id map is inconsistent with index")
        self._index = loaded
        self._rid_to_id = rid_to_id
        self._id_to_rid = id_to_rid
        self._rebuild_positions()
        self._next_id = next_id
        self._dirty = False

    def _rebuild_positions(self) -> None:
        """Refresh external FAISS ID to flat-index row positions."""
        self._id_to_position = {
            int(iid): position
            for position, iid in enumerate(
                faiss.vector_to_array(self._index.id_map)
            )
        }

    def add(self, rids: list[str], vectors: NDArray[np.float32]) -> None:
        if not rids:
            return
        if len(rids) != int(vectors.shape[0]):
            raise ValueError("rids and vectors length mismatch")

        unique_rids: list[str] = []
        rid_to_vector: dict[str, NDArray[np.float32]] = {}
        for idx, rid in enumerate(rids):
            srid = str(rid)
            if srid not in rid_to_vector:
                unique_rids.append(srid)
            rid_to_vector[srid] = vectors[idx]

        reindex_rids = [rid for rid in unique_rids if rid in self._rid_to_id]
        if reindex_rids:
            self.delete(reindex_rids)

        matrix = np.vstack([rid_to_vector[rid] for rid in unique_rids]).astype(
            np.float32,
            copy=False,
        )
        normed = matrix.copy()
        faiss.normalize_L2(normed)

        int_ids: list[int] = []
        for rid in unique_rids:
            iid = self._next_id
            self._next_id += 1
            self._rid_to_id[rid] = iid
            self._id_to_rid[iid] = rid
            int_ids.append(iid)

        first_position = int(self._index.ntotal)
        ids = np.array(int_ids, dtype=np.int64)
        self._index.add_with_ids(normed, ids)
        for offset, iid in enumerate(int_ids):
            self._id_to_position[iid] = first_position + offset
        self._dirty = True
        self._discarded = False

    def search(
        self,
        vector: NDArray[np.float32],
        k: int,
    ) -> list[SearchHit]:
        if self._index.ntotal == 0:
            return []

        q = np.asarray(vector, dtype=np.float32).reshape(1, -1).copy()
        faiss.normalize_L2(q)

        limit = min(int(k), int(self._index.ntotal))
        scores, ids = self._index.search(q, limit)

        out: list[SearchHit] = []
        for iid, score in zip(ids[0], scores[0], strict=False):
            if int(iid) < 0:
                continue
            rid = self._id_to_rid.get(int(iid))
            if rid is None:
                continue
            out.append((rid, float(score)))
        return out

    def search_filtered(
        self,
        vector: NDArray[np.float32],
        k: int,
        allowed_rids: Collection[str],
    ) -> list[SearchHit]:
        """Search only vectors whose record IDs are allowed.

        ``IndexIDMap2`` retains the stable external-ID mapping for rows in its
        flat index. Scoring only the selected rows keeps work proportional to
        the SQLite selection and avoids rebuilding a temporary FAISS index per
        request. The flat inner-product index is exact, so this produces the
        same cosine scores as ``search`` for the selected records.
        """
        limit = int(k)
        if limit <= 0 or self._index.ntotal == 0 or not allowed_rids:
            return []

        q = np.asarray(vector, dtype=np.float32).reshape(1, -1).copy()
        faiss.normalize_L2(q)
        query = q[0]

        selected: list[tuple[str, int]] = []
        for rid in sorted({str(value) for value in allowed_rids}):
            iid = self._rid_to_id.get(rid)
            if iid is None:
                continue
            position = self._id_to_position.get(iid)
            if position is not None:
                selected.append((rid, position))

        if not selected:
            return []

        flat = faiss.downcast_index(self._index.index)
        vectors = faiss.rev_swig_ptr(
            flat.get_xb(),
            int(flat.ntotal) * self._dimension,
        ).reshape(int(flat.ntotal), self._dimension)

        # Bound the temporary matrix while keeping score computation in NumPy
        # rather than crossing the Python/FAISS boundary once per record.
        hits: list[SearchHit] = []
        batch_size = 4096
        for offset in range(0, len(selected), batch_size):
            batch = selected[offset : offset + batch_size]
            positions = np.asarray(
                [position for _rid, position in batch],
                dtype=np.intp,
            )
            scores = vectors[positions] @ query
            hits.extend(
                (rid, float(score))
                for (rid, _position), score in zip(
                    batch,
                    scores,
                    strict=False,
                )
            )

        hits.sort(key=lambda hit: (-hit[1], hit[0]))
        return hits[:limit]

    def count(self) -> int:
        """Return the number of vectors currently indexed."""
        return int(self._index.ntotal)

    def rids(self) -> set[str]:
        return set(self._rid_to_id)

    def get_vector(self, rid: str) -> NDArray[np.float32] | None:
        """Return the stored (normalized) vector for `rid`, or None.

        Records ingested with a caller-supplied vector keep no text to
        re-encode, so the index is the only surviving copy of their
        embedding and an index rebuild has to read it back from here.
        """
        iid = self._rid_to_id.get(str(rid))
        if iid is None:
            return None
        try:
            vector = self._index.reconstruct(int(iid))
        except Exception:
            # The id map can drift from the index (partial load, or a
            # removal that only landed on one side); no vector, no lie.
            return None
        return np.asarray(vector, dtype=np.float32).reshape(-1)

    def delete(self, rids: list[str]) -> None:
        if not rids:
            return
        int_ids: list[int] = []
        for rid in rids:
            srid = str(rid)
            iid = self._rid_to_id.pop(srid, None)
            if iid is None:
                continue
            self._id_to_rid.pop(iid, None)
            int_ids.append(iid)

        if int_ids:
            self._index.remove_ids(np.array(int_ids, dtype=np.int64))
            self._rebuild_positions()
            self._dirty = True
            self._discarded = False

    def discard(self) -> None:
        """Drop pending persistence for a backend that is about to be removed."""
        self._discarded = True

    def flush(self) -> None:
        if self._discarded:
            return
        persisted = self._index_file.is_file() and self._map_file.is_file()
        if persisted and not self._dirty:
            return

        if int(self._index.ntotal) == 0 and not self._dirty:
            # Empty and untouched means this instance never took ownership of
            # whatever is on disk (partial or failed load), so writing it out
            # would erase vectors nobody asked to delete.
            on_disk = 0
            if self._index_file.is_file():
                try:
                    on_disk = int(faiss.read_index(str(self._index_file)).ntotal)
                except Exception:
                    on_disk = 0
            if on_disk > 0:
                message = (
                    "FAISS index refusing to persist: empty in-memory index "
                    f"would overwrite {on_disk} vectors at {self._index_file}"
                )
                log.error(message)
                raise RuntimeError(message)

        self._storage_dir.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self._index, str(self._index_file))

        payload = {
            "rid_to_id": self._rid_to_id,
            "id_to_rid": {str(iid): rid for iid, rid in self._id_to_rid.items()},
            "next_id": self._next_id,
        }
        self._map_file.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        self._dirty = False

    def close(self) -> None:
        self.flush()
