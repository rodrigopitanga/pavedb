# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""BYOV raw-vector mode over HTTP (mirror Chroma's server path).

Identity collections are vector-only; embedding collections are permissive
(accept text OR a raw vector). See MEMORIES "BYOV / raw-vector mode".
"""


def _mk_identity(client, tenant="acme", name="vecs", dim=4):
    r = client.post(
        f"/v1/collections/{tenant}/{name}",
        json={"embedder_type": "identity", "embedder_config": {"dim": dim}},
    )
    assert r.status_code == 201, r.text
    return tenant, name


def test_identity_vector_round_trip(client, cfg):
    tenant, name = _mk_identity(client)
    for vec, docid in ([1.0, 0.0, 0.0, 0.0], "A"), ([0.0, 1.0, 0.0, 0.0], "B"):
        r = client.post(
            f"/v1/collections/{tenant}/{name}/documents",
            json={"vector": vec, "docid": docid},
        )
        assert r.status_code == 201, r.text

    r = client.post(
        f"/v1/collections/{tenant}/{name}/search",
        json={"v": [1.0, 0.0, 0.0, 0.0], "k": 2},
    )
    assert r.status_code == 200, r.text
    matches = r.json()["matches"]
    assert matches[0]["meta"]["docid"] == "A"
    filtered = client.post(
        f"/v1/collections/{tenant}/{name}/search",
        json={
            "v": [1.0, 0.0, 0.0, 0.0],
            "content_filter": {"op": "exact", "value": "anything"},
        },
    )
    assert filtered.status_code == 200
    assert filtered.json()["matches"] == []

    cfg.set("common_enabled", True)
    cfg.set("common_tenant", tenant)
    cfg.set("common_collection", name)
    r = client.post("/v1/search", json={"v": [1.0, 0.0, 0.0, 0.0], "k": 1})
    assert r.status_code == 200, r.text
    assert r.json()["matches"][0]["meta"]["docid"] == "A"


def test_embedding_collection_accepts_both_text_and_vector(client):
    # Default (native/fake) embedder, dim 64 — permissive: text OR vector.
    client.post("/v1/collections/acme/mixed")
    r = client.post(
        "/v1/collections/acme/mixed/documents",
        json={"text": "hello world", "docid": "t1"},
    )
    assert r.status_code == 201, r.text
    r = client.post(
        "/v1/collections/acme/mixed/documents",
        json={"vector": [0.0] * 64, "docid": "v1"},
    )
    assert r.status_code == 201, r.text
    # Both query forms are accepted on an embedding collection.
    assert client.post(
        "/v1/collections/acme/mixed/search", json={"q": "hello"}
    ).status_code == 200
    assert client.post(
        "/v1/collections/acme/mixed/search", json={"v": [0.0] * 64}
    ).status_code == 200


def test_dim_mismatch_ingest_and_search_are_400(client):
    tenant, name = _mk_identity(client)
    r = client.post(
        f"/v1/collections/{tenant}/{name}/documents",
        json={"vector": [1.0, 2.0], "docid": "bad"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "dim_mismatch"

    r = client.post(
        f"/v1/collections/{tenant}/{name}/search",
        json={"v": [1.0, 2.0]},
    )
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "dim_mismatch"


def test_embedding_collection_dim_mismatch_is_400(client):
    client.post("/v1/collections/acme/mixed-dim")
    for path, payload in (
        ("documents", {"vector": [1.0, 2.0], "docid": "bad"}),
        ("search", {"v": [1.0, 2.0]}),
    ):
        r = client.post(f"/v1/collections/acme/mixed-dim/{path}", json=payload)
        assert r.status_code == 400, r.text
        assert r.json()["code"] == "dim_mismatch"


def test_raw_vector_ingest_validates_docid_and_metadata(client):
    tenant, name = _mk_identity(client)
    vector = [1.0, 0.0, 0.0, 0.0]

    r = client.post(
        f"/v1/collections/{tenant}/{name}/documents",
        json={"vector": vector, "docid": "x" * 201},
    )
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "invalid_docid"

    r = client.post(
        f"/v1/collections/{tenant}/{name}/documents",
        json={"vector": vector, "docid": "valid", "metadata": {"text": "x"}},
    )
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "invalid_metadata_keys"


def test_text_on_identity_collection_is_400(client):
    tenant, name = _mk_identity(client)
    r = client.post(
        f"/v1/collections/{tenant}/{name}/documents",
        json={"text": "not allowed", "docid": "x"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "vectors_only"

    r = client.post(
        f"/v1/collections/{tenant}/{name}/search",
        json={"q": "not allowed"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "vectors_only"


def test_exactly_one_of_text_or_vector_on_ingest(client):
    client.post("/v1/collections/acme/oneof")
    # JSON ingest validates the body by hand → 400 invalid_document_json.
    for bad in ({"docid": "n"}, {"text": "hi", "vector": [0.0] * 64}):
        r = client.post("/v1/collections/acme/oneof/documents", json=bad)
        assert r.status_code == 400, r.text
        assert r.json()["code"] == "invalid_document_json"


def test_exactly_one_of_q_or_v_on_search(client):
    client.post("/v1/collections/acme/oneofsearch")
    # Search uses a FastAPI body param → schema validation → 422.
    for bad in ({}, {"q": "hi", "v": [0.0] * 64}):
        r = client.post("/v1/collections/acme/oneofsearch/search", json=bad)
        assert r.status_code == 422, r.text


def test_batch_vector_ingest_round_trip(client):
    tenant, name = _mk_identity(client)
    r = client.post(
        f"/v1/collections/{tenant}/{name}/documents:batch",
        json={"documents": [
            {"vector": [1.0, 0.0, 0.0, 0.0], "docid": "A"},
            {"vector": [0.0, 1.0, 0.0, 0.0], "docid": "B"},
        ]},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["succeeded"] == 2 and body["failed"] == 0

    r = client.post(
        f"/v1/collections/{tenant}/{name}/search",
        json={"v": [0.0, 1.0, 0.0, 0.0], "k": 2},
    )
    assert r.json()["matches"][0]["meta"]["docid"] == "B"
