# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import json


def test_list_documents_empty_collection(client):
    client.post("/v1/collections/acme/listdocs-empty")

    r = client.get("/v1/collections/acme/listdocs-empty/documents")

    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["tenant"] == "acme"
    assert data["collection"] == "listdocs-empty"
    assert data["documents"] == []
    assert data["count"] == 0


def test_list_documents_returns_ingested_docs(client):
    client.post("/v1/collections/acme/listdocs")
    upload_a = client.post(
        "/v1/collections/acme/listdocs/documents",
        files={"file": ("a.txt", b"alpha bravo", "text/plain")},
        data={
            "docid": "DOC-A",
            "metadata": json.dumps({"lang": "en"}),
        },
    )
    upload_b = client.post(
        "/v1/collections/acme/listdocs/documents",
        files={"file": ("b.txt", b"one\ntwo\nthree", "text/plain")},
        data={
            "docid": "DOC-B",
            "metadata": json.dumps({"lang": "pt"}),
        },
    )
    assert upload_a.status_code == 201
    assert upload_b.status_code == 201

    r = client.get("/v1/collections/acme/listdocs/documents")

    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["tenant"] == "acme"
    assert data["collection"] == "listdocs"
    assert data["count"] == 2

    documents = data["documents"]
    assert [doc["docid"] for doc in documents] == ["DOC-A", "DOC-B"]
    assert [doc["version"] for doc in documents] == [1, 1]
    assert all(doc["ingested_at"].endswith("Z") for doc in documents)
    assert documents[0]["chunk_count"] == 1
    assert documents[1]["chunk_count"] == 1


def test_list_documents_nonexistent_collection_returns_empty(client):
    r = client.get("/v1/collections/acme/missing-docs/documents")

    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["tenant"] == "acme"
    assert data["collection"] == "missing-docs"
    assert data["documents"] == []
    assert data["count"] == 0


def test_list_documents_failure_hides_exception_text(client, app, monkeypatch):
    client.post("/v1/collections/acme/listdocs-fail")

    def boom(*args, **kwargs):
        raise RuntimeError("Legacy catalog.json detected in /srv/pavedb/data")

    monkeypatch.setattr(app.state.store, "list_documents", boom)
    r = client.get("/v1/collections/acme/listdocs-fail/documents")

    assert r.status_code == 500
    assert r.json()["code"] == "list_documents_failed"
    assert "/srv/pavedb" not in r.text
