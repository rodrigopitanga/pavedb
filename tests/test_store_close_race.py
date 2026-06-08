# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import errno
import shutil
import time
from pathlib import Path

from unittest.mock import patch

from pave.metadb import CatalogDB, CollectionDB
from pave.stores.local import LocalStore
from utils import FakeEmbedder


def _store(temp_data_dir):
    return LocalStore(str(temp_data_dir), FakeEmbedder())


def test_get_document_recovers_from_closed_cached_db(temp_data_dir):
    store = _store(temp_data_dir)
    tenant, collection, docid = "acme", "race_get_doc", "DOC-1"
    store.index_records(
        tenant,
        collection,
        docid,
        [("0", "cache close race probe", {"lang": "en"})],
    )

    # Simulate a stale cached CollectionDB object that was already closed.
    store._dbs[(tenant, collection)].close()

    # Should not raise "Cannot operate on a closed database."
    doc = store.get_document(tenant, collection, docid)
    assert doc is not None
    assert doc["docid"] == docid


def test_search_recovers_from_closed_cached_db(temp_data_dir):
    store = _store(temp_data_dir)
    tenant, collection, docid = "acme", "race_search", "DOC-2"
    store.index_records(
        tenant,
        collection,
        docid,
        [("0", "semantic probe text", {"lang": "en"})],
    )

    # Simulate stale cached CollectionDB while embeddings stay cached.
    store._dbs[(tenant, collection)].close()

    hits = store.search(tenant, collection, "semantic", k=1)
    assert len(hits) == 1
    assert hits[0].meta.get("docid") == docid


def test_delete_collection_evicts_cache_before_close(temp_data_dir):
    store = _store(temp_data_dir)
    tenant, collection = "acme", "delete_order"
    store._load_or_init(tenant, collection)
    key = (tenant, collection)
    col_db = store._dbs[key]
    seen: dict[str, bool] = {}
    orig_close = col_db.close

    def _spy_close() -> None:
        seen["present_during_close"] = key in store._dbs
        orig_close()

    col_db.close = _spy_close  # type: ignore[method-assign]

    store.delete_collection(tenant, collection)

    assert seen.get("present_during_close") is False


def test_rename_collection_evicts_old_cache_before_close(temp_data_dir):
    store = _store(temp_data_dir)
    tenant, old_name, new_name = "acme", "old_order", "new_order"
    store._load_or_init(tenant, old_name)
    old_key = (tenant, old_name)
    col_db = store._dbs[old_key]
    seen: dict[str, bool] = {}
    orig_close = col_db.close

    def _spy_close() -> None:
        seen["present_during_close"] = old_key in store._dbs
        orig_close()

    col_db.close = _spy_close  # type: ignore[method-assign]

    store.rename_collection(tenant, old_name, new_name)

    assert seen.get("present_during_close") is False


def test_get_document_fallback_opens_read_only(temp_data_dir):
    """Fallback CollectionDB opens read-only (no _wconn)."""
    store = _store(temp_data_dir)
    tenant, collection = "acme", "ro_fallback"
    docid = "DOC-RO"
    store.index_records(
        tenant, collection, docid,
        [("0", "read-only probe", {"lang": "en"})],
    )

    # Close and remove cached DB to force fallback path
    store._dbs[(tenant, collection)].close()
    del store._dbs[(tenant, collection)]

    opened: list[CollectionDB] = []
    _orig_open = CollectionDB.open

    def _spy_open(self, path, *, read_only=False):
        opened.append(self)
        _orig_open(self, path, read_only=read_only)

    with patch.object(CollectionDB, "open", _spy_open):
        result = store.get_document(tenant, collection, docid)

    assert result is not None
    assert result["docid"] == docid
    assert len(opened) == 1
    assert opened[0]._wconn is None


def test_get_document_fallback_handles_db_removed_before_read_only_open(
    temp_data_dir,
):
    store = _store(temp_data_dir)
    tenant, collection = "acme", "ro_disappears"
    docid = "DOC-GONE"
    store.index_records(
        tenant, collection, docid,
        [("0", "ro race probe", {"lang": "en"})],
    )

    store._dbs[(tenant, collection)].close()
    del store._dbs[(tenant, collection)]
    db_path = store._db_path(tenant, collection)
    opened: list[CollectionDB] = []
    _orig_open = CollectionDB.open

    def _unlink_then_open(self, path, *, read_only=False):
        if read_only and Path(path) == db_path and db_path.exists():
            db_path.unlink()
        opened.append(self)
        return _orig_open(self, path, read_only=read_only)

    with patch.object(CollectionDB, "open", _unlink_then_open):
        result = store.get_document(tenant, collection, docid)

    assert result is None
    assert len(opened) == 1
    assert not db_path.exists()


def test_get_document_fallback_treats_empty_meta_db_as_transient(temp_data_dir):
    store = _store(temp_data_dir)
    tenant, collection, docid = "acme", "ro_empty_schema", "DOC-EMPTY"
    db_path = store._db_path(tenant, collection)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.touch()

    assert store.get_document(tenant, collection, docid) is None


