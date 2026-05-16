# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import json

from pave import cli as pvcli
from pave.config import get_cfg
from pave.metadb import CatalogDB, CollectionDB
from pave.stores.local import LocalStore
from utils import FakeEmbedder


def _mk_collections_with_store(store, tenant: str, *collections: str) -> None:
    for collection in collections:
        store.create_collection(tenant, collection)


def test_create_and_delete_collection(client):
    r = client.post("/v1/collections/acme/invoices")
    assert r.status_code == 201 and r.json()["ok"] is True
    data = r.json()
    assert data["embedder_type"] == "sbert"
    assert data["embed_model"] == "fake"
    r2 = client.delete("/v1/collections/acme/invoices")
    assert r2.status_code == 200 and r2.json()["deleted"] == "invoices"


def test_create_collection_accepts_matching_embedder_config(client):
    r = client.post(
        "/v1/collections/acme/contracts",
        json={"embedder_type": "sbert", "embed_model": "fake"},
    )

    assert r.status_code == 201
    data = r.json()
    assert data["ok"] is True
    assert data["embedder_type"] == "sbert"
    assert data["embed_model"] == "fake"


def test_create_collection_rejects_different_embed_model(client):
    r = client.post(
        "/v1/collections/acme/contracts2",
        json={"embed_model": "sentence-transformers/all-MiniLM-L6-v2"},
    )

    assert r.status_code == 400
    data = r.json()
    assert data["code"] == "embed_model_not_supported"
    assert "not yet supported" in data["error"]


def test_create_collection_rejects_different_embedder_type(client):
    r = client.post(
        "/v1/collections/acme/contracts3",
        json={"embedder_type": "openai"},
    )

    assert r.status_code == 400
    data = r.json()
    assert data["code"] == "embedder_type_not_supported"
    assert "not yet supported" in data["error"]


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


def test_list_collections_api_sorted(client, tmp_path, monkeypatch):
    store = client.app.state.store.impl
    monkeypatch.setattr(store, "_data_dir", str(tmp_path))

    client.post("/v1/collections/acme/invoices")
    client.post("/v1/collections/acme/contracts")
    client.post("/v1/collections/acme/reports")

    r = client.get("/v1/collections/acme")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["tenant"] == "acme"
    assert [c["name"] for c in data["collections"]] == [
        "contracts",
        "invoices",
        "reports",
    ]
    assert [c["display_name"] for c in data["collections"]] == [
        None,
        None,
        None,
    ]
    assert [c["embedder_label"] for c in data["collections"]] == [
        "sbert:fake",
        "sbert:fake",
        "sbert:fake",
    ]
    assert data["count"] == 3


def test_list_collections_api_empty_tenant(client, tmp_path, monkeypatch):
    store = client.app.state.store.impl
    monkeypatch.setattr(store, "_data_dir", str(tmp_path))

    # Create tenant dir but no collections
    (tmp_path / "t_empty").mkdir(parents=True, exist_ok=True)

    r = client.get("/v1/collections/empty")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["collections"] == []
    assert data["count"] == 0


def test_list_collections_ignores_legacy_catalog_only(client, tmp_path, monkeypatch):
    store = client.app.state.store.impl
    monkeypatch.setattr(store, "_data_dir", str(tmp_path))
    coll_dir = tmp_path / "t_acme" / "c_legacy"
    coll_dir.mkdir(parents=True, exist_ok=True)
    (coll_dir / "catalog.json").write_text("{}", encoding="utf-8")

    r = client.get("/v1/collections/acme")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["collections"] == []
    assert data["count"] == 0


def test_list_collections_api_nonexistent_tenant(client, tmp_path, monkeypatch):
    store = client.app.state.store.impl
    monkeypatch.setattr(store, "_data_dir", str(tmp_path))

    r = client.get("/v1/collections/nonexistent")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["collections"] == []
    assert data["count"] == 0


def test_list_collections_cli(tmp_path, capsys, monkeypatch):
    cfg = get_cfg()
    monkeypatch.setattr(cfg, "_cfg", {**cfg._cfg, "data_dir": str(tmp_path)})

    # Monkeypatch the store in cli module to use the new config
    store = LocalStore(str(tmp_path), FakeEmbedder())
    monkeypatch.setattr(pvcli, "store", store)

    _mk_collections_with_store(store, "demo", "books", "articles")

    pvcli.main_cli(["list-collections", "demo"])
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["tenant"] == "demo"
    assert [c["name"] for c in out["collections"]] == [
        "articles",
        "books",
    ]
    assert [c["embedder_label"] for c in out["collections"]] == [
        "sbert:fake",
        "sbert:fake",
    ]
    assert out["count"] == 2


def test_list_collections_bootstraps_existing_collection_db(tmp_path):
    db = CollectionDB()
    db.open(tmp_path / "t_acme" / "c_bootstrap" / "meta.db")
    db.close()

    store = LocalStore(str(tmp_path), FakeEmbedder())

    assert store.list_tenants() == ["acme"]
    assert store.list_collections("acme") == [
        {
            "name": "bootstrap",
            "display_name": None,
            "embedder_label": "sbert:fake",
        }
    ]

    cfg = store.get_collection_config("acme", "bootstrap")
    assert cfg is not None
    assert cfg["backend_type"] == "faiss"
    assert cfg["embedder_type"] == "sbert"
    assert cfg["embed_model"] == "fake"


def test_local_store_accepts_injected_catalog_db(tmp_path):
    cat_db = CatalogDB()
    store = LocalStore(str(tmp_path), FakeEmbedder(), cat_db=cat_db)

    store.create_collection("acme", "docs")

    assert cat_db._conn is not None
    assert cat_db.list_tenants() == ["acme"]
    assert cat_db.list_collections("acme") == ["docs"]
