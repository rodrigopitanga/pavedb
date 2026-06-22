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
