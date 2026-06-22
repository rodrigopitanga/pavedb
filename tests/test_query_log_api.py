# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import time

from fastapi.testclient import TestClient

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
        json={"q": "captain", "k": 5, "include_common": True},
        headers={"X-Request-ID": "req-common-1"},
    )
    assert response.status_code == 200

    logs = client.get("/v1/collections/acme/local/queries")
    query_id = logs.json()["queries"][0]["query_id"]

    entry = client.get(f"/v1/collections/acme/local/queries/{query_id}")

    assert entry.status_code == 200
    data = entry.json()["query"]
    assert data["query_id"] == query_id
    assert data["actor"] == "admin"
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


def test_scoped_search_common_merge_requires_request_opt_in(client, app, cfg):
    cfg.set("common_enabled", True)
    cfg.set("common_tenant", "global")
    cfg.set("common_collection", "commonopt")

    client.post("/v1/collections/acme/localopt")
    client.post("/v1/collections/global/commonopt")
    client.post(
        "/v1/collections/acme/localopt/documents",
        files={"file": ("a.txt", b"captain local", "text/plain")},
        data={"docid": "DOC-L"},
    )
    client.post(
        "/v1/collections/global/commonopt/documents",
        files={"file": ("b.txt", b"captain common", "text/plain")},
        data={"docid": "DOC-C"},
    )

    store = app.state.store
    store.calls.clear()

    response = client.post(
        "/v1/collections/acme/localopt/search",
        json={"q": "captain", "k": 2},
    )
    assert response.status_code == 200
    assert {
        match["meta"]["docid"] for match in response.json()["matches"]
    } == {"DOC-L"}
    assert (
        "search",
        "global",
        "commonopt",
        "captain",
        10,
        None,
    ) not in store.calls
    log_calls = [call for call in store.calls if call[0] == "log_query"]
    assert log_calls[-1][1]["include_common"] is False
    assert log_calls[-1][1]["common_tenant"] is None
    assert log_calls[-1][1]["common_collection"] is None

    store.calls.clear()

    local_only = client.post(
        "/v1/collections/acme/localopt/search",
        json={"q": "captain", "k": 2, "include_common": False},
    )
    assert local_only.status_code == 200
    assert {
        match["meta"]["docid"] for match in local_only.json()["matches"]
    } == {"DOC-L"}
    assert (
        "search",
        "global",
        "commonopt",
        "captain",
        10,
        None,
    ) not in store.calls
    log_calls = [call for call in store.calls if call[0] == "log_query"]
    assert log_calls[-1][1]["include_common"] is False
    assert log_calls[-1][1]["common_tenant"] is None
    assert log_calls[-1][1]["common_collection"] is None

    store.calls.clear()

    merged = client.get(
        "/v1/collections/acme/localopt/search",
        params={"q": "captain", "k": 2, "include_common": "true"},
    )
    assert merged.status_code == 200
    assert {
        match["meta"]["docid"] for match in merged.json()["matches"]
    } == {"DOC-L", "DOC-C"}
    assert (
        "search",
        "global",
        "commonopt",
        "captain",
        10,
        None,
    ) in store.calls
    log_calls = [call for call in store.calls if call[0] == "log_query"]
    assert log_calls[-1][1]["include_common"] is True
    assert log_calls[-1][1]["common_tenant"] == "global"
    assert log_calls[-1][1]["common_collection"] == "commonopt"


def test_get_query_log_not_found(client):
    response = client.get(
        "/v1/collections/acme/missing/queries/missing-query-id"
    )

    assert response.status_code == 404
    data = response.json()
    assert data["code"] == "query_not_found"
    assert "missing-query-id" in data["error"]


def test_admin_get_query_log_entry_by_bare_id(app, cfg):
    open_client = svc_client = None
    try:
        open_client = TestClient(app)
        collection = "adminqlog"
        open_client.post(f"/v1/collections/acme/{collection}")
        open_client.post(
            f"/v1/collections/acme/{collection}/documents",
            files={"file": ("a.txt", b"hello world", "text/plain")},
            data={"docid": "DOC-1"},
        )
        open_client.post(
            f"/v1/collections/acme/{collection}/search",
            json={"q": "hello", "k": 5},
        )
        logs = open_client.get(f"/v1/collections/acme/{collection}/queries")
        query_id = logs.json()["queries"][0]["query_id"]

        cfg.set("auth.mode", "static")
        cfg.set("auth.global_key", "adminkey")
        cfg.set("auth.api_keys", {"acme": "acmekey"})

        svc_client = TestClient(app)
        unauthorized = svc_client.get(f"/v1/admin/queries/{query_id}")
        assert unauthorized.status_code == 401

        response = svc_client.get(
            f"/v1/admin/queries/{query_id}",
            headers={"Authorization": "Bearer adminkey"},
        )
        assert response.status_code == 200
        data = response.json()["query"]
        assert data["query_id"] == query_id
        assert data["tenant"] == "acme"
        assert data["collection"] == collection
        assert data["actor"] == "admin"
        assert data["query_text"] == "hello"
    finally:
        if svc_client is not None:
            svc_client.close()
        if open_client is not None:
            open_client.close()


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
        json={"q": "flag", "k": 5, "include_common": True},
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


def test_query_log_moves_with_collection_move(client):
    client.post("/v1/collections/acme/qmove")
    client.post(
        "/v1/collections/acme/qmove/documents",
        files={"file": ("a.txt", b"move log history", "text/plain")},
        data={"docid": "DOC-1"},
    )

    response = client.post(
        "/v1/collections/acme/qmove/search",
        json={"q": "move", "k": 5},
    )
    assert response.status_code == 200

    logs = client.get("/v1/collections/acme/qmove/queries")
    query_id = logs.json()["queries"][0]["query_id"]

    moved = client.post(
        "/v1/admin/collections/acme/qmove/move",
        json={"new_name": "qmoved"},
    )
    assert moved.status_code == 200

    old_logs = client.get("/v1/collections/acme/qmove/queries")
    new_logs = client.get("/v1/collections/acme/qmoved/queries")
    entry = client.get(
        f"/v1/collections/acme/qmoved/queries/{query_id}"
    )

    assert old_logs.status_code == 200
    assert old_logs.json()["count"] == 0
    assert new_logs.status_code == 200
    assert new_logs.json()["count"] == 1
    assert new_logs.json()["queries"][0]["query_id"] == query_id
    assert entry.status_code == 200
    assert entry.json()["query"]["collection"] == "qmove"


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
        actor="admin",
        _log=False,
    )

    assert result["ok"] is True
    assert inner.list_query_logs("acme", "nolog") == []
    assert not any(call[0] == "log_query" for call in store.calls)


def test_service_search_query_log_disabled_skips_query_log(app, cfg):
    cfg.set("query_log.enabled", False)
    store = app.state.store
    inner = store.impl
    inner.create_collection("acme", "nologconfig")
    inner.index_records(
        "acme",
        "nologconfig",
        "DOC-1",
        [("DOC-1::chunk_0", "hello world", {"docid": "DOC-1"})],
        doc_meta={"docid": "DOC-1"},
    )

    result = svc_search(
        store,
        "acme",
        "nologconfig",
        "hello",
        k=5,
        actor="admin",
    )

    assert result["ok"] is True
    assert result["query_id"] is None
    assert inner.list_query_logs("acme", "nologconfig") == []
    assert not any(call[0] == "log_query" for call in store.calls)
