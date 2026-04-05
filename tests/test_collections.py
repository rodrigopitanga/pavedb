# (C) 2025 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

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
