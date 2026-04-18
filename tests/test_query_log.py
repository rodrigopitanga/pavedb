# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import re
import time

from pave.metadb import CollectionDB


def _meta_db(tmp_path):
    return tmp_path / "meta.db"


def test_collection_db_query_log_roundtrip(tmp_path):
    db = CollectionDB()
    db.open(_meta_db(tmp_path))

    db.log_query(
        query_id="qid-1",
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
        query_text="first",
        k=1,
        result_count=1,
    )
    time.sleep(0.005)
    db.log_query(
        query_id="qid-2",
        query_text="second",
        k=2,
        result_count=2,
    )
    time.sleep(0.005)
    db.log_query(
        query_id="qid-3",
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
        query_text="alpha",
        k=1,
        result_count=1,
    )
    db.log_query(
        query_id="qid-books",
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
        query_text="alpha",
        k=1,
    )

    entry = db.get_query_log_entry("qid-1")

    assert entry is not None
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z",
        entry["executed_at"],
    )
    db.close()
