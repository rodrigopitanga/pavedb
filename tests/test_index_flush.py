# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

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
    assert not (index_dir / "faiss.index").exists()
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
    """One text chunk (has a sidecar) plus one BYOV chunk (has none)."""
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


def test_rebuild_that_loses_a_chunk_stays_dirty(tmp_path):
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
    text_hits = reopened.search(tenant, collection, "otters", k=5)

    assert [h.meta["docid"] for h in text_hits] == ["doc_text"]
    assert (index_dir / ".pavedb-index-dirty").is_file()


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
    assert not (index_dir / "faiss.index").exists()

    for db in list(store._dbs.values()):
        db.close()

    reopened = LocalStore(str(tmp_path), FakeEmbedder())
    hits = reopened.search(tenant, collection, "dirty chunk", k=1)

    assert hits
    assert (index_dir / "faiss.index").is_file()
    assert (index_dir / "id_map.json").is_file()
    assert not (index_dir / ".pavedb-index-dirty").exists()
