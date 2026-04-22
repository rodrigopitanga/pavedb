# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import re
import time

from pave.metadb import CatalogDB, CollectionDB
from pave.service import (
    get_query_log_entry_scoped as svc_get_query_log_entry_scoped,
    replay_query_scoped as svc_replay_query_scoped,
)
from pave.stores.local import LocalStore
from utils import FakeEmbedder


def _meta_db(tmp_path):
    return tmp_path / "meta.db"


def _catalog_db(tmp_path):
    return tmp_path / "catalog.db"


def test_collection_db_query_log_roundtrip(tmp_path):
    db = CollectionDB()
    db.open(_meta_db(tmp_path))

    db.log_query(
        query_id="qid-1",
        tenant="acme",
        collection="docs",
        actor="tenant:acme",
        query_text="captain nemo",
        k=3,
        filters={"lang": "en"},
        include_common=True,
        common_tenant="global",
        common_collection="common",
        result_ids=["DOC-1::chunk_0", "DOC-2::chunk_0"],
        result_count=2,
        latency_ms=12.34,
        timing={
            "embed_ms": 1.0,
            "search_ms": 2.0,
            "filter_ms": 3.0,
            "hydrate_ms": 4.0,
        },
        request_id="req-1",
    )

    entry = db.get_query_log_entry("qid-1")

    assert entry is not None
    assert entry["query_id"] == "qid-1"
    assert entry["tenant"] == "acme"
    assert entry["collection"] == "docs"
    assert entry["actor"] == "tenant:acme"
    assert entry["query_text"] == "captain nemo"
    assert entry["k"] == 3
    assert entry["filters"] == {"lang": "en"}
    assert entry["include_common"] is True
    assert entry["common_tenant"] == "global"
    assert entry["common_collection"] == "common"
    assert entry["result_ids"] == ["DOC-1::chunk_0", "DOC-2::chunk_0"]
    assert entry["result_count"] == 2
    assert entry["latency_ms"] == 12.34
    assert entry["timing"] == {
        "embed_ms": 1.0,
        "search_ms": 2.0,
        "filter_ms": 3.0,
        "hydrate_ms": 4.0,
    }
    assert entry["request_id"] == "req-1"
    assert entry["replay_of"] is None
    assert entry["executed_at"].endswith("Z")
    db.close()


def test_collection_db_list_query_logs_pagination(tmp_path):
    db = CollectionDB()
    db.open(_meta_db(tmp_path))

    db.log_query(
        query_id="qid-1",
        tenant="acme",
        collection="docs",
        actor="admin",
        query_text="first",
        k=1,
        result_count=1,
    )
    time.sleep(0.005)
    db.log_query(
        query_id="qid-2",
        tenant="acme",
        collection="docs",
        actor="admin",
        query_text="second",
        k=2,
        result_count=2,
    )
    time.sleep(0.005)
    db.log_query(
        query_id="qid-3",
        tenant="acme",
        collection="docs",
        actor="admin",
        query_text="third",
        k=3,
        result_count=3,
    )

    page1 = db.list_query_logs(limit=1, offset=0)
    page2 = db.list_query_logs(limit=1, offset=1)

    assert len(page1) == 1
    assert len(page2) == 1
    assert page1[0]["query_id"] == "qid-3"
    assert page2[0]["query_id"] == "qid-2"
    db.close()


def test_collection_db_list_query_logs_replay_marker(tmp_path):
    db = CollectionDB()
    db.open(_meta_db(tmp_path))

    db.log_query(
        query_id="qid-docs",
        tenant="acme",
        collection="docs",
        actor="admin",
        query_text="alpha",
        k=1,
        result_count=1,
    )
    db.log_query(
        query_id="qid-books",
        tenant="acme",
        collection="docs",
        actor="admin",
        query_text="beta",
        k=1,
        result_count=1,
        replay_of="qid-docs",
    )

    all_logs = db.list_query_logs()

    summary = {row["query_id"]: row for row in all_logs}
    assert summary["qid-docs"]["replay_of"] is None
    assert summary["qid-books"]["replay_of"] == "qid-docs"
    db.close()


def test_collection_db_query_log_executed_at_auto_generated(tmp_path):
    db = CollectionDB()
    db.open(_meta_db(tmp_path))

    db.log_query(
        query_id="qid-1",
        tenant="acme",
        collection="docs",
        actor="admin",
        query_text="alpha",
        k=1,
    )

    entry = db.get_query_log_entry("qid-1")

    assert entry is not None
    assert entry["tenant"] == "acme"
    assert entry["collection"] == "docs"
    assert entry["actor"] == "admin"
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z",
        entry["executed_at"],
    )
    db.close()


def test_catalog_db_query_home_roundtrip_and_filters(tmp_path):
    db = CatalogDB()
    db.open(_catalog_db(tmp_path))

    db.put_query_home("qid-1", "acme", "docs")
    time.sleep(0.005)
    db.put_query_home("qid-2", "acme", "books")
    time.sleep(0.005)
    db.put_query_home("qid-3", "beta", "docs")

    assert db.resolve_query_home("qid-1") == ("acme", "docs")
    assert db.resolve_query_home("missing") is None

    all_rows = db.list_query_homes(limit=10, offset=0)
    acme_rows = db.list_query_homes(tenant="acme", limit=10, offset=0)
    docs_rows = db.list_query_homes(collection="docs", limit=10, offset=0)

    assert [row["query_id"] for row in all_rows] == [
        "qid-3",
        "qid-2",
        "qid-1",
    ]
    assert [row["query_id"] for row in acme_rows] == ["qid-2", "qid-1"]
    assert [row["query_id"] for row in docs_rows] == ["qid-3", "qid-1"]
    assert all(row["created_at"].endswith("Z") for row in all_rows)
    db.close()


