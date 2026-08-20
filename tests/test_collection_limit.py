# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
from concurrent.futures import ThreadPoolExecutor

from pave.config import get_cfg
from pave.metadb import CollectionLimitError
from pave.stores.local import LocalStore
from utils import FakeEmbedder


def test_collection_limit_enforced_over_http(client):
    # Fresh tenants: the session data dir keeps collections from other tests.
    get_cfg().set("tenants.default_max_collections", 2)
    assert client.post("/v1/collections/vandelay/one").status_code == 201
    assert client.post("/v1/collections/vandelay/two").status_code == 201

    r = client.post("/v1/collections/vandelay/three")
    assert r.status_code == 429
    assert r.json()["code"] == "collection_limit_reached"

    # Idempotent re-create of an existing collection stays allowed at the cap,
    # and other tenants get their own count.
    assert client.post("/v1/collections/vandelay/two").status_code == 201
    assert client.post("/v1/collections/wentworth/one").status_code == 201

    # Deleting frees a slot.
    assert client.delete("/v1/collections/vandelay/one").status_code == 200
    assert client.post("/v1/collections/vandelay/three").status_code == 201


def test_search_cannot_mint_collections_past_the_limit(client):
    """B33: read-path auto-creation registered with the cap disabled."""
    get_cfg().set("tenants.default_max_collections", 1)
    assert client.post("/v1/collections/kruger/one").status_code == 201

    for name in ("two", "three"):
        r = client.post(
            f"/v1/collections/kruger/{name}/search", json={"q": "x", "k": 1}
        )
        assert r.status_code == 200, (name, r.text)

    r = client.get("/v1/collections/kruger")
    assert [c["name"] for c in r.json()["collections"]] == ["one"]


def test_implicit_create_on_ingest_is_capped(client):
    """The implicit create inside ingest registered with the cap disabled."""
    get_cfg().set("tenants.default_max_collections", 1)
    assert client.post("/v1/collections/pendant/one").status_code == 201

    r = client.post(
        "/v1/collections/pendant/two/documents",
        json={"text": "hello", "docid": "d1", "metadata": None},
    )
    assert r.status_code == 429, r.text
    assert r.json()["code"] == "collection_limit_reached"
    # A refused registration must not leave the data directory behind.
    store = client.app.state.store.impl
    assert not os.path.isdir(store._base_path("pendant", "two"))


def test_per_tenant_override_beats_default(client):
    # Fresh tenants: the session data dir keeps collections from other tests.
    cfg = get_cfg()
    cfg.set("tenants.default_max_collections", 1)
    cfg.set("tenants.initech.max_collections", 2)
    cfg.set("tenants.hooli.max_collections", 0)

    assert client.post("/v1/collections/initech/one").status_code == 201
    assert client.post("/v1/collections/initech/two").status_code == 201
    assert client.post("/v1/collections/initech/three").status_code == 429

    # 0 override disables the cap even with a global default set.
    assert client.post("/v1/collections/hooli/one").status_code == 201
    assert client.post("/v1/collections/hooli/two").status_code == 201


def test_concurrent_creates_cannot_exceed_limit(tmp_path):
    get_cfg().set("tenants.default_max_collections", 3)
    store = LocalStore(str(tmp_path), FakeEmbedder())

    def create(i: int) -> str:
        try:
            store.create_collection("acme", f"c{i}")
            return "ok"
        except CollectionLimitError:
            return "limit"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(create, range(8)))

    assert outcomes.count("ok") == 3
    assert outcomes.count("limit") == 5
    assert len(store.list_collections("acme")) == 3
