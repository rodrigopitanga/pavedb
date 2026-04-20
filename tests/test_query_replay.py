# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from uuid import UUID

from fastapi.testclient import TestClient


def test_replay_query_returns_fresh_results_and_logs_replay(client):
    collection = "replayfresh"
    client.post(f"/v1/collections/acme/{collection}")
    client.post(
        f"/v1/collections/acme/{collection}/documents",
        files={"file": ("a.txt", b"hello world", "text/plain")},
        data={"docid": "DOC-1"},
    )

    original = client.post(
        f"/v1/collections/acme/{collection}/search",
        json={"q": "hello", "k": 5},
    )
    assert original.status_code == 200

    logs = client.get(f"/v1/collections/acme/{collection}/queries")
    assert logs.status_code == 200
    original_query_id = logs.json()["queries"][0]["query_id"]

    original_entry = client.get(
        f"/v1/collections/acme/{collection}/queries/{original_query_id}"
    )
    assert original_entry.status_code == 200
    original_query = original_entry.json()["query"]

    client.post(
        f"/v1/collections/acme/{collection}/documents",
        files={"file": ("b.txt", b"hello again", "text/plain")},
        data={"docid": "DOC-2"},
    )

    replay = client.post(
        f"/v1/collections/acme/{collection}/queries/{original_query_id}/replay"
    )

    assert replay.status_code == 200
    data = replay.json()
    assert data["ok"] is True
    assert data["original_query_id"] == original_query_id
    assert data["replay_query_id"] != original_query_id
    UUID(data["replay_query_id"])
    assert data["original_result_count"] == original_query["result_count"]
    assert data["original_latency_ms"] == original_query["latency_ms"]
    assert len(data["matches"]) > data["original_result_count"]
    assert {match["meta"]["docid"] for match in data["matches"]} == {
        "DOC-1",
        "DOC-2",
    }

    logs_after = client.get(f"/v1/collections/acme/{collection}/queries")
    assert logs_after.status_code == 200
    assert logs_after.json()["count"] == 2
    assert {
        row["query_id"] for row in logs_after.json()["queries"]
    } == {
        original_query_id,
        data["replay_query_id"],
    }


def test_replay_query_not_found(client):
    response = client.post(
        "/v1/collections/acme/replaymissing/queries/missing-query-id/replay"
    )

    assert response.status_code == 404
    data = response.json()
    assert data["code"] == "query_not_found"
    assert "missing-query-id" in data["error"]


def test_replay_query_preserves_common_merge_and_logs_once(
    client,
    app,
    cfg,
):
    local_collection = "replaylocalmerge"
    common_collection = "replaycommonmerge"

    cfg.set("common_enabled", True)
    cfg.set("common_tenant", "global")
    cfg.set("common_collection", common_collection)

    client.post(f"/v1/collections/acme/{local_collection}")
    client.post(f"/v1/collections/global/{common_collection}")
    client.post(
        f"/v1/collections/acme/{local_collection}/documents",
        files={"file": ("a.txt", b"captain nemo local", "text/plain")},
        data={"docid": "DOC-L"},
    )
    client.post(
        f"/v1/collections/global/{common_collection}/documents",
        files={"file": ("b.txt", b"captain nemo common", "text/plain")},
        data={"docid": "DOC-C"},
    )

    original = client.post(
        f"/v1/collections/acme/{local_collection}/search",
        json={"q": "captain", "k": 5},
    )
    assert original.status_code == 200

    logs = client.get(f"/v1/collections/acme/{local_collection}/queries")
    assert logs.status_code == 200
    original_query_id = logs.json()["queries"][0]["query_id"]

    store = app.state.store
    store.calls.clear()

    replay = client.post(
        f"/v1/collections/acme/{local_collection}/queries/{original_query_id}/replay"
    )

    assert replay.status_code == 200
    search_calls = [call for call in store.calls if call[0] == "search"]
    log_calls = [call for call in store.calls if call[0] == "log_query"]

    assert (
        "search",
        "acme",
        local_collection,
        "captain",
        10,
        {},
    ) in search_calls
    assert (
        "search",
        "global",
        common_collection,
        "captain",
        10,
        {},
    ) in search_calls
    assert len(log_calls) == 1
    assert log_calls[0][1]["include_common"] is True
    assert log_calls[0][1]["common_tenant"] == "global"
    assert log_calls[0][1]["common_collection"] == common_collection

    logs_after = client.get(
        f"/v1/collections/acme/{local_collection}/queries"
    )
    assert logs_after.status_code == 200
    assert logs_after.json()["count"] == 2


