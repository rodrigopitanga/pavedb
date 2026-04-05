# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later


def test_ingest_overloaded_returns_503(client, app, monkeypatch):
    """Counter at max returns 503 ingest_overloaded immediately."""
    client.post("/v1/collections/acme/ingestlimitcol")

    monkeypatch.setattr(app.state, "max_ingests", 1)
    monkeypatch.setattr(app.state, "active_ingests", 1)

    r = client.post(
        "/v1/collections/acme/ingestlimitcol/documents",
        files={"file": ("test.txt", b"hello world", "text/plain")},
    )
    assert r.status_code == 503
    data = r.json()
    assert data["ok"] is False
    assert data["code"] == "ingest_overloaded"
