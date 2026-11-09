# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
from pathlib import Path

import faiss
import numpy as np
import pytest

from pave.stores.local import LocalStore
from utils import FakeEmbedder


class WarmableFakeEmbedder(FakeEmbedder):
    def __init__(self):
        super().__init__()
        self.warm_calls = 0

    def warm(self) -> None:
        self.warm_calls += 1


def _index_dir(store: LocalStore, tenant: str, collection: str) -> Path:
    return Path(store._base_path(tenant, collection)) / "index"


def test_store_warm_uses_embedder_warm_hook(tmp_path):
    embedder = WarmableFakeEmbedder()
    store = LocalStore(str(tmp_path), embedder)

    store.warm()

    assert embedder.warm_calls == 1


def test_ingest_defers_faiss_flush_until_explicit_save(tmp_path):
    store = LocalStore(str(tmp_path), FakeEmbedder())
    tenant, collection = "acme", "deferred"

    store.index_records(
        tenant,
        collection,
        "doc1",
        [("0", "deferred flush probe", {"lang": "en"})],
    )

    index_dir = _index_dir(store, tenant, collection)
    assert (index_dir / ".pavedb-index-dirty").is_file()
    # Only the empty pair written at creation is on disk (B85).
    assert faiss.read_index(str(index_dir / "faiss.index")).ntotal == 0
    assert store.search(tenant, collection, "deferred", k=1)

    store._save(tenant, collection)

    assert (index_dir / "faiss.index").is_file()
    assert (index_dir / "id_map.json").is_file()
    assert not (index_dir / ".pavedb-index-dirty").exists()


def test_close_flushes_pending_index_and_clears_dirty_marker(tmp_path):
    store = LocalStore(str(tmp_path), FakeEmbedder())
    tenant, collection = "acme", "close_flush"

    store.index_records(
        tenant,
        collection,
        "doc1",
        [("0", "close flush probe", {"lang": "en"})],
    )
    index_dir = _index_dir(store, tenant, collection)
    assert (index_dir / ".pavedb-index-dirty").is_file()

    store.close()

    assert (index_dir / "faiss.index").is_file()
    assert (index_dir / "id_map.json").is_file()
    assert not (index_dir / ".pavedb-index-dirty").exists()


def test_byov_ingest_survives_reopen_with_a_dirty_marker(tmp_path):
    tenant, collection = "acme", "byov_reopen"
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.create_collection(
        tenant,
        collection,
        embedder_type="identity",
        embedder_config={"dim": 4},
    )
    store.index_records(
        tenant,
        collection,
        "doc1",
        [("0", "", {})],
        vectors=[[1.0, 0.0, 0.0, 0.0]],
    )

    index_dir = _index_dir(store, tenant, collection)
    assert (index_dir / "faiss.index").is_file()
    assert not (index_dir / ".pavedb-index-dirty").exists()

    store._mark_index_dirty(tenant, collection)
    for db in store._dbs.values():
        db.close()
    store._catalog.close()

    reopened = LocalStore(str(tmp_path), FakeEmbedder())
    hits = reopened.search(
        tenant,
        collection,
        "",
        k=1,
        query_vector=[1.0, 0.0, 0.0, 0.0],
    )

    assert hits[0].meta["docid"] == "doc1"
    # The rebuild carries the BYOV vector over from the live index, so it
    # completes for real and the marker is cleared.
    assert not (index_dir / ".pavedb-index-dirty").exists()


def _seed_mixed_collection(store, tenant, collection):
    """One stored-text chunk plus one BYOV chunk with NULL text."""
    store.index_records(
        tenant,
        collection,
        "doc_text",
        [("0", "plain text chunk about otters", {"lang": "en"})],
    )
    vector = [0.0] * 64
    vector[7] = 1.0
    store.index_records(
        tenant,
        collection,
        "doc_byov",
        [("0", "", {})],
        vectors=[vector],
    )
    store._save(tenant, collection)
    return vector


def test_rebuild_carries_byov_vectors_instead_of_bailing_out(tmp_path):
    tenant, collection = "acme", "byov_mixed"
    store = LocalStore(str(tmp_path), FakeEmbedder())
    vector = _seed_mixed_collection(store, tenant, collection)

    # Index drifted: the text chunk lost its vector but the BYOV one kept
    # its own. The rebuild must repair the first and carry the second.
    backend = store._emb[(tenant, collection)]
    backend.delete(["doc_text::0"])
    backend.flush()
    store._mark_index_dirty(tenant, collection)
    for db in list(store._dbs.values()):
        db.close()

    reopened = LocalStore(str(tmp_path), FakeEmbedder())
    text_hits = reopened.search(tenant, collection, "otters", k=5)
    byov_hits = reopened.search(
        tenant,
        collection,
        "",
        k=5,
        query_vector=vector,
    )

    assert text_hits[0].meta["docid"] == "doc_text"
    assert byov_hits[0].meta["docid"] == "doc_byov"
    index_dir = _index_dir(store, tenant, collection)
    assert not (index_dir / ".pavedb-index-dirty").exists()


