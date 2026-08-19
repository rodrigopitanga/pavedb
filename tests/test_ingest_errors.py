# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest
from pypdf.errors import PyPdfError

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


def test_multipart_ingest_rejects_non_object_metadata(client):
    r = client.post(
        "/v1/collections/acme/bad-metadata/documents",
        files={"file": ("x.txt", b"hello world", "text/plain")},
        data={"metadata": "[1, 2, 3]"},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_metadata"


def test_multipart_ingest_rejects_metadata_without_file(client):
    r = client.post(
        "/v1/collections/acme/metadata-only/documents",
        files={"metadata": (None, '{"source": "api"}')},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "missing_document_file"


def test_multipart_ingest_rejects_corrupt_pdf_as_client_error(client):
    r = client.post(
        "/v1/collections/acme/bad-pdf/documents",
        files={"file": ("bad.pdf", b"%PDF-1.4\nbroken", "application/pdf")},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "unsupported_or_invalid_document"


def test_multipart_ingest_rejects_lazy_pdf_error(client, monkeypatch):
    class BrokenPage:
        def extract_text(self):
            raise PyPdfError("broken page")

    reader = type("Reader", (), {"pages": [BrokenPage()]})()
    monkeypatch.setattr("pave.preprocess.PdfReader", lambda _: reader)
    r = client.post(
        "/v1/collections/acme/bad-pdf-page/documents",
        files={"file": ("bad.pdf", b"ignored", "application/pdf")},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "unsupported_or_invalid_document"


def test_multipart_ingest_rejects_unsupported_extension_as_document_error(client):
    r = client.post(
        "/v1/collections/acme/bad-type/documents",
        files={
            "file": (
                "bad.docx",
                b"not a supported document",
                "application/octet-stream",
            )
        },
    )
    assert r.status_code == 400
    assert r.json()["code"] == "unsupported_or_invalid_document"


def test_batch_ingest_rejects_non_object_metadata(client):
    r = client.post(
        "/v1/collections/acme/bad-metadata-batch/documents:batch",
        json={"documents": [{"text": "hello", "metadata": [1, 2, 3]}]},
    )
    assert r.status_code == 422
    data = r.json()
    assert data["code"] == "validation_error"
    assert any(
        err["loc"] == ["body", "documents", 0, "metadata"]
        for err in data["details"]["errors"]
    )


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


def test_ingest_encode_failure_returns_503(client, app, monkeypatch):
    client.post("/v1/collections/acme/encode-failure-ingest")
    embedder = app.state.store.impl._collection_embedders[
        ("acme", "encode-failure-ingest")
    ]

    def fail_encode(_texts):
        raise RuntimeError("embedder offline")

    monkeypatch.setattr(embedder, "encode", fail_encode)
    response = client.post(
        "/v1/collections/acme/encode-failure-ingest/documents",
        json={"text": "hello", "docid": "d1"},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "embedder_unavailable"


def test_json_text_ingest_rejects_control_char_docid(client):
    """A NUL in the docid used to fail mid-write, after earlier commits."""
    client.post("/v1/collections/acme/ctrldocs")
    r = client.post(
        "/v1/collections/acme/ctrldocs/documents",
        json={"text": "hello", "docid": "bad\x00id", "metadata": None},
    )
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "invalid_docid"


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_search_rejects_invalid_collection_name(client, bad):
    """Unvalidated, these auto-created a collection delete then refused."""
    r = client.post(
        f"/v1/collections/acme/{bad}/search", json={"q": "x", "k": 1}
    )
    assert r.status_code == 400, (bad, r.status_code, r.text)
    assert r.json()["code"] == "invalid_collection_name"


def test_delete_document_rejects_invalid_collection_name(client):
    r = client.delete("/v1/collections/acme/under_score/documents/d1")
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_collection_name"


def test_shared_scope_search_still_works_with_a_valid_common_collection(
    client, cfg
):
    """The configured collection must not be rejected by the caller-name rule."""
    cfg.set("common_enabled", True)
    cfg.set("common_tenant", "global")
    cfg.set("common_collection", "common")
    client.post("/v1/collections/global/common")

    r = client.post("/v1/search", json={"q": "anything", "k": 3})
    assert r.status_code == 200, r.text
