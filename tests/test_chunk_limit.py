# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from concurrent.futures import ThreadPoolExecutor

from pave.config import get_cfg
from pave.metadb import ChunkLimitError
from pave.stores.local import LocalStore
from utils import FakeEmbedder


def _ingest(client, tenant, docid, text="hello world"):
    return client.post(
        f"/v1/collections/{tenant}/capped/documents",
        json={"text": text, "docid": docid},
    )


def test_chunk_limit_enforced_over_http(client):
    # Fresh tenants: the session data dir keeps collections from other tests.
    get_cfg().set("tenants.default_max_chunks_per_collection", 2)
    assert _ingest(client, "sunshine", "d1").status_code == 201
    assert _ingest(client, "sunshine", "d2").status_code == 201

    r = _ingest(client, "sunshine", "d3")
    assert r.status_code == 429
    assert r.json()["code"] == "chunk_limit_reached"

    # Replacing an existing document at the cap stays allowed.
    assert _ingest(client, "sunshine", "d2", "replaced").status_code == 201

    # Per-tenant override beats the default; 0 disables the cap.
    get_cfg().set("tenants.moonbeam.max_chunks_per_collection", 0)
    assert _ingest(client, "moonbeam", "d1").status_code == 201
    assert _ingest(client, "moonbeam", "d2").status_code == 201
    assert _ingest(client, "moonbeam", "d3").status_code == 201

    # Deleting a document frees its chunk budget.
    r = client.delete("/v1/collections/sunshine/capped/documents/d1")
    assert r.status_code == 200
    assert _ingest(client, "sunshine", "d3").status_code == 201


def test_rejected_reingest_keeps_existing_document(tmp_path):
    get_cfg().set("tenants.default_max_chunks_per_collection", 2)
    store = LocalStore(str(tmp_path), FakeEmbedder())
    store.index_records("acme", "capped", "d1", [("d1::0", "one", {})])
    store.index_records("acme", "capped", "d2", [("d2::0", "two", {})])

    try:
        store.index_records(
            "acme",
            "capped",
            "d1",
            [("d1::0", "one", {}), ("d1::1", "bis", {})],
        )
        raise AssertionError("expected ChunkLimitError")
    except ChunkLimitError:
        pass

    # The rejected replacement must not have purged the indexed version.
    hits = store.search("acme", "capped", "one", k=5)
    assert any(hit.id == "d1::0" for hit in hits)


def test_concurrent_ingests_cannot_exceed_limit(tmp_path):
    get_cfg().set("tenants.default_max_chunks_per_collection", 3)
    store = LocalStore(str(tmp_path), FakeEmbedder())

    def ingest(i: int) -> str:
        try:
            store.index_records(
                "acme", "capped", f"d{i}", [(f"d{i}::0", f"text {i}", {})]
            )
            return "ok"
        except ChunkLimitError:
            return "limit"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(ingest, range(8)))

    assert outcomes.count("ok") == 3
    assert outcomes.count("limit") == 5