def test_rebuild_that_loses_a_chunk_fails_closed(tmp_path):
    tenant, collection = "acme", "byov_lost"
    store = LocalStore(str(tmp_path), FakeEmbedder())
    _seed_mixed_collection(store, tenant, collection)

    index_dir = _index_dir(store, tenant, collection)
    for db in list(store._dbs.values()):
        db.close()
    # Index files gone: the BYOV chunk has neither text nor a recoverable
    # vector, so the rebuild is short and must not look successful.
    (index_dir / "faiss.index").unlink()
    (index_dir / "id_map.json").unlink()

    reopened = LocalStore(str(tmp_path), FakeEmbedder())
    with pytest.raises(RuntimeError, match="refusing to publish"):
        reopened.search(tenant, collection, "otters", k=5)

    assert (index_dir / ".pavedb-index-dirty").is_file()


@pytest.mark.parametrize(
    "damage",
    ["missing", "invalid_json", "truncated", "wrong_rid"],
)
def test_partial_faiss_load_preserves_byov_index(tmp_path, damage):
    tenant, collection = "acme", "byov_partial"
    store = LocalStore(str(tmp_path), FakeEmbedder())
    vector = _seed_mixed_collection(store, tenant, collection)
    index_dir = _index_dir(store, tenant, collection)
    store.close()

    index_before = (index_dir / "faiss.index").read_bytes()
    map_path = index_dir / "id_map.json"
    map_before = map_path.read_bytes()
    if damage == "missing":
        map_path.unlink()
    elif damage == "invalid_json":
        map_path.write_text("not json", encoding="utf-8")
    else:
        id_map = json.loads(map_before)
        if damage == "truncated":
            id_map["rid_to_id"] = {}
            id_map["id_to_rid"] = {}
            id_map["next_id"] = 0
        else:
            index_id = id_map["rid_to_id"].pop("doc_byov::0")
            id_map["rid_to_id"]["wrong::0"] = index_id
            id_map["id_to_rid"][str(index_id)] = "wrong::0"
        map_path.write_text(json.dumps(id_map), encoding="utf-8")

    reopened = LocalStore(str(tmp_path), FakeEmbedder())
    with pytest.raises(RuntimeError, match="refusing"):
        reopened.search(tenant, collection, "otters", k=5)
    reopened.close()

    assert (index_dir / "faiss.index").read_bytes() == index_before
    map_path.write_bytes(map_before)

    recovered = LocalStore(str(tmp_path), FakeEmbedder())
    hits = recovered.search(
        tenant,
        collection,
        "",
        k=5,
        query_vector=vector,
    )
    recovered.close()
    assert hits[0].meta["docid"] == "doc_byov"


def test_dirty_index_rebuilds_from_chunks_on_reopen(tmp_path):
    tenant, collection = "acme", "rebuild"
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.index_records(
        tenant,
        collection,
        "doc1",
        [("0", "rebuild from dirty chunk text", {"lang": "en"})],
    )
    index_dir = _index_dir(store, tenant, collection)
    assert (index_dir / ".pavedb-index-dirty").is_file()
    assert faiss.read_index(str(index_dir / "faiss.index")).ntotal == 0

    for db in list(store._dbs.values()):
        db.close()

    reopened = LocalStore(str(tmp_path), FakeEmbedder())
    hits = reopened.search(tenant, collection, "dirty chunk", k=1)

    assert hits
    assert (index_dir / "faiss.index").is_file()
    assert (index_dir / "id_map.json").is_file()
    assert not (index_dir / ".pavedb-index-dirty").exists()


def test_empty_rebuild_persists_before_clearing_dirty_marker(tmp_path):
    tenant, collection = "acme", "empty_rebuild"
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.create_collection(tenant, collection)
    index_dir = _index_dir(store, tenant, collection)

    # Simulate an index flush that retained a vector after its chunk was
    # removed from metadata. Recovery must publish the empty index pair.
    backend = store._emb[(tenant, collection)]
    backend.add(["orphan::0"], np.ones((1, 64), dtype=np.float32))
    store.close()
    marker = index_dir / ".pavedb-index-dirty"
    marker.touch()
    assert faiss.read_index(str(index_dir / "faiss.index")).ntotal == 1

    reopened = LocalStore(str(tmp_path), FakeEmbedder())
    assert not reopened.search(tenant, collection, "orphan", k=1)
    assert faiss.read_index(str(index_dir / "faiss.index")).ntotal == 0
    assert json.loads((index_dir / "id_map.json").read_text())["rid_to_id"] == {}
    assert not marker.exists()
    reopened.close()
