# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations


def test_json_text_document_ingest_api(client):
    create = client.post("/v1/collections/acme/jsondocs")
    assert create.status_code == 201

    response = client.post(
        "/v1/collections/acme/jsondocs/documents",
        json={
            "text": "Captain Nemo commands the Nautilus.",
            "docid": "NOTE-1",
            "metadata": {"source": "json"},
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert data["ok"] is True
    assert data["tenant"] == "acme"
    assert data["collection"] == "jsondocs"
    assert data["docid"] == "NOTE-1"
    assert data["chunks"] == 1

    doc = client.get("/v1/collections/acme/jsondocs/documents/NOTE-1")
    assert doc.status_code == 200
    metadata = doc.json()["metadata"]
    assert metadata["source"] == "json"
    assert "filename" not in metadata


def test_batch_document_ingest_api(client):
    create = client.post("/v1/collections/acme/batchdocs")
    assert create.status_code == 201

    response = client.post(
        "/v1/collections/acme/batchdocs/documents:batch",
        json={
            "documents": [
                {
                    "text": "Captain Nemo commands the Nautilus.",
                    "docid": "NOTE-1",
                    "metadata": {"source": "manual"},
                },
                {
                    "text": "The Nautilus dives beneath the ice.",
                    "docid": "NOTE-2",
                    "metadata": None,
                },
                {
                    "text": "",
                    "docid": "EMPTY",
                    "metadata": None,
                },
            ]
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert data["ok"] is True
    assert data["tenant"] == "acme"
    assert data["collection"] == "batchdocs"
    assert data["count"] == 3
    assert data["succeeded"] == 2
    assert data["failed"] == 1
    assert data["documents"][0]["docid"] == "NOTE-1"
    assert data["documents"][0]["chunks"] == 1
    assert data["documents"][2]["ok"] is False
    assert data["documents"][2]["code"] == "no_text_extracted"

    doc = client.get("/v1/collections/acme/batchdocs/documents/NOTE-1")
    assert doc.status_code == 200
    metadata = doc.json()["metadata"]
    assert metadata["source"] == "manual"
    assert "filename" not in metadata


def test_batch_document_ingest_rejects_empty_payload(client):
    client.post("/v1/collections/acme/batchreject")

    response = client.post(
        "/v1/collections/acme/batchreject/documents:batch",
        json={"documents": []},
    )

    assert response.status_code == 422
