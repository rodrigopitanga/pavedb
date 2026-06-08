# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

def test_delete_document_success(client):
    """DELETE /collections/{t}/{c}/documents/{docid} should delete a document."""
    client.post("/v1/collections/acme/deldoc")
    client.post("/v1/collections/acme/deldoc/documents",
                files={"file": ("a.txt", b"hello world", "text/plain")},
                data={"docid": "DOC-DEL-1"})
    # Verify document is searchable
    r = client.post("/v1/collections/acme/deldoc/search",
                    json={"q": "hello", "k": 5, "filters": {"docid": "DOC-DEL-1"}})
    assert r.status_code == 200
    assert len(r.json()["matches"]) >= 1

    # Delete the document
    r = client.delete("/v1/collections/acme/deldoc/documents/DOC-DEL-1")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["docid"] == "DOC-DEL-1"
    assert data["chunks_deleted"] >= 1

    # Verify document is no longer searchable
    r = client.post("/v1/collections/acme/deldoc/search",
                    json={"q": "hello", "k": 5, "filters": {"docid": "DOC-DEL-1"}})
    assert r.status_code == 200
    assert len(r.json()["matches"]) == 0

def test_delete_document_not_found(client):
    """DELETE non-existent document is idempotent: 200 with chunks_deleted=0."""
    client.post("/v1/collections/acme/deldoc2")
    r = client.delete("/v1/collections/acme/deldoc2/documents/NONEXISTENT")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["chunks_deleted"] == 0

def test_delete_document_preserves_others(client):
    """Deleting one document should not affect other documents."""
    client.post("/v1/collections/acme/deldoc3")
    # Upload two documents
    client.post("/v1/collections/acme/deldoc3/documents",
                files={"file": ("a.txt", b"alpha bravo charlie", "text/plain")},
                data={"docid": "DOC-A"})
    client.post("/v1/collections/acme/deldoc3/documents",
                files={"file": ("b.txt", b"delta echo foxtrot", "text/plain")},
                data={"docid": "DOC-B"})

    # Delete DOC-A
    r = client.delete("/v1/collections/acme/deldoc3/documents/DOC-A")
    assert r.status_code == 200

    # DOC-B should still be searchable
    r = client.post("/v1/collections/acme/deldoc3/search",
                    json={"q": "delta", "k": 5, "filters": {"docid": "DOC-B"}})
    assert r.status_code == 200
    assert len(r.json()["matches"]) >= 1

    # DOC-A should not be searchable
    r = client.post("/v1/collections/acme/deldoc3/search",
                    json={"q": "alpha", "k": 5, "filters": {"docid": "DOC-A"}})
    assert r.status_code == 200
    assert len(r.json()["matches"]) == 0

def test_delete_document_metrics(client):
    """Deleting a document should increment documents_deleted_total."""
    client.post("/v1/collections/acme/deldoc4")
    client.post("/v1/collections/acme/deldoc4/documents",
                files={"file": ("c.txt", b"metrics test", "text/plain")},
                data={"docid": "DOC-M"})

    snap1 = client.get("/health/metrics").json()
    initial = snap1.get("documents_deleted_total", 0)

    r = client.delete("/v1/collections/acme/deldoc4/documents/DOC-M")
    assert r.status_code == 200

    snap2 = client.get("/health/metrics").json()
    assert snap2["documents_deleted_total"] == initial + 1

def test_delete_document_cli(tmp_path, monkeypatch):
    """CLI delete-document command should work."""
    from pave.cli import main_cli
    from pave.config import get_cfg
    from pave.stores.local import LocalStore
    from utils import FakeEmbedder

    cfg = get_cfg()
    monkeypatch.setattr(cfg, "_cfg", {
        "data_dir": str(tmp_path),
        "vector_store": {"type": "faiss"},
        "auth": {"mode": "none"},
    })

    store = LocalStore(str(tmp_path), FakeEmbedder())
    import pave.cli
    monkeypatch.setattr(pave.cli, "store", store)

    # Create collection and ingest document
    main_cli(["create-collection", "t1", "c1"])
    test_file = tmp_path / "test.txt"
    test_file.write_text("hello world cli test")
    main_cli(["ingest", "t1", "c1", str(test_file), "--docid", "CLI-DOC"])

    # Verify document exists
    assert store.get_document("t1", "c1", "CLI-DOC") is not None

    # Delete document via CLI
    main_cli(["delete-document", "t1", "c1", "CLI-DOC"])

    # Verify document is gone
    assert store.get_document("t1", "c1", "CLI-DOC") is None
