# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from threading import Event

import pytest

from pave.config import get_cfg
from pave.metadb import ChunkLimitError, CollectionDB, TenantChunkLimitError
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

    # Per-tenant override beats the default; -1 disables the cap.
    get_cfg().set("tenants.kramerica.max_chunks_per_tenant", -1)
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

    # -1 = unlimited is the default, and it must cost no cross-collection read.
    store.index_records("acme", "vault", "d1", [("d1::0", "one", {})])
    assert calls == []

    get_cfg().set("tenants.default_max_chunks_per_tenant", 10)
    store.index_records("acme", "vault", "d2", [("d2::0", "two", {})])
    assert calls == ["acme"]


def test_zero_tenant_chunk_budget_refuses_ingest(tmp_path):
    get_cfg().set("tenants.default_max_chunks_per_tenant", 0)
    store = LocalStore(str(tmp_path), FakeEmbedder())
    with pytest.raises(TenantChunkLimitError):
        store.index_records("acme", "books", "d1", [("d1::0", "text", {})])
    get_cfg().set("tenants.acme.max_chunks_per_tenant", -1)
    store.index_records("acme", "books", "d1", [("d1::0", "text", {})])


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


def test_batch_replacement_spends_only_net_chunks(tmp_path):
    get_cfg().set("tenants.default_max_chunks_per_tenant", 3)
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.index_records("acme", "one", "d1", [("d1::0", "one", {})])
    store.index_records("acme", "two", "d2", [("d2::0", "two", {})])

    store.index_documents(
        "acme", "one", [
            ("d1", [("d1::0", "replacement", {})], None),
            ("d3", [("d3::0", "new", {})], None),
        ],
    )
    assert store.tenant_chunk_count("acme") == 3

    with pytest.raises(TenantChunkLimitError):
        store.index_documents(
            "acme", "one", [
                ("d1", [("d1::0", "too", {}), ("d1::1", "many", {})], None),
                ("d3", [("d3::0", "also", {})], None),
            ],
        )
    assert any(
        hit.id == "d1::0"
        for hit in store.search("acme", "one", "replacement", k=5)
    )
    assert store.tenant_chunk_count("acme") == 3


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


@pytest.mark.parametrize("batch_second", [False, True])
def test_distinct_collection_admission_is_atomic(
    tmp_path, monkeypatch, batch_second
):
    get_cfg().set("tenants.default_max_chunks_per_tenant", 2)
    store = LocalStore(str(tmp_path), FakeEmbedder())
    for name in ("one", "two"):
        store.index_records("acme", name, "seed", [])

    first_counting = Event()
    release_first = Event()
    second_admitting = Event()
    second_counting = Event()
    original_count = store.tenant_chunk_count
    original_lock = store._tenant_chunk_write_lock

    def counted(tenant, *, excluding=None):
        if excluding == "one":
            first_counting.set()
            assert release_first.wait(30)
        else:
            second_counting.set()
        return original_count(tenant, excluding=excluding)

    @contextmanager
    def admission(tenant, collection, adding_chunks):
        if collection == "two":
            second_admitting.set()
        with original_lock(tenant, collection, adding_chunks) as budget:
            yield budget

    monkeypatch.setattr(store, "tenant_chunk_count", counted)
    monkeypatch.setattr(store, "_tenant_chunk_write_lock", admission)

    def first():
        store.index_records(
            "acme", "one", "d1",
            [("d1::0", "one", {}), ("d1::1", "two", {})],
        )

    def second():
        try:
            if batch_second:
                store.index_documents(
                    "acme", "two", [
                        ("d2", [("d2::0", "three", {})], None),
                        ("d3", [("d3::0", "four", {})], None),
                    ],
                )
            else:
                store.index_records(
                    "acme", "two", "d2",
                    [("d2::0", "three", {}), ("d2::1", "four", {})],
                )
            return "ok"
        except TenantChunkLimitError:
            return "limit"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_result = pool.submit(first)
        assert first_counting.wait(30)
        second_result = pool.submit(second)
        try:
            assert second_admitting.wait(30)
            assert not second_counting.wait(0.1)
        finally:
            release_first.set()
        first_result.result(timeout=30)
        assert second_result.result(timeout=30) == "limit"

    assert store.tenant_chunk_count("acme") == 2


@pytest.mark.parametrize("missing", [False, True])
def test_unavailable_count_refuses_admission_and_preserves_documents(
    tmp_path, monkeypatch, missing
):
    get_cfg().set("tenants.default_max_chunks_per_tenant", 3)
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.index_records("acme", "one", "d1", [("d1::0", "one", {})])
    store.index_records("acme", "two", "d2", [("d2::0", "two", {})])

    if missing:
        store._db_path("acme", "two").unlink()
    else:
        original_counts = CollectionDB.get_doc_chunk_counts
        unreadable_path = store._db_path("acme", "two").resolve()

        def unreadable(self):
            if self.path == unreadable_path:
                raise sqlite3.OperationalError("database is locked")
            return original_counts(self)

        monkeypatch.setattr(CollectionDB, "get_doc_chunk_counts", unreadable)

    with pytest.raises(RuntimeError, match="chunk count unavailable"):
        store.index_records(
            "acme", "one", "d1",
            [("d1::0", "replacement", {}), ("d1::1", "extra", {})],
        )
    assert any(
        hit.id == "d1::0" for hit in store.search("acme", "one", "one", k=5)
    )


def test_concurrent_collection_delete_still_admits_an_ingest(
    tmp_path, monkeypatch
):
    # delete_collection removes the collection directory before it
    # unregisters the catalog row, so a tenant count that lists the row and
    # then looks at the directory straddles a window where neither answer is
    # final. Reading that window must not fail an unrelated ingest.
    get_cfg().set("tenants.default_max_chunks_per_tenant", 2)
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.index_records("acme", "one", "d1", [("d1::0", "one", {})])
    store.index_records("acme", "two", "d2", [("d2::0", "two", {})])

    removed = Event()
    counting = Event()
    release = Event()
    original_remove = LocalStore._remove_path
    original_strict = store._read_doc_chunk_counts_strict

    def paused_remove(path, *, retries=4):
        original_remove(path, retries=retries)
        removed.set()
        assert release.wait(30)

    def counted(tenant, collection):
        if collection == "two":
            counting.set()
        return original_strict(tenant, collection)

    monkeypatch.setattr(store, "_remove_path", paused_remove)
    monkeypatch.setattr(store, "_read_doc_chunk_counts_strict", counted)

    with ThreadPoolExecutor(max_workers=2) as pool:
        deleting = pool.submit(store.delete_collection, "acme", "two")
        assert removed.wait(30)
        ingesting = pool.submit(
            store.index_records,
            "acme", "one", "d3", [("d3::0", "three", {})],
        )
        try:
            assert counting.wait(30)
        finally:
            release.set()
        deleting.result(timeout=30)
        ingesting.result(timeout=30)

    assert store.tenant_chunk_count("acme") == 2


def test_prebuild_skips_a_collection_still_being_created(tmp_path):
    """The unlocked prebuild can see a new collection's directory before its
    config is written; it must leave recovery to the locked load."""
    store = LocalStore(str(tmp_path), FakeEmbedder())
    Path(store._base_path("acme", "vault")).mkdir(parents=True)
    assert store._prebuild_collection_embedder("acme", "vault") is None
