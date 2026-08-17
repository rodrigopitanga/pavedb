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


def test_batch_document_ingest_api(client, app):
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
    assert any(call[0] == "index_documents" for call in app.state.store.calls)

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


def test_batch_rejects_bad_chunk_id_before_committing_earlier_docs(tmp_path):
    """A mid-loop failure used to leave metadata rows with no vectors."""
    import pytest

    from pave.stores.local import LocalStore
    from utils import FakeEmbedder

    store = LocalStore(str(tmp_path), FakeEmbedder())
    with pytest.raises(ValueError, match="invalid chunk id"):
        store.index_documents(
            "acme",
            "batch",
            [
                ("good1", [("good1::0", "one", {})], {}),
                ("bad", [("bad\x00::0", "two", {})], {}),
            ],
        )

    # Assert on the metadata side, not on search: search returns nothing
    # either way, because the vectors are added after the commit loop. The
    # durable rows are what diverged from the index.
    assert store.list_documents("acme", "batch") == []


def test_reingest_with_bad_chunk_id_keeps_the_previous_version(tmp_path):
    """index_records purges before it writes, so it must reject even earlier."""
    import pytest

    from pave.stores.local import LocalStore
    from utils import FakeEmbedder

    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.index_records("acme", "docs", "d1", [("d1::0", "original", {})])

    with pytest.raises(ValueError, match="invalid chunk id"):
        store.index_records("acme", "docs", "d1", [("d1::\x00", "replaced", {})])

    hits = list(store.search("acme", "docs", "original", k=5).matches)
    assert [h.id for h in hits] == ["d1::0"]