def test_replay_marks_replay_of_in_log(client):
    collection = "replaymarker"
    client.post(f"/v1/collections/acme/{collection}")
    client.post(
        f"/v1/collections/acme/{collection}/documents",
        files={"file": ("a.txt", b"hello world", "text/plain")},
        data={"docid": "DOC-1"},
    )
    client.post(
        f"/v1/collections/acme/{collection}/search",
        json={"q": "hello", "k": 5},
    )
    logs = client.get(f"/v1/collections/acme/{collection}/queries")
    original_query_id = logs.json()["queries"][0]["query_id"]

    replay = client.post(
        f"/v1/collections/acme/{collection}/queries/{original_query_id}/replay"
    )
    assert replay.status_code == 200
    replay_qid = replay.json()["replay_query_id"]

    replay_entry = client.get(
        f"/v1/collections/acme/{collection}/queries/{replay_qid}"
    ).json()["query"]
    assert replay_entry["replay_of"] == original_query_id

    original_entry = client.get(
        f"/v1/collections/acme/{collection}/queries/{original_query_id}"
    ).json()["query"]
    assert original_entry["replay_of"] is None

    logs_after = client.get(
        f"/v1/collections/acme/{collection}/queries"
    ).json()
    summary = {row["query_id"]: row for row in logs_after["queries"]}
    assert summary[replay_qid]["replay_of"] == original_query_id
    assert summary[original_query_id]["replay_of"] is None


def _seed_query(client, tenant, collection, body=b"hello"):
    client.post(f"/v1/collections/{tenant}/{collection}")
    client.post(
        f"/v1/collections/{tenant}/{collection}/documents",
        files={"file": ("a.txt", body, "text/plain")},
        data={"docid": "DOC-1"},
    )
    client.post(
        f"/v1/collections/{tenant}/{collection}/search",
        json={"q": "hello", "k": 5},
    )
    logs = client.get(f"/v1/collections/{tenant}/{collection}/queries")
    return logs.json()["queries"][0]["query_id"]


def test_replay_forbidden_for_foreign_tenant(app, cfg):
    open_client = TestClient(app)
    collection = "foreignreplay"
    qid = _seed_query(open_client, "acme", collection)

    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", None)
    cfg.set("auth.api_keys", {"acme": "acmekey", "other": "otherkey"})

    c = TestClient(app)
    r = c.post(
        f"/v1/collections/acme/{collection}/queries/{qid}/replay",
        headers={"Authorization": "Bearer otherkey"},
    )
    assert r.status_code == 403
    assert r.json()["code"] == "auth_forbidden"


def test_replay_allowed_for_owning_tenant(app, cfg):
    open_client = TestClient(app)
    collection = "owningreplay"
    qid = _seed_query(open_client, "acme", collection)

    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", None)
    cfg.set("auth.api_keys", {"acme": "acmekey"})

    c = TestClient(app)
    r = c.post(
        f"/v1/collections/acme/{collection}/queries/{qid}/replay",
        headers={"Authorization": "Bearer acmekey"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_replay_allowed_for_admin_across_tenants(app, cfg):
    open_client = TestClient(app)
    collection = "adminreplay"
    qid = _seed_query(open_client, "acme", collection)

    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", "adminkey")
    cfg.set("auth.api_keys", {"other": "otherkey"})

    c = TestClient(app)
    r = c.post(
        f"/v1/collections/acme/{collection}/queries/{qid}/replay",
        headers={"Authorization": "Bearer adminkey"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_admin_replay_query_by_bare_id(app, cfg):
    open_client = TestClient(app)
    collection = "adminbareidreplay"
    qid = _seed_query(open_client, "acme", collection)

    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", "adminkey")
    cfg.set("auth.api_keys", {"other": "otherkey"})

    c = TestClient(app)
    r = c.post(
        f"/v1/admin/queries/{qid}/replay",
        headers={"Authorization": "Bearer adminkey"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["original_query_id"] == qid
    assert data["replay_query_id"] != qid


def test_replay_honours_tenant_rate_limit(app, cfg, monkeypatch):
    open_client = TestClient(app)
    collection = "ratelimitreplay"
    qid = _seed_query(open_client, "acme", collection)

    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", None)
    cfg.set("auth.api_keys", {"acme": "acmekey"})
    monkeypatch.setitem(app.state.tenant_limits, "acme", 1)
    monkeypatch.setitem(app.state.tenant_active, "acme", 1)

    c = TestClient(app)
    r = c.post(
        f"/v1/collections/acme/{collection}/queries/{qid}/replay",
        headers={"Authorization": "Bearer acmekey"},
    )
    assert r.status_code == 429
    assert r.json()["code"] == "tenant_rate_limited"
    assert r.headers.get("Retry-After") == "1"


def test_admin_replay_honours_owner_tenant_rate_limit(app, cfg, monkeypatch):
    open_client = TestClient(app)
    collection = "adminratelimitreplay"
    qid = _seed_query(open_client, "acme", collection)

    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", "adminkey")
    cfg.set("auth.api_keys", {"other": "otherkey"})
    monkeypatch.setitem(app.state.tenant_limits, "acme", 1)
    monkeypatch.setitem(app.state.tenant_active, "acme", 1)

    c = TestClient(app)
    r = c.post(
        f"/v1/admin/queries/{qid}/replay",
        headers={"Authorization": "Bearer adminkey"},
    )
    assert r.status_code == 429
    assert r.json()["code"] == "tenant_rate_limited"
    assert r.headers.get("Retry-After") == "1"
