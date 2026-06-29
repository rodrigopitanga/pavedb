# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest

# Names that create/delete reject (^[a-z0-9][a-z0-9-]{0,62}$) and that ingest
# used to auto-create anyway, minting undeletable orphan collections.
BAD_NAMES = ["under_score", "UPPER", "bad.dot", "-leadhyphen"]
LONG_DOCID = "d" * 201
BYTE_LONG_DOCID = "\U0001f600" * 200


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_multipart_ingest_rejects_invalid_collection_name(client, bad):
    r = client.post(
        f"/v1/collections/acme/{bad}/documents",
        files={"file": ("x.txt", b"hello world", "text/plain")},
    )
    assert r.status_code == 400, (bad, r.status_code, r.text)
    assert r.json()["code"] == "invalid_collection_name"


def test_json_text_ingest_rejects_invalid_collection_name(client):
    r = client.post(
        "/v1/collections/acme/under_score/documents",
        json={"text": "hello", "docid": "d1", "metadata": None},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_collection_name"


def test_batch_ingest_rejects_invalid_collection_name(client):
    r = client.post(
        "/v1/collections/acme/under_score/documents:batch",
        json={"documents": [{"text": "hello", "docid": "d1"}]},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_collection_name"


def test_rejected_ingest_does_not_mint_orphan_collection(client):
    client.post(
        "/v1/collections/acme/under_score/documents",
        files={"file": ("x.txt", b"hi", "text/plain")},
    )
    names = {
        c["name"]
        for c in client.get("/v1/collections/acme").json()["collections"]
    }
    assert "under_score" not in names


def test_valid_collection_name_still_ingests(client):
    # guard against over-rejection: a conforming name still auto-creates + ingests
    r = client.post(
        "/v1/collections/acme/valid-name-1/documents",
        files={"file": ("x.txt", b"hello world", "text/plain")},
    )
    assert r.status_code == 201


def test_multipart_ingest_rejects_overlong_docid(client):
    r = client.post(
        "/v1/collections/acme/long-docid/documents",
        files={"file": ("x.txt", b"hello world", "text/plain")},
        data={"docid": LONG_DOCID},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_docid"


def test_multipart_ingest_rejects_byte_long_docid(client):
    r = client.post(
        "/v1/collections/acme/byte-long-docid/documents",
        files={"file": ("x.txt", b"hello world", "text/plain")},
        data={"docid": BYTE_LONG_DOCID},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_docid"


def test_json_text_ingest_rejects_overlong_docid(client):
    r = client.post(
        "/v1/collections/acme/long-docid-json/documents",
        json={"text": "hello", "docid": LONG_DOCID, "metadata": None},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_docid"


def test_batch_ingest_rejects_overlong_docid(client):
    r = client.post(
        "/v1/collections/acme/long-docid-batch/documents:batch",
        json={"documents": [{"text": "hello", "docid": LONG_DOCID}]},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_docid"


def test_ingest_overloaded_returns_503(client, app, monkeypatch):
    """Counter at max returns 503 ingest_overloaded immediately."""
    client.post("/v1/collections/acme/ingestlimitcol")

    monkeypatch.setattr(app.state, "max_ingests", 1)
    monkeypatch.setattr(app.state, "active_ingests", 1)

    r = client.post(
        "/v1/collections/acme/ingestlimitcol/documents",
        files={"file": ("test.txt", b"hello world", "text/plain")},
    )
    assert r.status_code == 503
    data = r.json()
    assert data["ok"] is False
    assert data["code"] == "ingest_overloaded"
