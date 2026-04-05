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


def test_ingest_accepts_file_within_limit(client, cfg):
    client.post("/v1/collections/acme/sizelimit2")

    cfg.set("ingest.max_file_size_mb", 100)

    files = {"file": ("small.txt", b"hello world", "text/plain")}
    r = client.post("/v1/collections/acme/sizelimit2/documents", files=files)

    assert r.status_code == 201


def test_ingest_no_limit_when_zero(client, cfg):
    client.post("/v1/collections/acme/sizelimit3")

    cfg.set("ingest.max_file_size_mb", 0)  # 0 = unlimited

    files = {"file": ("any.txt", b"hello world", "text/plain")}
    r = client.post("/v1/collections/acme/sizelimit3/documents", files=files)

    assert r.status_code == 201
