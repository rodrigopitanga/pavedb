# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later


def test_get_collection_detail_empty_collection(client):
    create = client.post("/v1/collections/acme/detail-empty")
    assert create.status_code == 201

    r = client.get("/v1/collections/acme/detail-empty/detail")

    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["tenant"] == "acme"
    assert data["name"] == "detail-empty"
    assert data["display_name"] is None
    assert data["embedder_type"] == "sbert"
    assert data["embed_model"] == "fake"
    assert data["created_at"].endswith("Z")
    assert data["doc_count"] == 0
    assert data["chunk_count"] == 0


def test_get_collection_detail_after_ingest(client):
    create = client.post("/v1/collections/acme/detail-ingest")
    assert create.status_code == 201

    upload = client.post(
        "/v1/collections/acme/detail-ingest/documents",
        files={"file": ("a.txt", b"hello detail world", "text/plain")},
        data={"docid": "DOC-1"},
    )
    assert upload.status_code == 201

    r = client.get("/v1/collections/acme/detail-ingest/detail")

    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["doc_count"] == 1
    assert data["chunk_count"] > 0


def test_get_collection_detail_not_found(client):
    r = client.get("/v1/collections/acme/missing-detail/detail")

    assert r.status_code == 404
    data = r.json()
    assert data["code"] == "collection_not_found"
    assert "missing-detail" in data["error"]