def test_get_query_log_entry_recovers_from_closed_cached_db(temp_data_dir):
    store = _store(temp_data_dir)
    tenant, collection = "acme", "race_query_log"
    store.create_collection(tenant, collection)
    store.log_query(
        query_id="qid-1",
        tenant=tenant,
        collection=collection,
        actor="admin",
        query_text="captain nemo",
        k=1,
        result_count=1,
    )

    store._dbs[(tenant, collection)].close()

    entry = store.get_query_log_entry(tenant, collection, "qid-1")

    assert entry is not None
    assert entry["query_id"] == "qid-1"
    assert entry["query_text"] == "captain nemo"


def test_list_query_logs_recovers_from_closed_cached_db(temp_data_dir):
    store = _store(temp_data_dir)
    tenant, collection = "acme", "race_query_logs"
    store.create_collection(tenant, collection)
    store.log_query(
        query_id="qid-1",
        tenant=tenant,
        collection=collection,
        actor="admin",
        query_text="first",
        k=1,
        result_count=1,
    )
    time.sleep(0.005)
    store.log_query(
        query_id="qid-2",
        tenant=tenant,
        collection=collection,
        actor="admin",
        query_text="second",
        k=1,
        result_count=1,
    )

    store._dbs[(tenant, collection)].close()

    rows = store.list_query_logs(tenant, collection, limit=10, offset=0)

    assert [row["query_id"] for row in rows] == ["qid-2", "qid-1"]


def test_catalog_metrics_recovers_from_closed_cached_db(temp_data_dir):
    store = _store(temp_data_dir)
    tenant, collection, docid = "acme", "race_metrics", "DOC-1"
    store.index_records(
        tenant,
        collection,
        docid,
        [("0", "metrics probe", {"lang": "en"})],
    )

    store._dbs[(tenant, collection)].close()

    metrics = store.catalog_metrics()

    assert metrics["tenant_count"] >= 1
    assert metrics["collection_count"] >= 1
    assert metrics["doc_count"] >= 1
    assert metrics["chunk_count"] >= 1


def test_flush_caches_replaces_catalog_handle_before_close(temp_data_dir):
    store = _store(temp_data_dir)
    old_catalog = store._catalog

    store._flush_caches(async_close=False)

    assert store._catalog is not old_catalog
    assert old_catalog._conn is None
    assert store._catalog._conn is None


def test_catalog_metrics_retries_after_transient_catalog_close(
    monkeypatch,
    temp_data_dir,
):
    store = _store(temp_data_dir)
    tenant, collection, docid = "acme", "race_cat_metrics", "DOC-1"
    store.index_records(
        tenant,
        collection,
        docid,
        [("0", "catalog metrics retry probe", {"lang": "en"})],
    )

    closed_catalog = CatalogDB()
    closed_catalog.open(store._catalog_db_path())
    closed_catalog.close()
    real_ensure_catalog = store._ensure_catalog
    attempts = {"count": 0}

    def flaky_ensure_catalog():
        attempts["count"] += 1
        if attempts["count"] == 1:
            return closed_catalog
        return real_ensure_catalog()

    monkeypatch.setattr(store, "_ensure_catalog", flaky_ensure_catalog)

    metrics = store.catalog_metrics()

    assert attempts["count"] >= 2
    assert metrics["tenant_count"] >= 1
    assert metrics["collection_count"] >= 1
    assert metrics["doc_count"] >= 1
    assert metrics["chunk_count"] >= 1


def test_delete_collection_retries_after_transient_catalog_close(
    monkeypatch,
    temp_data_dir,
):
    store = _store(temp_data_dir)
    tenant, collection, docid = "acme", "retry_cat_delete", "DOC-1"
    store.index_records(
        tenant,
        collection,
        docid,
        [("0", "catalog delete retry probe", {"lang": "en"})],
    )

    closed_catalog = CatalogDB()
    closed_catalog.open(store._catalog_db_path())
    closed_catalog.close()
    real_ensure_catalog = store._ensure_catalog
    attempts = {"count": 0}

    def flaky_ensure_catalog():
        attempts["count"] += 1
        if attempts["count"] == 1:
            return closed_catalog
        return real_ensure_catalog()

    monkeypatch.setattr(store, "_ensure_catalog", flaky_ensure_catalog)

    store.delete_collection(tenant, collection)

    assert attempts["count"] >= 2
    assert not Path(store._base_path(tenant, collection)).exists()
    assert store.get_collection_config(tenant, collection) is None


def test_delete_collection_retries_transient_dir_not_empty(
    monkeypatch,
    temp_data_dir,
):
    store = _store(temp_data_dir)
    tenant, collection, docid = "acme", "retry_delete", "DOC-DEL"
    store.index_records(
        tenant, collection, docid,
        [("0", "delete retry probe", {"lang": "en"})],
    )
    target = Path(store._base_path(tenant, collection))
    calls = {"n": 0}
    real_rmtree = shutil.rmtree

    def flaky_rmtree(path, *args, **kwargs):
        if Path(path) == target and calls["n"] < 2:
            calls["n"] += 1
            raise OSError(errno.ENOTEMPTY, "directory not empty")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", flaky_rmtree)

    store.delete_collection(tenant, collection)

    assert calls["n"] == 2
    assert not target.exists()
