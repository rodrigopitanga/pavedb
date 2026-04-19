# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import time

from pave.service import search as svc_search


def test_search_logs_query_and_lists_it(client):
    client.post("/v1/collections/acme/qlog")
    client.post(
        "/v1/collections/acme/qlog/documents",
        files={"file": ("a.txt", b"hello world", "text/plain")},
        data={"docid": "DOC-1"},
    )

    response = client.post(
        "/v1/collections/acme/qlog/search",
        json={"q": "hello", "k": 5},
    )

    assert response.status_code == 200

    logs = client.get("/v1/collections/acme/qlog/queries")

    assert logs.status_code == 200
    data = logs.json()
    assert data["ok"] is True
    assert data["tenant"] == "acme"
    assert data["collection"] == "qlog"
    assert data["count"] == 1
    assert data["queries"][0]["query_text"] == "hello"
    assert data["queries"][0]["result_count"] >= 1
    assert data["queries"][0]["query_id"]
    assert data["queries"][0]["executed_at"].endswith("Z")


def test_get_query_log_entry_includes_common_merge_fields(client, cfg):
    cfg.set("common_enabled", True)
    cfg.set("common_tenant", "global")
    cfg.set("common_collection", "common")

    client.post("/v1/collections/acme/local")
    client.post("/v1/collections/global/common")
    client.post(
        "/v1/collections/acme/local/documents",
        files={"file": ("a.txt", b"captain nemo local", "text/plain")},
        data={"docid": "DOC-L"},
    )
    client.post(
        "/v1/collections/global/common/documents",
        files={"file": ("b.txt", b"captain nemo common", "text/plain")},
        data={"docid": "DOC-C"},
    )

    response = client.post(
        "/v1/collections/acme/local/search",
        json={"q": "captain", "k": 5},
        headers={"X-Request-ID": "req-common-1"},
    )
    assert response.status_code == 200

    logs = client.get("/v1/collections/acme/local/queries")
    query_id = logs.json()["queries"][0]["query_id"]

    entry = client.get(f"/v1/collections/acme/local/queries/{query_id}")

    assert entry.status_code == 200
    data = entry.json()["query"]
    assert data["query_id"] == query_id
    assert data["include_common"] is True
    assert data["common_tenant"] == "global"
    assert data["common_collection"] == "common"
    assert data["request_id"] == "req-common-1"
    assert len(data["result_ids"]) >= 1
    assert set(data["timing"]) == {
        "embed_ms",
        "search_ms",
        "filter_ms",
        "hydrate_ms",
    }


def test_get_query_log_not_found(client):
    response = client.get(
        "/v1/collections/acme/missing/queries/missing-query-id"
    )

    assert response.status_code == 404
    data = response.json()
    assert data["code"] == "query_not_found"
    assert "missing-query-id" in data["error"]


def test_list_query_logs_pagination(client):
    client.post("/v1/collections/acme/qpage")
    client.post(
        "/v1/collections/acme/qpage/documents",
        files={"file": ("a.txt", b"alpha beta gamma", "text/plain")},
        data={"docid": "DOC-1"},
    )

    first = client.post(
        "/v1/collections/acme/qpage/search",
        json={"q": "alpha", "k": 1},
    )
    assert first.status_code == 200
    time.sleep(0.005)
    second = client.post(
        "/v1/collections/acme/qpage/search",
        json={"q": "beta", "k": 1},
    )
    assert second.status_code == 200

    page1 = client.get("/v1/collections/acme/qpage/queries?limit=1")
    page2 = client.get("/v1/collections/acme/qpage/queries?limit=1&offset=1")

    assert page1.status_code == 200
    assert page2.status_code == 200
    assert page1.json()["count"] == 1
    assert page2.json()["count"] == 1
    assert page1.json()["queries"][0]["query_text"] == "beta"
    assert page2.json()["queries"][0]["query_text"] == "alpha"


def test_search_log_reflects_common_merge_flags(client, cfg):
    cfg.set("common_enabled", True)
    cfg.set("common_tenant", "global")
    cfg.set("common_collection", "common")

    client.post("/v1/collections/acme/qflags")
    client.post("/v1/collections/global/common")
    client.post(
        "/v1/collections/acme/qflags/documents",
        files={"file": ("a.txt", b"flag local", "text/plain")},
        data={"docid": "DOC-L"},
    )
    client.post(
        "/v1/collections/global/common/documents",
        files={"file": ("b.txt", b"flag common", "text/plain")},
        data={"docid": "DOC-C"},
    )

    response = client.post(
        "/v1/collections/acme/qflags/search",
        json={"q": "flag", "k": 5},
    )
    assert response.status_code == 200

    logs = client.get("/v1/collections/acme/qflags/queries")
    query_id = logs.json()["queries"][0]["query_id"]
    entry = client.get(f"/v1/collections/acme/qflags/queries/{query_id}")

    assert entry.status_code == 200
    query = entry.json()["query"]
    assert query["include_common"] is True
    assert query["common_tenant"] == "global"
    assert query["common_collection"] == "common"
    assert query["replay_of"] is None


def test_query_log_moves_with_collection_rename(client):
    client.post("/v1/collections/acme/qrename")
    client.post(
        "/v1/collections/acme/qrename/documents",
        files={"file": ("a.txt", b"rename log history", "text/plain")},
        data={"docid": "DOC-1"},
    )

    response = client.post(
        "/v1/collections/acme/qrename/search",
        json={"q": "rename", "k": 5},
    )
    assert response.status_code == 200

    logs = client.get("/v1/collections/acme/qrename/queries")
    query_id = logs.json()["queries"][0]["query_id"]

    renamed = client.put(
        "/v1/collections/acme/qrename",
        json={"new_name": "qrenamed"},
    )
    assert renamed.status_code == 200

    old_logs = client.get("/v1/collections/acme/qrename/queries")
    new_logs = client.get("/v1/collections/acme/qrenamed/queries")
    entry = client.get(
        f"/v1/collections/acme/qrenamed/queries/{query_id}"
    )

    assert old_logs.status_code == 200
    assert old_logs.json()["count"] == 0
    assert new_logs.status_code == 200
    assert new_logs.json()["count"] == 1
    assert new_logs.json()["queries"][0]["query_id"] == query_id
    assert entry.status_code == 200
    assert entry.json()["query"]["collection"] == "qrenamed"


def test_service_search_with_log_false_skips_query_log(app):
    store = app.state.store
    inner = store.impl
    inner.create_collection("acme", "nolog")
    inner.index_records(
        "acme",
        "nolog",
        "DOC-1",
        [("DOC-1::chunk_0", "hello world", {"docid": "DOC-1"})],
        doc_meta={"docid": "DOC-1"},
    )

    result = svc_search(
        store,
        "acme",
        "nolog",
        "hello",
        k=5,
        _log=False,
    )

    assert result["ok"] is True
    assert inner.list_query_logs("acme", "nolog") == []
    assert not any(call[0] == "log_query" for call in store.calls)