def test_local_store_query_home_purged_on_delete_collection(tmp_path):
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.create_collection("acme", "docs")
    store.log_query(
        query_id="qid-1",
        tenant="acme",
        collection="docs",
        actor="admin",
        query_text="alpha",
        k=1,
        result_count=1,
    )

    assert store.resolve_query_home("qid-1") == ("acme", "docs")

    store.delete_collection("acme", "docs")

    assert store.resolve_query_home("qid-1") is None


def test_local_store_query_home_follows_collection_rename(tmp_path):
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.create_collection("acme", "docs")
    store.log_query(
        query_id="qid-rename",
        tenant="acme",
        collection="docs",
        actor="admin",
        query_text="alpha",
        k=1,
        result_count=1,
    )

    store.rename_collection("acme", "docs", "renamed")

    assert store.resolve_query_home("qid-rename") == ("acme", "renamed")
    entry = store.get_query_log_entry("acme", "renamed", "qid-rename")
    assert entry is not None
    assert entry["tenant"] == "acme"
    assert entry["collection"] == "docs"
    assert entry["actor"] == "admin"


def test_local_store_query_home_upsert_failure_keeps_collection_log(
    tmp_path,
    monkeypatch,
    caplog,
):
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.create_collection("acme", "docs")
    catalog = store._ensure_catalog()

    def fail_put(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(catalog, "put_query_home", fail_put)

    with caplog.at_level("WARNING"):
        store.log_query(
            query_id="qid-fail",
            tenant="acme",
            collection="docs",
            actor="admin",
            query_text="alpha",
            k=1,
            result_count=1,
        )

    assert "query_home upsert failed" in caplog.text
    entry = store.get_query_log_entry("acme", "docs", "qid-fail")
    assert entry is not None
    assert entry["query_id"] == "qid-fail"
    assert store.resolve_query_home("qid-fail") is None


def test_scoped_query_lookup_returns_not_found_on_query_home_mismatch(
    tmp_path,
):
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.create_collection("acme", "docs")
    store.create_collection("acme", "other")
    store.log_query(
        query_id="qid-scope",
        tenant="acme",
        collection="docs",
        actor="admin",
        query_text="alpha",
        k=1,
        result_count=1,
    )

    result = svc_get_query_log_entry_scoped(
        store,
        "acme",
        "other",
        "qid-scope",
    )

    assert result["ok"] is False
    assert result["code"] == "query_not_found"


def test_scoped_replay_returns_not_found_on_query_home_mismatch(tmp_path):
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.create_collection("acme", "docs")
    store.create_collection("acme", "other")
    store.log_query(
        query_id="qid-replay-scope",
        tenant="acme",
        collection="docs",
        actor="admin",
        query_text="alpha",
        k=1,
        result_count=1,
    )

    result = svc_replay_query_scoped(
        store,
        "acme",
        "other",
        "qid-replay-scope",
        actor="admin",
    )

    assert result["ok"] is False
    assert result["code"] == "query_not_found"


def test_scoped_query_lookup_still_works_when_query_home_is_missing(
    tmp_path,
    monkeypatch,
):
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.create_collection("acme", "docs")
    store.log_query(
        query_id="qid-no-home",
        tenant="acme",
        collection="docs",
        actor="admin",
        query_text="alpha",
        k=1,
        result_count=1,
    )

    monkeypatch.setattr(store, "resolve_query_home", lambda _qid: None)

    result = svc_get_query_log_entry_scoped(
        store,
        "acme",
        "docs",
        "qid-no-home",
    )

    assert result["ok"] is True
    assert result["query"]["query_id"] == "qid-no-home"


def test_replay_after_collection_rename_uses_current_home_but_keeps_history(tmp_path):
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.create_collection("acme", "docs")
    store.index_records(
        "acme",
        "docs",
        "DOC-1",
        [("DOC-1::chunk_0", "alpha renamed", {"docid": "DOC-1"})],
        doc_meta={"docid": "DOC-1"},
    )
    store.log_query(
        query_id="qid-rename",
        tenant="acme",
        collection="docs",
        actor="tenant:acme",
        query_text="alpha",
        k=1,
        result_ids=["DOC-1::chunk_0"],
        result_count=1,
    )

    store.rename_collection("acme", "docs", "renamed")

    result = svc_replay_query_scoped(
        store,
        "acme",
        "renamed",
        "qid-rename",
        actor="admin",
    )

    assert result["ok"] is True
    assert result["original_query_id"] == "qid-rename"
    assert result["replay_query_id"] != "qid-rename"

    original = store.get_query_log_entry("acme", "renamed", "qid-rename")
    replay = store.get_query_log_entry(
        "acme",
        "renamed",
        result["replay_query_id"],
    )
    assert original is not None
    assert replay is not None
    assert original["collection"] == "docs"
    assert original["actor"] == "tenant:acme"
    assert replay["collection"] == "renamed"
    assert replay["actor"] == "admin"
    assert replay["replay_of"] == "qid-rename"
