# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later


def test_ingest_rejects_oversized_file(client, cfg):
    client.post("/v1/collections/acme/sizelimit")

    cfg.set("ingest.max_file_size_mb", 0.000001)  # ~1 byte limit

    files = {"file": ("big.txt", b"hello world", "text/plain")}
    r = client.post("/v1/collections/acme/sizelimit/documents", files=files)

    assert r.status_code == 413
    data = r.json()
    assert data["ok"] is False
    assert data["code"] == "file_too_large"


def test_batch_ingest_rejects_oversized_text(client, cfg):
    client.post("/v1/collections/acme/batchsizelimit")

    cfg.set("ingest.max_file_size_mb", 0.000001)  # ~1 byte limit

    r = client.post(
        "/v1/collections/acme/batchsizelimit/documents:batch",
        json={"documents": [{"text": "hello world", "docid": "too-big"}]},
    )

    assert r.status_code == 413
    data = r.json()
    assert data["ok"] is False
    assert data["code"] == "file_too_large"


def test_ingest_accepts_file_within_limit(client, cfg):
    client.post("/v1/collections/acme/sizelimit2")

    cfg.set("ingest.max_file_size_mb", 100)

    files = {"file": ("small.txt", b"hello world", "text/plain")}
    r = client.post("/v1/collections/acme/sizelimit2/documents", files=files)

    assert r.status_code == 201


def test_batch_ingest_rejects_oversized_total(client, cfg):
    client.post("/v1/collections/acme/batchtotal")

    cfg.set("ingest.max_file_size_mb", 0.001)  # ~1 KB per document

    # Each document is under the per-document limit; the batch is not.
    r = client.post(
        "/v1/collections/acme/batchtotal/documents:batch",
        json={
            "documents": [
                {"text": "x" * 600, "docid": f"part-{i}"} for i in range(3)
            ]
        },
    )

    assert r.status_code == 413
    data = r.json()
    assert data["ok"] is False
    assert data["code"] == "file_too_large"
    assert "batch" in data["error"]


def test_batch_ingest_total_limit_has_its_own_key(client, cfg):
    client.post("/v1/collections/acme/batchtotal2")

    cfg.set("ingest.max_file_size_mb", 0.001)  # ~1 KB per document
    cfg.set("ingest.max_batch_size_mb", 0.01)  # ~10 KB per batch

    r = client.post(
        "/v1/collections/acme/batchtotal2/documents:batch",
        json={
            "documents": [
                {"text": "x" * 600, "docid": f"part-{i}"} for i in range(3)
            ]
        },
    )

    assert r.status_code == 201, r.text
    assert r.json()["succeeded"] == 3


def test_ingest_no_limit_when_zero(client, cfg):
    client.post("/v1/collections/acme/sizelimit3")

    cfg.set("ingest.max_file_size_mb", 0)  # 0 = unlimited

    files = {"file": ("any.txt", b"hello world", "text/plain")}
    r = client.post("/v1/collections/acme/sizelimit3/documents", files=files)

    assert r.status_code == 201
