# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from concurrent.futures import ThreadPoolExecutor

import pytest

from pave.config import get_cfg
from pave.metadb import ChunkLimitError, TenantChunkLimitError
from pave.stores.local import LocalStore
from utils import FakeEmbedder


def _ingest(client, tenant, collection, docid, text="hello world"):
    return client.post(
        f"/v1/collections/{tenant}/{collection}/documents",
        json={"text": text, "docid": docid},
    )


def test_tenant_chunk_limit_enforced_over_http(client):
    # Fresh tenants: the session data dir keeps collections from other tests.
    get_cfg().set("tenants.default_max_chunks_per_tenant", 2)
    assert _ingest(client, "seltzer", "one", "d1").status_code == 201
    assert _ingest(client, "seltzer", "two", "d2").status_code == 201

    # A fresh collection is not a fresh budget: the ceiling is the tenant's.
    r = _ingest(client, "seltzer", "three", "d3")
    assert r.status_code == 429
    assert r.json()["code"] == "tenant_chunk_limit_reached"

    # Replacing an existing document at the ceiling stays allowed.
    assert _ingest(client, "seltzer", "one", "d1", "replaced").status_code == 201

    # Deleting a document frees the budget for the whole tenant.
    assert client.delete(
        "/v1/collections/seltzer/one/documents/d1"
    ).status_code == 200
    assert _ingest(client, "seltzer", "three", "d3").status_code == 201

    # Per-tenant override beats the default; 0 disables the cap.
    get_cfg().set("tenants.kramerica.max_chunks_per_tenant", 0)
    for name in ("one", "two", "three"):
        assert _ingest(client, "kramerica", name, "d1").status_code == 201


def test_collection_cap_alone_leaves_a_tenant_unbounded(tmp_path):
    # The gap this cap closes: max_chunks_per_collection bounds a collection,
    # so a tenant allowed several of them has no ceiling without P2-21.
    get_cfg().set("tenants.default_max_chunks_per_collection", 1)
    store = LocalStore(str(tmp_path), FakeEmbedder())
    for name in ("one", "two", "three"):
        store.index_records("acme", name, "d1", [("d1::0", "one", {})])
    assert store.tenant_chunk_count("acme") == 3

    get_cfg().set("tenants.default_max_chunks_per_tenant", 3)
    with pytest.raises(TenantChunkLimitError):
        store.index_records("acme", "four", "d1", [("d1::0", "one", {})])


def test_uncapped_tenant_is_never_counted(tmp_path, monkeypatch):
    store = LocalStore(str(tmp_path), FakeEmbedder())
    calls: list[str] = []
    monkeypatch.setattr(
        LocalStore,
        "tenant_chunk_count",
        lambda self, tenant, *, excluding=None: calls.append(tenant) or 0,
    )

    # 0 = unlimited is the default, and it must cost no cross-collection read.
    store.index_records("acme", "vault", "d1", [("d1::0", "one", {})])
    assert calls == []

    get_cfg().set("tenants.default_max_chunks_per_tenant", 10)
    store.index_records("acme", "vault", "d2", [("d2::0", "two", {})])
    assert calls == ["acme"]


def test_rejected_ingest_keeps_the_existing_document(tmp_path):
    get_cfg().set("tenants.default_max_chunks_per_tenant", 2)
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.index_records("acme", "one", "d1", [("d1::0", "one", {})])
    store.index_records("acme", "two", "d2", [("d2::0", "two", {})])

    with pytest.raises(TenantChunkLimitError):
        store.index_records(
            "acme",
            "one",
            "d1",
            [("d1::0", "one", {}), ("d1::1", "bis", {})],
        )

    # The refused replacement must not have purged the indexed version.
    hits = store.search("acme", "one", "one", k=5)
    assert any(hit.id == "d1::0" for hit in hits)


def test_purge_by_empty_ingest_survives_a_full_tenant(tmp_path):
    get_cfg().set("tenants.default_max_chunks_per_tenant", 2)
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.index_records("acme", "one", "d1", [("d1::0", "one", {})])
    store.index_records("acme", "two", "d2", [("d2::0", "two", {})])

    with pytest.raises(TenantChunkLimitError):
        store.index_records("acme", "one", "d3", [("d3::0", "three", {})])

    # An ingest carrying no record is a purge, not an attempt to grow, so a
    # full tenant is never locked out of freeing its own budget.
    result = store.index_records("acme", "one", "d1", [])
    assert result.indexed_chunks == 0
    assert result.purged_chunks == 1
    assert store.tenant_chunk_count("acme") == 1


def test_batch_ingest_cannot_bypass_the_tenant_ceiling(tmp_path):
    get_cfg().set("tenants.default_max_chunks_per_tenant", 2)
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.index_records("acme", "one", "d1", [("d1::0", "one", {})])

    with pytest.raises(TenantChunkLimitError):
        store.index_documents(
            "acme",
            "two",
            [
                ("d2", [("d2::0", "two", {})], None),
                ("d3", [("d3::0", "three", {})], None),
            ],
        )

    # Nothing from the rejected batch may have landed.
    hits = store.search("acme", "two", "two", k=5)
    assert not any(hit.id.startswith(("d2::", "d3::")) for hit in hits)
    assert store.tenant_chunk_count("acme") == 1


def test_a_write_over_both_caps_reports_the_tenant_ceiling(tmp_path):
    # Both caps set is the documented setup, and only the tenant ceiling
    # tells the caller that no other collection has room either.
    get_cfg().set("tenants.default_max_chunks_per_collection", 1)
    get_cfg().set("tenants.default_max_chunks_per_tenant", 1)
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.index_records("acme", "one", "d1", [("d1::0", "one", {})])

    with pytest.raises(TenantChunkLimitError):
        store.index_records("acme", "one", "d2", [("d2::0", "two", {})])
    with pytest.raises(TenantChunkLimitError):
        store.index_documents(
            "acme", "one", [("d2", [("d2::0", "two", {})], None)]
        )

    # Where the tenant still has room, the per-collection refusal stands.
    get_cfg().set("tenants.default_max_chunks_per_tenant", 10)
    with pytest.raises(ChunkLimitError):
        store.index_records("acme", "one", "d2", [("d2::0", "two", {})])
    with pytest.raises(ChunkLimitError):
        store.index_documents(
            "acme", "one", [("d2", [("d2::0", "two", {})], None)]
        )


def test_another_tenant_keeps_its_own_budget(tmp_path):
    get_cfg().set("tenants.default_max_chunks_per_tenant", 1)
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.index_records("acme", "vault", "d1", [("d1::0", "one", {})])

    with pytest.raises(TenantChunkLimitError):
        store.index_records("acme", "vault", "d2", [("d2::0", "two", {})])

    store.index_records("umbrella", "vault", "d1", [("d1::0", "one", {})])
    assert store.tenant_chunk_count("umbrella") == 1


def test_concurrent_ingests_into_one_collection_cannot_exceed_it(tmp_path):
    get_cfg().set("tenants.default_max_chunks_per_tenant", 3)
    store = LocalStore(str(tmp_path), FakeEmbedder())

    def ingest(i: int) -> str:
        try:
            store.index_records(
                "acme", "vault", f"d{i}", [(f"d{i}::0", f"text {i}", {})]
            )
            return "ok"
        except TenantChunkLimitError:
            return "limit"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(ingest, range(8)))

    # Within one collection its write lock serializes every chunk writer, so
    # the ceiling holds exactly.
    assert outcomes.count("ok") == 3
    assert outcomes.count("limit") == 5
    assert store.tenant_chunk_count("acme") == 3
