# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Request bodies are bounded before routing, parsing, and auth (B37)."""

import asyncio
import json

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


def test_archive_restore_is_not_limited_by_document_size(guarded):
    blob = b"not a zip" + b"z" * (4 * 1024 * 1024)

    r = guarded.put(
        "/v1/admin/archive",
        files={"file": ("backup.zip", blob, "application/zip")},
        headers={"Authorization": "Bearer test-key"},
    )

    assert r.status_code == 400
    assert r.json()["code"] == "archive_invalid"


def test_collection_archive_restore_is_not_limited_by_document_size(guarded):
    blob = b"not a zip" + b"z" * (4 * 1024 * 1024)

    r = guarded.post(
        "/v1/admin/collections/acme/books/archive",
        files={"file": ("backup.zip", blob, "application/zip")},
        headers={"Authorization": "Bearer test-key"},
    )

    assert r.status_code == 400
    assert r.json()["code"] == "collection_archive_invalid"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PUT", "/v1/admin/archive"),
        ("POST", "/v1/admin/collections/acme/books/archive"),
    ],
)
def test_archive_auth_is_checked_before_body_is_read(cfg, method, path):
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", "test-key")
    received = False
    sent = []

    async def downstream(_scope, _receive, _send):
        raise AssertionError("request reached routing")

    async def receive():
        nonlocal received
        received = True
        raise AssertionError("request body was read")

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [],
        "state": {},
    }
    middleware = main_mod.BodySizeLimitMiddleware(downstream)
    asyncio.run(middleware(scope, receive, send))

    assert received is False
    assert sent[0]["status"] == 401
    assert json.loads(sent[1]["body"])["code"] == "auth_invalid"


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


def test_tenant_collection_archive_restore_gets_the_ingest_ceiling(guarded):
    headers = {"Authorization": "Bearer test-key"}
    within = b"not a zip" + b"z" * (1536 * 1024)
    r = guarded.post(
        "/v1/collections/acme/books/archive",
        files={"file": ("backup.zip", within, "application/zip")},
        headers=headers,
    )
    # Above the 1 MB JSON cap, below the 2 MB ingest cap: reaches the handler.
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "collection_archive_invalid"

    r = guarded.post(
        "/v1/collections/acme/books/archive",
        files={"file": ("backup.zip", b"z" * (4 * 1024 * 1024), "application/zip")},
    )
    assert r.status_code == 413
    assert r.json()["code"] == "request_too_large"


def test_validation_errors_do_not_echo_the_rejected_value(client):
    junk = "x" * 5000
    response = client.post(
        "/v1/collections/acme/echo/search", json={"q": "a", "k": junk}
    )

    assert response.status_code == 422
    assert junk[:100] not in response.text
    assert response.json()["details"]["errors"][0]["loc"] == ["body", "k"]


def test_long_location_parts_are_cut():
    from pave.main import _client_validation_errors

    key = "y" * 5000
    [error] = _client_validation_errors(
        [{"type": "extra_forbidden", "loc": ("body", key), "input": 1}]
    )

    assert "input" not in error
    assert error["loc"] == ["body", "y" * 64 + "..."]


def test_unparseable_bodies_get_the_error_envelope(client):
    response = client.post(
        "/v1/collections/acme/echo/search",
        content='{"q": "a", "k": 1' + "0" * 5000 + "}",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert response.json()["code"] == "http_error"
