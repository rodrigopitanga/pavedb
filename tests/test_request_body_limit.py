# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Request bodies are bounded before routing, parsing, and auth (B37)."""

import pytest
from fastapi.testclient import TestClient

import pave.main as main_mod


@pytest.fixture()
def guarded(cfg):
    """App with a small body cap and authentication turned on."""
    cfg.set("server.max_request_body_mb", 1)
    cfg.set("ingest.max_file_size_mb", 2)
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", "test-key")
    app = main_mod.build_app(cfg)
    client = TestClient(app)
    try:
        yield client
    finally:
        client.close()


def test_oversize_json_body_rejected_before_auth(guarded):
    body = '{"v": [' + ",".join(["0.0"] * 400_000) + "]}"

    r = guarded.post(
        "/v1/collections/acme/books/search",
        content=body.encode(),
        headers={"content-type": "application/json"},
    )

    # Without the guard this is a 401: the body is parsed first, and the
    # allocation an unauthenticated caller triggers is unbounded.
    assert r.status_code == 413
    data = r.json()
    assert data["ok"] is False
    assert data["code"] == "request_too_large"
    assert data["request_id"]


def test_chunked_oversize_body_rejected(guarded):
    def chunks():
        yield b'{"documents": [{"text": "'
        for _ in range(3):
            yield b"y" * (1024 * 1024)
        yield b'"}]}'

    # No Content-Length: the cap is enforced on the stream instead.
    r = guarded.post(
        "/v1/collections/acme/books/documents:batch",
        content=chunks(),
        headers={"content-type": "application/json"},
    )

    assert r.status_code == 413
    assert r.json()["code"] == "request_too_large"


def test_chunked_body_within_limit_is_accepted(guarded):
    def chunks():
        yield b'{"documents": [{"text": "hello chunked", "docid": "CH1"}]}'

    r = guarded.post(
        "/v1/collections/acme/books/documents:batch",
        content=chunks(),
        headers={
            "content-type": "application/json",
            "Authorization": "Bearer test-key",
        },
    )

    assert r.status_code == 201, r.text


def test_upload_within_ingest_limit_is_accepted(guarded):
    blob = ("lorem ipsum dolor sit amet " * 80_000)[: 2 * 1024 * 1024 - 4096]

    r = guarded.post(
        "/v1/collections/acme/books/documents",
        files={"file": ("big.txt", blob.encode(), "text/plain")},
        headers={"Authorization": "Bearer test-key"},
    )

    # A multipart upload at the configured ingest size stays legitimate even
    # though it is far above the in-memory body cap.
    assert r.status_code == 201, r.text
    assert r.json()["chunks"] > 0


def test_upload_over_ingest_limit_rejected_before_auth(guarded):
    blob = b"z" * (4 * 1024 * 1024)

    r = guarded.post(
        "/v1/collections/acme/books/documents",
        files={"file": ("huge.txt", blob, "text/plain")},
    )

    assert r.status_code == 413
    assert r.json()["code"] == "request_too_large"


def test_small_request_is_untouched(guarded):
    r = guarded.post(
        "/v1/collections/acme/books/search",
        json={"q": "nemo", "k": 3},
    )

    assert r.status_code == 401
    assert r.json()["code"] == "auth_invalid"


def test_multipart_header_cannot_buy_the_upload_limit(client, cfg):
    """The ceiling follows the path; Content-Type is the caller's to forge."""
    limit = client.app.state.max_body_bytes
    upload_limit = client.app.state.max_upload_body_bytes
    assert upload_limit > limit, "test needs the two ceilings to differ"

    oversize = b"x" * (limit + 4096)
    r = client.post(
        "/v1/collections/acme/anything/search",
        content=oversize,
        headers={"Content-Type": "multipart/form-data; boundary=spoof"},
    )
    assert r.status_code == 413, r.status_code
    assert r.json()["code"] == "request_too_large"
