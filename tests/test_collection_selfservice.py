# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tenant-facing collection portability: archive, restore, reindex.

The admin routes stay; these cover the self-service surface — a tenant
key working on its own collections only, under the expensive-op limits
(concurrency gate + persistent windows).
"""

from __future__ import annotations

import sqlite3
import types

import pytest
from fastapi.testclient import TestClient

from pave.config import get_cfg
from pave.routes._deps import operation_gate


def _ingest(client, tenant, name, text=b"Captain Nemo submarine"):
    client.post(f"/v1/collections/{tenant}/{name}")
    client.post(
        f"/v1/collections/{tenant}/{name}/documents",
        files={"file": ("book.txt", text, "text/plain")},
        data={"docid": "verne"},
    )


def test_tenant_archive_round_trip(client):
    _ingest(client, "acme", "books")

    dumped = client.get("/v1/collections/acme/books/archive")
    assert dumped.status_code == 200
    assert dumped.headers["content-type"] == "application/zip"

    restored = client.post(
        "/v1/collections/acme/library/archive",
        files={"file": ("books.zip", dumped.content, "application/zip")},
    )
    assert restored.status_code == 200
    assert restored.json()["tenant"] == "acme"
    assert restored.json()["collection"] == "library"

    result = client.post(
        "/v1/collections/acme/library/search",
        json={"q": "submarine", "k": 1},
    )
    assert result.status_code == 200
    assert result.json()["matches"][0]["id"].startswith("verne::")


def test_tenant_archive_daily_window(client):
    cfg = get_cfg()
    cfg.set("tenants.default_max_archives_per_day", 1)
    try:
        _ingest(client, "acme", "books")
        first = client.get("/v1/collections/acme/books/archive")
        assert first.status_code == 200
        second = client.get("/v1/collections/acme/books/archive")
        assert second.status_code == 429
        assert second.json()["code"] == "archive_rate_limited"
    finally:
        cfg.set("tenants.default_max_archives_per_day", -1)


@pytest.mark.parametrize(
    "field,operation,window",
    [("max_archives_per_day", "archive", 86400),
     ("max_reindex_per_month", "reindex", 30 * 86400)],
)
def test_zero_operation_budget_denies_and_override_unlimits(
    client, field, operation, window
):
    cfg = get_cfg()
    cfg.set(f"tenants.default_{field}", 0)
    store = client.app.state.store.impl
    denied = store.charge_operation_window("acme", operation, field, window)
    assert not denied.allowed
    assert denied.retry_after == 0
    cfg.set(f"tenants.acme.{field}", -1)
    assert store.charge_operation_window("acme", operation, field, window) is None


def test_zero_operation_concurrency_denies():
    cfg = get_cfg()
    cfg.set("archive.max_concurrent", 0)
    request = types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace()),
    )
    slot, refused = operation_gate(request, "acme", "archive")
    assert slot is None
    assert refused[2] == 0


def test_tenant_reindex_monthly_window_and_ownership(client):
    cfg = get_cfg()
    for key, model in (("v2", "fake-v2"), ("v3", "fake-v3")):
        cfg.set(f"embedder.instances.{key}.type", "sbert")
        cfg.set(f"embedder.instances.{key}.model", model)
    cfg.set("tenants.default_max_reindex_per_month", 1)
    try:
        _ingest(client, "acme", "books")
        started = client.post(
            "/v1/collections/acme/books/reindex",
            json={"embed_model": "fake-v2"},
        )
        assert started.status_code == 200, started.text
        job_id = started.json()["job_id"]

        # The job is visible on its own collection path only.
        mine = client.get(f"/v1/collections/acme/books/reindex/{job_id}")
        assert mine.status_code == 200
        _ingest(client, "other", "books")
        foreign = client.get(f"/v1/collections/other/books/reindex/{job_id}")
        assert foreign.status_code == 404

        # Second start in the window is refused and charged nothing extra.
        again = client.post(
            "/v1/collections/acme/books/reindex",
            json={"embed_model": "fake-v3"},
        )
        assert again.status_code == 429
        assert again.json()["code"] == "reindex_rate_limited"
    finally:
        cfg.set("tenants.default_max_reindex_per_month", -1)


def test_operation_gate_bounds_tenant_and_instance():
    cfg = get_cfg()
    cfg.set("archive.max_concurrent", 2)
    cfg.set("archive.max_concurrent_per_tenant", 1)
    try:
        request = types.SimpleNamespace(
            app=types.SimpleNamespace(state=types.SimpleNamespace()),
        )
        slot1, refused = operation_gate(request, "acme", "archive")
        assert refused is None
        _, refused_same = operation_gate(request, "acme", "archive")
        assert refused_same is not None
        assert "tenant-scoped" in refused_same[0]
        slot2, refused_other = operation_gate(request, "beta", "archive")
        assert refused_other is None
        _, refused_full = operation_gate(request, "gama", "archive")
        assert refused_full is not None
        assert "instance-scoped" in refused_full[0]
        slot1.release()
        slot3, ok = operation_gate(request, "gama", "archive")
        assert ok is None
        slot2.release()
        slot3.release()
    finally:
        cfg.set("archive.max_concurrent", 2)
        cfg.set("archive.max_concurrent_per_tenant", 1)


@pytest.mark.parametrize(
    "op,method,path,kwargs,expected",
    [
        ("archive", "get", "/v1/collections/acme/books/archive", {}, 200),
        (
            "archive", "post", "/v1/collections/acme/copy/archive",
            {"files": {"file": ("bad.zip", b"not a zip", "application/zip")}},
            400,
        ),
        (
            "reindex", "post", "/v1/collections/acme/books/reindex",
            {"json": {"embed_model": "unconfigured"}}, 400,
        ),
    ],
)
def test_operation_charge_failure_releases_slot(
    app, monkeypatch, op, method, path, kwargs, expected,
):
    cfg = get_cfg()
    old_instance = cfg.get(f"{op}.max_concurrent")
    old_tenant = cfg.get(f"{op}.max_concurrent_per_tenant")
    cfg.set(f"{op}.max_concurrent", 1)
    cfg.set(f"{op}.max_concurrent_per_tenant", 1)
    store = app.state.store
    charge = store.charge_operation_window
    calls = 0

    def flaky_charge(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")
        return charge(*args, **kwargs)

    monkeypatch.setattr(store, "charge_operation_window", flaky_charge)
    try:
        client = TestClient(
            app, base_url="http://localhost", raise_server_exceptions=False,
        )
        try:
            assert client.post("/v1/collections/acme/books").status_code == 201
            request = getattr(client, method)
            first = request(path, **kwargs)
            assert first.status_code == 500
            gate = app.state.op_gates[op]
            assert gate["active"] == 0
            assert gate["per_tenant"]["acme"] == 0
            second = request(path, **kwargs)
            assert second.status_code == expected, second.text
            assert calls == 2
            assert gate["active"] == 0
            assert gate["per_tenant"]["acme"] == 0
        finally:
            client.close()
    finally:
        cfg.set(f"{op}.max_concurrent", old_instance)
        cfg.set(f"{op}.max_concurrent_per_tenant", old_tenant)


def test_tenant_restore_rejects_invalid_archive_as_400(client):
    r = client.post(
        "/v1/collections/acme/books/archive",
        files={"file": ("backup.zip", b"not a zip", "application/zip")},
    )
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "collection_archive_invalid"


@pytest.mark.parametrize(
    "error_type,status",
    [("conflict", 409), ("unavailable", 503), ("not_found", 404)],
)
def test_tenant_reindex_maps_service_errors_like_admin(
    client, monkeypatch, error_type, status,
):
    import pave.routes.collections as routes

    monkeypatch.setattr(
        routes, "svc_start_collection_reindex",
        lambda *a, **k: {
            "ok": False, "code": "reindex_refused",
            "error": "nope", "error_type": error_type,
        },
    )
    r = client.post(
        "/v1/collections/acme/books/reindex", json={"embed_model": "fake-v2"},
    )
    assert r.status_code == status, r.text
    assert r.json()["code"] == "reindex_refused"
