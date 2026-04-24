# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json


def test_list_chunks_returns_chunk_summaries_without_text_preview(client):
    client.post("/v1/collections/acme/chunks-list")
    upload = client.post(
        "/v1/collections/acme/chunks-list/documents",
        files={"file": ("a.txt", b"alpha bravo", "text/plain")},
        data={
            "docid": "DOC-CHUNKS-1",
            "metadata": json.dumps({"lang": "pt"}),
        },
    )
    assert upload.status_code == 201

    r = client.get(
        "/v1/collections/acme/chunks-list/documents/DOC-CHUNKS-1/chunks"
    )

    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["tenant"] == "acme"
    assert data["collection"] == "chunks-list"
    assert data["docid"] == "DOC-CHUNKS-1"
    assert data["count"] == 1
    assert data["chunks"] == [
        {
            "rid": "DOC-CHUNKS-1::chunk_0",
            "chunk_path": "chunks/DOC-CHUNKS-1__chunk_0.txt",
            "meta": {"offset": 0},
            "ingested_at": data["chunks"][0]["ingested_at"],
        }
    ]
    assert data["chunks"][0]["ingested_at"].endswith("Z")
    assert "text_preview" not in data["chunks"][0]


def test_list_chunks_returns_empty_for_missing_doc_or_collection(client):
    client.post("/v1/collections/acme/chunks-empty")

    missing_doc = client.get(
        "/v1/collections/acme/chunks-empty/documents/MISSING/chunks"
    )
    missing_collection = client.get(
        "/v1/collections/acme/chunks-missing/documents/MISSING/chunks"
    )

    assert missing_doc.status_code == 200
    assert missing_doc.json()["chunks"] == []
    assert missing_doc.json()["count"] == 0
    assert missing_collection.status_code == 200
    assert missing_collection.json()["chunks"] == []
    assert missing_collection.json()["count"] == 0


def test_get_chunk_returns_chunk_metadata_only(client):
    client.post("/v1/collections/acme/chunks-get")
    upload = client.post(
        "/v1/collections/acme/chunks-get/documents",
        files={"file": ("b.txt", b"captain nemo", "text/plain")},
        data={"docid": "DOC-CHUNK-GET"},
    )
    assert upload.status_code == 201

    r = client.get(
        "/v1/collections/acme/chunks-get/chunks/DOC-CHUNK-GET::chunk_0"
    )

    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["tenant"] == "acme"
    assert data["collection"] == "chunks-get"
    assert data["docid"] == "DOC-CHUNK-GET"
    assert data["rid"] == "DOC-CHUNK-GET::chunk_0"
    assert data["chunk_path"] == "chunks/DOC-CHUNK-GET__chunk_0.txt"
    assert data["meta"] == {"offset": 0}
    assert data["ingested_at"].endswith("Z")
    assert "text" not in data


def test_get_chunk_not_found_returns_404(client):
    client.post("/v1/collections/acme/chunks-404")

    r = client.get(
        "/v1/collections/acme/chunks-404/chunks/DOC-404::chunk_0"
    )

    assert r.status_code == 404
    data = r.json()
    assert data["code"] == "chunk_not_found"
    assert "DOC-404::chunk_0" in data["error"]


def test_get_chunk_content_returns_raw_payload(client):
    client.post("/v1/collections/acme/chunks-content")
    upload = client.post(
        "/v1/collections/acme/chunks-content/documents",
        files={"file": ("c.txt", b"nautilus", "text/plain")},
        data={"docid": "DOC-CHUNK-CONTENT"},
    )
    assert upload.status_code == 201

    r = client.get(
        "/v1/collections/acme/chunks-content/chunks/"
        "DOC-CHUNK-CONTENT::chunk_0/content"
    )

    assert r.status_code == 200
    assert r.content == b"nautilus"
    assert r.headers["content-type"] == "text/plain; charset=utf-8"


def test_get_chunk_content_not_found_returns_404(client):
    client.post("/v1/collections/acme/chunk-content-404")

    r = client.get(
        "/v1/collections/acme/chunk-content-404/chunks/DOC-X::chunk_0/content"
    )

    assert r.status_code == 404
    data = r.json()
    assert data["code"] == "chunk_content_not_found"
