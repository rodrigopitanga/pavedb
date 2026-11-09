# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import pytest

from pave.embedders.base import EmbedderUnavailableError
from pave.stores.local import LocalStore
from utils import FakeEmbedder


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


@pytest.mark.parametrize("batch", [False, True])
def test_replacement_reencode_failure_preserves_document(
    tmp_path, monkeypatch, batch,
):
    tenant, collection = "acme", "docs"
    key = (tenant, collection)
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.index_records(
        tenant, collection, "d1",
        [("0", "original text", {"version": "old"})],
        doc_meta={"source": "original"},
    )
    store._save(tenant, collection)
    original_chunk = store.get_chunk_content(tenant, collection, "d1::0")
    original_doc = store.get_document(tenant, collection, "d1")
    original_vector = FakeEmbedder().encode(["original text"])[0].tolist()

    class FailingEmbedder(FakeEmbedder):
        def encode(self, texts):
            raise EmbedderUnavailableError("provider unavailable")

    original_encode = store._encode_embedder
    calls = 0

    def swap_after_first_encode(embedder, texts):
        nonlocal calls
        calls += 1
        vectors = original_encode(embedder, texts)
        store._collection_embedders[key] = FailingEmbedder()
        return vectors

    monkeypatch.setattr(store, "_encode_embedder", swap_after_first_encode)
    replacement = [("0", "replacement text", {"version": "new"})]
    with pytest.raises(EmbedderUnavailableError, match="provider unavailable"):
        if batch:
            store.index_documents(
                tenant, collection,
                [("d1", replacement, {"source": "replacement"}),
                 ("d2", [("0", "second document", {})], {})],
            )
        else:
            store.index_records(
                tenant, collection, "d1", replacement,
                doc_meta={"source": "replacement"},
            )
    assert calls == 2
    assert store.get_document(tenant, collection, "d1") == original_doc
    assert store.get_chunk_content(tenant, collection, "d1::0") == original_chunk
    assert store.get_document(tenant, collection, "d2") is None
    assert [hit.id for hit in store.search(
        tenant, collection, "", query_vector=original_vector,
    ).matches] == ["d1::0"]

    store.close()
    reopened = LocalStore(str(tmp_path), FakeEmbedder())
    assert [hit.id for hit in reopened.search(
        tenant, collection, "", query_vector=original_vector,
    ).matches] == ["d1::0"]
    assert reopened.get_document(tenant, collection, "d1") == original_doc
    assert reopened.get_chunk_content(tenant, collection, "d1::0") == original_chunk
    assert reopened.get_document(tenant, collection, "d2") is None


@pytest.mark.parametrize("batch", [False, True])
def test_replacement_reencode_success_replaces_old_chunks(
    tmp_path, monkeypatch, batch,
):
    tenant, collection = "acme", "docs"
    key = (tenant, collection)
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.index_records(tenant, collection, "d1", [("0", "old", {})])
    original_encode = store._encode_embedder

    def swap_after_first_encode(embedder, texts):
        vectors = original_encode(embedder, texts)
        store._collection_embedders[key] = FakeEmbedder()
        return vectors

    monkeypatch.setattr(store, "_encode_embedder", swap_after_first_encode)
    if batch:
        store.index_documents(
            tenant, collection,
            [("d1", [("1", "new", {})], {"source": "new"}),
             ("d2", [("0", "second", {})], {})],
        )
    else:
        store.index_records(
            tenant, collection, "d1", [("1", "new", {})],
            doc_meta={"source": "new"},
        )
    assert [chunk["rid"] for chunk in store.list_chunks(
        tenant, collection, "d1",
    )] == ["d1::1"]
    assert store.get_document(tenant, collection, "d1")["metadata"]["source"] == "new"
    assert store.get_chunk_content(tenant, collection, "d1::0") is None
    assert store.get_chunk_content(tenant, collection, "d1::1")["content"] == b"new"
    new_vector = FakeEmbedder().encode(["new"])[0].tolist()
    hits = store.search(
        tenant, collection, "", query_vector=new_vector,
    ).matches
    assert "d1::1" in {hit.id for hit in hits}
    assert "d1::0" not in {hit.id for hit in hits}


def test_selector_change_failure_preserves_old_document(tmp_path, monkeypatch):
    from pave.stores import local as local_mod

    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.index_records("acme", "docs", "d1", [("0", "old", {})])
    original = store.get_document("acme", "docs", "d1")
    selector_calls = 0

    def resolve_selector(_cfg, *, selector):
        nonlocal selector_calls
        selector_calls += 1
        if selector_calls == 2:
            raise EmbedderUnavailableError("selector unavailable")
        return FakeEmbedder()

    space_calls = 0

    def changed_space(_cfg):
        nonlocal space_calls
        space_calls += 1
        return f"space-{space_calls}"

    monkeypatch.setattr(store, "_get_embedder_for_config", resolve_selector)
    monkeypatch.setattr(
        local_mod.embedder_factory_mod, "vector_space_key", changed_space,
    )
    with pytest.raises(EmbedderUnavailableError, match="selector unavailable"):
        store.index_records(
            "acme", "docs", "d1", [("1", "new", {})],
            embedder_selector="native",
        )
    assert store.get_document("acme", "docs", "d1") == original
    assert [chunk["rid"] for chunk in store.list_chunks(
        "acme", "docs", "d1",
    )] == ["d1::0"]
