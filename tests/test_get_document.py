# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json


def test_get_document_success(client):
    client.post("/v1/collections/acme/getdoc")
    upload = client.post(
        "/v1/collections/acme/getdoc/documents",
        files={"file": ("meta.txt", b"hello document", "text/plain")},
        data={
            "docid": "DOC-GET-1",
            "metadata": json.dumps({"lang": "pt", "source": "api"}),
        },
    )
    assert upload.status_code == 201
    assert upload.json()["ok"] is True

    r = client.get("/v1/collections/acme/getdoc/documents/DOC-GET-1")
    assert r.status_code == 200
    data = r.json()

    assert data["ok"] is True
    assert data["tenant"] == "acme"
    assert data["collection"] == "getdoc"
    assert data["docid"] == "DOC-GET-1"
    assert data["version"] == 1
    assert data["chunk_ids"] == ["DOC-GET-1::chunk_0"]
    assert data["chunk_count"] == 1
    assert data["ingested_at"].endswith("Z")
    assert data["metadata"]["docid"] == "DOC-GET-1"
    assert data["metadata"]["filename"] == "meta.txt"
    assert data["metadata"]["lang"] == "pt"
    assert data["metadata"]["source"] == "api"
    assert data["metadata"]["ingested_at"].endswith("Z")


def test_get_document_not_found(client):
    client.post("/v1/collections/acme/getdoc404")

    r = client.get("/v1/collections/acme/getdoc404/documents/MISSING")

    assert r.status_code == 404
    data = r.json()
    assert data["code"] == "document_not_found"
    assert "MISSING" in data["error"]
