# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from uuid import UUID


def _assert_uuid(value: str) -> None:
    assert value
    UUID(value)


def test_request_id_from_body(client):
    """request_id in body should be echoed in response."""
    client.post("/v1/collections/acme/reqid")
    client.post("/v1/collections/acme/reqid/documents",
                files={"file": ("a.txt", b"hello world", "text/plain")},
                data={"docid": "D1"})
    body = {"q": "hello", "k": 5, "request_id": "req-body-123"}
    r = client.post("/v1/collections/acme/reqid/search", json=body)
    assert r.status_code == 200
    data = r.json()
    assert r.headers["X-Request-ID"] == body["request_id"]
    assert data["request_id"] == "req-body-123"
    assert "latency_ms" in data
    assert isinstance(data["latency_ms"], (int, float))

def test_request_id_from_header(client):
    """X-Request-ID header should be echoed in response."""
    client.post("/v1/collections/acme/reqhdr")
    client.post("/v1/collections/acme/reqhdr/documents",
                files={"file": ("b.txt", b"testing headers", "text/plain")},
                data={"docid": "D2"})
    body = {"q": "testing", "k": 5}
    r = client.post("/v1/collections/acme/reqhdr/search", json=body,
                    headers={"X-Request-ID": "req-header-456"})
    assert r.status_code == 200
    assert r.headers["X-Request-ID"] == "req-header-456"
    data = r.json()
    assert data["request_id"] == "req-header-456"

def test_request_id_body_takes_precedence(client):
    """request_id in body should take precedence over header."""
    client.post("/v1/collections/acme/reqprec")
    client.post("/v1/collections/acme/reqprec/documents",
                files={"file": ("c.txt", b"precedence test", "text/plain")},
                data={"docid": "D3"})
    body = {"q": "precedence", "k": 5, "request_id": "body-wins"}
    r = client.post("/v1/collections/acme/reqprec/search", json=body,
                    headers={"X-Request-ID": "header-loses"})
    assert r.status_code == 200
    assert r.headers["X-Request-ID"] == "body-wins"
    data = r.json()
    assert data["request_id"] == "body-wins"

def test_request_id_get_endpoint(client):
    """GET search should accept X-Request-ID header."""
    client.post("/v1/collections/acme/reqget")
    client.post("/v1/collections/acme/reqget/documents",
                files={"file": ("d.txt", b"get endpoint test", "text/plain")},
                data={"docid": "D4"})
    r = client.get("/v1/collections/acme/reqget/search",
                   params={"q": "endpoint", "k": 5},
                   headers={"X-Request-ID": "get-req-789"})
    assert r.status_code == 200
    assert r.headers["X-Request-ID"] == "get-req-789"
    data = r.json()
    assert data["request_id"] == "get-req-789"
    assert "latency_ms" in data

def test_request_id_generated_when_not_provided(client):
    """request_id should be auto-generated when not provided."""
    client.post("/v1/collections/acme/reqnull")
    client.post("/v1/collections/acme/reqnull/documents",
                files={"file": ("e.txt", b"no request id", "text/plain")},
                data={"docid": "D5"})
    body = {"q": "request", "k": 5}
    r = client.post("/v1/collections/acme/reqnull/search", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["request_id"] == r.headers["X-Request-ID"]
    _assert_uuid(data["request_id"])
    assert "latency_ms" in data

def test_latency_ms_in_response(client):
    """latency_ms should be present and reasonable."""
    client.post("/v1/collections/acme/latms")
    client.post("/v1/collections/acme/latms/documents",
                files={"file": ("f.txt", b"latency measurement", "text/plain")},
                data={"docid": "D6"})
    body = {"q": "latency", "k": 5}
    r = client.post("/v1/collections/acme/latms/search", json=body)
    assert r.status_code == 200
    data = r.json()
    assert "latency_ms" in data
    assert data["latency_ms"] >= 0
    assert data["latency_ms"] < 60000  # should complete in under 60s

def test_common_search_request_id(client):
    """Common collection search should also support request_id."""
    # Common collection search returns empty matches when not enabled
    # but should still echo request_id
    body = {"q": "test", "k": 5, "request_id": "common-req-123"}
    r = client.post("/v1/search", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["request_id"] == "common-req-123"
    assert "latency_ms" in data


def test_create_collection_has_trace_fields(client):
    r = client.post(
        "/v1/collections/acme/traced",
        headers={"X-Request-ID": "create-req-1"},
    )
    assert r.status_code == 201
    data = r.json()
    assert r.headers["X-Request-ID"] == "create-req-1"
    assert data["request_id"] == "create-req-1"
    assert isinstance(data["latency_ms"], (int, float))


def test_health_ready_has_trace_fields(client):
    r = client.get("/health/ready", headers={"X-Request-ID": "health-req-1"})
    assert r.status_code in (200, 503)
    data = r.json()
    assert r.headers["X-Request-ID"] == "health-req-1"
    assert data["request_id"] == "health-req-1"
    assert isinstance(data["latency_ms"], (int, float))


def test_error_response_includes_trace_fields(client, app, monkeypatch):
    client.post("/v1/collections/acme/failtrace")

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(app.state.store, "search", boom, raising=True)
    r = client.post(
        "/v1/collections/acme/failtrace/search",
        json={"q": "x", "k": 1},
        headers={"X-Request-ID": "err-req-1"},
    )
    assert r.status_code == 500
    data = r.json()
    assert r.headers["X-Request-ID"] == "err-req-1"
    assert data["request_id"] == "err-req-1"
    assert isinstance(data["latency_ms"], (int, float))


def test_validation_error_includes_trace_fields(client):
    r = client.get(
        "/v1/search",
        params={"q": "x", "k": 0},
        headers={"X-Request-ID": "validation-req-1"},
    )
    assert r.status_code == 422
    data = r.json()
    assert r.headers["X-Request-ID"] == "validation-req-1"
    assert data["request_id"] == "validation-req-1"
    assert isinstance(data["latency_ms"], (int, float))
    assert data["code"] == "validation_error"
    assert isinstance(data["details"]["errors"], list)
