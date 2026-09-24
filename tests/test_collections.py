# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import logging

import pytest

from pave import cli as pvcli
from pave.config import get_cfg
from pave.metadb import (
    CatalogDB,
    COLLECTION_CONFIG_FIELDS,
    COLLECTION_SCHEMA_VERSION,
    CollectionConfigRecoveryError,
    CollectionDB,
)
from pave.service import update_collection
from pave.stores.local import LocalStore
from pave.version import VERSION as PAVEDB_VERSION
from utils import FakeEmbedder


def _set_openai_instance(
    key: str,
    *,
    dim: int,
    model: str = "bge-m3",
    base_url: str = "http://one/v1",
) -> None:
    cfg = get_cfg()
    cfg.set(f"embedder.instances.{key}.type", "openai")
    cfg.set(f"embedder.instances.{key}.model", model)
    cfg.set(f"embedder.instances.{key}.config.dim", dim)
    cfg.set(f"embedder.instances.{key}.config.base_url", base_url)


def _mk_collections_with_store(store, tenant: str, *collections: str):
    for collection in collections:
        store.create_collection(tenant, collection)


def test_create_and_delete_collection(client):
    r = client.post("/v1/collections/acme/invoices")
    assert r.status_code == 201 and r.json()["ok"] is True
    data = r.json()
    assert data["name"] == "invoices"
    assert data["display_name"] == "invoices"
    assert data["embedder_type"] == "sbert"
    assert data["embed_model"] == "fake"
    r2 = client.delete("/v1/collections/acme/invoices")
    assert r2.status_code == 200 and r2.json()["deleted"] == "invoices"


def test_create_collection_accepts_matching_embedder_config(client):
    r = client.post(
        "/v1/collections/acme/contracts",
        json={"embedder_type": "sbert", "embed_model": "fake"},
    )

    assert r.status_code == 201
    data = r.json()
    assert data["ok"] is True
    assert data["name"] == "contracts"
    assert data["display_name"] == "contracts"
    assert data["embedder_type"] == "sbert"
    assert data["embed_model"] == "fake"


def test_create_collection_rejects_invalid_path_name(client):
    r = client.post(
        "/v1/collections/acme/Finance",
    )

    assert r.status_code == 400
    assert r.json()["code"] == "invalid_collection_name"


def test_create_collection_accepts_different_embed_model(client):
    r = client.post(
        "/v1/collections/acme/contracts2",
        json={"embed_model": "sentence-transformers/all-MiniLM-L6-v2"},
    )

    assert r.status_code == 201
    data = r.json()
    assert data["ok"] is True
    assert data["display_name"] == "contracts2"
    assert data["embedder_type"] == "sbert"
    assert data["embed_model"] == "sentence-transformers/all-MiniLM-L6-v2"


def test_create_collection_accepts_different_embedder_type_and_config(client):
    _set_openai_instance("openai-bge-1024", dim=1024)
    r = client.post(
        "/v1/collections/acme/contracts3",
        json={
            "embedder_type": "openai",
            "embed_model": "bge-m3",
            "embedder_config": {"dim": 1024},
        },
    )

    assert r.status_code == 201
    data = r.json()
    assert data["ok"] is True
    assert data["display_name"] == "contracts3"
    assert data["embedder_type"] == "openai"
    assert data["embed_model"] == "bge-m3"
    assert data["embedder_config"] == {"dim": 1024}


def test_create_collection_accepts_embedder_selector_shorthand(client):
    _set_openai_instance("frontier", dim=1024, base_url="http://one/v1")

    response = client.post(
        "/v1/collections/acme/selected",
        json={"embedder": "frontier"},
    )

    assert response.status_code == 201
    data = response.json()
    assert data["embedder_type"] == "openai"
    assert data["embed_model"] == "bge-m3"
    assert data["embedder_config"] == {"dim": 1024}
    detail = client.get("/v1/collections/acme/selected/detail").json()
    assert detail["vector_space_key"] == "openai:bge-m3:dim=1024"
    assert "frontier" not in str(detail)


def test_create_collection_rejects_mixed_embedder_selector_fields(client):
    response = client.post(
        "/v1/collections/acme/mixed",
        json={"embedder": "native", "embedder_type": "sbert"},
    )

    assert response.status_code == 422


def test_create_collection_rejects_unknown_embedder_selector(client):
    response = client.post(
        "/v1/collections/acme/unknown-selector",
        json={"embedder": "missing"},
    )

    assert response.status_code == 400
    data = response.json()
    assert data["code"] == "invalid_embedder_config"
    assert "did not match any configured instance" in data["error"]


def test_create_collection_rejects_unknown_embedder_config_key(client):
    r = client.post(
        "/v1/collections/acme/secret-config",
        json={"embedder_config": {"api_key": "secret"}},
    )

    assert r.status_code == 400
    data = r.json()
    assert data["code"] == "invalid_embedder_config"
    assert "api_key" in data["error"]


def test_get_collection_detail_empty_collection(client):
    create = client.post("/v1/collections/acme/detail-empty")
    assert create.status_code == 201

    r = client.get("/v1/collections/acme/detail-empty/detail")

    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["tenant"] == "acme"
    assert data["name"] == "detail-empty"
    assert data["display_name"] == "detail-empty"
    assert data["embedder_type"] == "sbert"
    assert data["embed_model"] == "fake"
    assert data["created_at"].endswith("Z")
    assert data["pavedb_version"] == PAVEDB_VERSION
    assert data["schema_version"] == COLLECTION_SCHEMA_VERSION
    assert data["doc_count"] == 0
    assert data["chunk_count"] == 0


def test_get_collection_detail_after_ingest(client):
    create = client.post("/v1/collections/acme/detail-ingest")
    assert create.status_code == 201

    upload = client.post(
        "/v1/collections/acme/detail-ingest/documents",
        files={"file": ("a.txt", b"hello detail world", "text/plain")},
        data={"docid": "DOC-1"},
    )
    assert upload.status_code == 201

    r = client.get("/v1/collections/acme/detail-ingest/detail")

    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["doc_count"] == 1
    assert data["chunk_count"] > 0


def test_get_collection_detail_not_found(client):
    r = client.get("/v1/collections/acme/missing-detail/detail")

    assert r.status_code == 404
    data = r.json()
    assert data["code"] == "collection_not_found"
    assert "missing-detail" in data["error"]


def test_update_collection_display_name(client):
    create = client.post("/v1/collections/acme/display-docs")
    assert create.status_code == 201

    r = client.patch(
        "/v1/collections/acme/display-docs",
        json={"display_name": "Display Docs"},
    )

    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["name"] == "display-docs"
    assert data["display_name"] == "Display Docs"

    detail = client.get("/v1/collections/acme/display-docs/detail")
    assert detail.status_code == 200
    assert detail.json()["display_name"] == "Display Docs"


def test_update_collection_rejects_empty_display_name(client):
    create = client.post("/v1/collections/acme/blank-display")
    assert create.status_code == 201

    r = client.patch(
        "/v1/collections/acme/blank-display",
        json={"display_name": "  "},
    )

    assert r.status_code == 400
    assert r.json()["code"] == "invalid_display_name"


def test_update_collection_not_found(client):
    r = client.patch(
        "/v1/collections/acme/missing-update",
        json={"display_name": "Whatever"},
    )
    assert r.status_code == 404
    assert r.json()["code"] == "collection_not_found"


def test_update_collection_vanished_after_preflight():
    class VanishingStore:
        def get_collection_config(self, tenant, name):
            return {"name": name}

        def update_collection(self, tenant, name, *, display_name=None):
            return None

    result = update_collection(
        VanishingStore(),
        "acme",
        "vanished",
        display_name="Vanished",
    )

    assert result["ok"] is False
    assert result["code"] == "collection_not_found"
    assert result["error_type"] == "not_found"


def test_delete_collection_not_found(client):
    r = client.delete("/v1/collections/acme/missing-delete")
    assert r.status_code == 404
    assert r.json()["code"] == "collection_not_found"


def test_list_collections_api_sorted(client, tmp_path, monkeypatch):
    store = client.app.state.store.impl
    monkeypatch.setattr(store, "_data_dir", str(tmp_path))

    client.post("/v1/collections/acme/invoices")
    client.post("/v1/collections/acme/contracts")
    client.post("/v1/collections/acme/reports")

    r = client.get("/v1/collections/acme")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["tenant"] == "acme"
    assert [c["name"] for c in data["collections"]] == [
        "contracts",
        "invoices",
        "reports",
    ]
    assert [c["display_name"] for c in data["collections"]] == [
        "contracts",
        "invoices",
        "reports",
    ]
    assert [c["embedder_label"] for c in data["collections"]] == [
        "sbert:fake",
        "sbert:fake",
        "sbert:fake",
    ]
    assert data["count"] == 3


def test_list_collections_api_empty_tenant(client, tmp_path, monkeypatch):
    store = client.app.state.store.impl
    monkeypatch.setattr(store, "_data_dir", str(tmp_path))

    # Create tenant dir but no collections
    (tmp_path / "t_empty").mkdir(parents=True, exist_ok=True)

    r = client.get("/v1/collections/empty")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["collections"] == []
    assert data["count"] == 0


def test_list_collections_ignores_legacy_catalog_only(client, tmp_path, monkeypatch):
    store = client.app.state.store.impl
    monkeypatch.setattr(store, "_data_dir", str(tmp_path))
    coll_dir = tmp_path / "t_acme" / "c_legacy"
    coll_dir.mkdir(parents=True, exist_ok=True)
    (coll_dir / "catalog.json").write_text("{}", encoding="utf-8")

    r = client.get("/v1/collections/acme")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["collections"] == []
    assert data["count"] == 0


def test_list_collections_api_nonexistent_tenant(client, tmp_path, monkeypatch):
    store = client.app.state.store.impl
    monkeypatch.setattr(store, "_data_dir", str(tmp_path))

    r = client.get("/v1/collections/nonexistent")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["collections"] == []
    assert data["count"] == 0


def test_list_collections_cli(tmp_path, capsys, monkeypatch):
    cfg = get_cfg()
    monkeypatch.setattr(cfg, "_cfg", {**cfg._cfg, "data_dir": str(tmp_path)})

    # Monkeypatch the store in cli module to use the new config
    store = LocalStore(str(tmp_path), FakeEmbedder())
    monkeypatch.setattr(pvcli, "store", store)

    _mk_collections_with_store(store, "demo", "books", "articles")

    pvcli.main_cli(["list-collections", "demo"])
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["tenant"] == "demo"
    assert [c["name"] for c in out["collections"]] == [
        "articles",
        "books",
    ]
    assert [c["embedder_label"] for c in out["collections"]] == [
        "sbert:fake",
        "sbert:fake",
    ]
    assert all(
        c["vector_space_key"] == "sbert:fake:" for c in out["collections"]
    )
    assert out["count"] == 2


def test_catalog_rebuild_recovers_legacy_collection_without_config(tmp_path):
    collection_dir = tmp_path / "t_acme" / "c_bootstrap"
    db = CollectionDB()
    db.open(collection_dir / "meta.db")
    db.close()

    store = LocalStore(str(tmp_path), FakeEmbedder())

    assert store.list_tenants() == ["acme"]
    config = store.get_collection_config("acme", "bootstrap")
    assert config is not None
    assert config["backend_type"] is None
    assert config["embedder_type"] is None
    assert config["embed_model"] is None
    assert not (collection_dir / "collection.json").exists()


def test_catalog_rebuild_rejects_malformed_collection_config(tmp_path):
    collection_dir = tmp_path / "t_acme" / "c_bootstrap"
    db = CollectionDB()
    db.open(collection_dir / "meta.db")
    db.close()
    (collection_dir / "collection.json").write_text("{", encoding="utf-8")

    with pytest.raises(CollectionConfigRecoveryError, match="collection config"):
        LocalStore(str(tmp_path), FakeEmbedder())


def test_catalog_rebuild_preserves_collection_embedder_config(tmp_path):
    store = LocalStore(str(tmp_path), FakeEmbedder(dim=8))
    store.create_collection(
        "acme",
        "vectors",
        embedder_type="identity",
        embedder_config={"dim": 4},
    )
    store._flush_caches()
    store._catalog_db_path().unlink()

    recovered = LocalStore(str(tmp_path), FakeEmbedder(dim=8))
    recovered.create_collection("acme", "vectors")

    config = recovered.get_collection_config("acme", "vectors")
    assert config is not None
    assert config["embedder_type"] == "identity"
    assert config["embedder_config"] == {"dim": 4}
    assert recovered._collection_embedders[("acme", "vectors")].dim == 4


def test_catalog_backfills_legacy_custom_embedder_config(tmp_path):
    _set_openai_instance("legacy-custom", dim=4)

    def make_embedder(_embedder_type, _embed_model, embedder_config):
        return FakeEmbedder(dim=int(embedder_config["dim"]))

    collection_dir = tmp_path / "t_acme" / "c_legacy"
    sidecar = collection_dir / "collection.json"
    store = LocalStore(
        str(tmp_path),
        FakeEmbedder(dim=8),
        embedder_factory=make_embedder,
    )
    store.create_collection(
        "acme",
        "legacy",
        embedder_type="openai",
        embed_model="bge-m3",
        embedder_config={"dim": 4},
    )
    store.index_records(
        "acme",
        "legacy",
        "doc",
        [("doc::0", "legacy vector space", {})],
    )
    sidecar.unlink()
    store.close()

    upgraded = LocalStore(
        str(tmp_path),
        FakeEmbedder(dim=8),
        embedder_factory=make_embedder,
    )
    assert json.loads(sidecar.read_text(encoding="utf-8"))[
        "embedder_config"
    ] == {"dim": 4}
    assert upgraded.get_collection_vector_dim("acme", "legacy") == 4
    assert upgraded.search("acme", "legacy", "legacy", k=1)[0].id == "doc::0"
    upgraded.close()

    (tmp_path / "catalog.db").unlink()
    recovered = LocalStore(
        str(tmp_path),
        FakeEmbedder(dim=8),
        embedder_factory=make_embedder,
    )
    config = recovered.get_collection_config("acme", "legacy")
    assert config is not None
    assert {
        key: config[key]
        for key in COLLECTION_CONFIG_FIELDS
    } == json.loads(sidecar.read_text(encoding="utf-8"))
    assert config["embedder_type"] == "openai"
    assert config["embed_model"] == "bge-m3"
    assert config["embedder_config"] == {"dim": 4}
    assert recovered.get_collection_vector_dim("acme", "legacy") == 4
    assert recovered.search("acme", "legacy", "legacy", k=1)[0].id == "doc::0"


def test_local_store_accepts_injected_catalog_db(tmp_path):
    cat_db = CatalogDB()
    store = LocalStore(str(tmp_path), FakeEmbedder(), cat_db=cat_db)

    store.create_collection("acme", "docs")

    assert cat_db._conn is not None
    assert cat_db.list_tenants() == ["acme"]
    assert cat_db.list_collections("acme") == ["docs"]


def test_local_store_logs_configured_embedder_registry(tmp_path, caplog):
    _set_openai_instance("frontier-affordable", dim=1536)

    with caplog.at_level(logging.INFO, logger="pave"):
        LocalStore(str(tmp_path), FakeEmbedder())

    assert (
        "native/sbert:fake:" in caplog.text
        and "frontier-affordable/openai:bge-m3:dim=1536" in caplog.text
    )


def test_local_store_round_robins_compatible_instances(tmp_path):
    cfg = get_cfg()
    cfg.set("embedder.routing.policy", "round_robin")
    _set_openai_instance("openai-a", dim=8, base_url="http://a/v1")
    _set_openai_instance("openai-b", dim=8, base_url="http://b/v1")
    created: list[str] = []

    def make_embedder(_embedder_type, _embed_model, embedder_config):
        created.append(str(embedder_config["base_url"]))
        return FakeEmbedder(dim=8)

    store = LocalStore(
        str(tmp_path),
        FakeEmbedder(),
        embedder_factory=make_embedder,
    )

    first = store.create_collection("acme", "first", embedder="openai-a")
    second = store.create_collection("acme", "second", embedder="openai-a")

    assert created == ["http://a/v1", "http://b/v1"]
    assert first["embedder_config"] == {"dim": 8}
    assert second["embedder_config"] == {"dim": 8}


def test_local_store_failover_tries_next_compatible_instance(tmp_path):
    cfg = get_cfg()
    cfg.set("embedder.routing.policy", "failover")
    _set_openai_instance("openai-a", dim=8, base_url="http://a/v1")
    _set_openai_instance("openai-b", dim=8, base_url="http://b/v1")
    attempts: list[str] = []

    def make_embedder(_embedder_type, _embed_model, embedder_config):
        base_url = str(embedder_config["base_url"])
        attempts.append(base_url)
        if base_url == "http://a/v1":
            raise RuntimeError("primary unavailable")
        return FakeEmbedder(dim=8)

    store = LocalStore(
        str(tmp_path),
        FakeEmbedder(),
        embedder_factory=make_embedder,
    )

    created = store.create_collection("acme", "docs", embedder="openai-a")

    assert attempts == ["http://a/v1", "http://b/v1"]
    assert created["embedder_config"] == {"dim": 8}


def test_local_store_uses_collection_embedder_specs(tmp_path):
    _set_openai_instance("openai-small", dim=8)
    _set_openai_instance("openai-large", dim=16)
    created: list[tuple[str, str, dict]] = []

    def make_embedder(
        embedder_type: str,
        embed_model: str,
        embedder_config: dict,
    ):
        created.append((embedder_type, embed_model, dict(embedder_config)))
        return FakeEmbedder(dim=int(embedder_config["dim"]))

    store = LocalStore(
        str(tmp_path),
        FakeEmbedder(),
        embedder_factory=make_embedder,
    )

    spec = {
        "embedder_type": "openai",
        "embed_model": "bge-m3",
        "embedder_config": {"dim": 8},
    }
    store.create_collection("acme", "small", **spec)
    store.create_collection(
        "acme",
        "large",
        embedder_type="openai",
        embed_model="bge-m3",
        embedder_config={"dim": 16},
    )
    store.create_collection("acme", "small2", **spec)

    assert store._collection_embedders[("acme", "small")].dim == 8
    assert store._collection_embedders[("acme", "large")].dim == 16
    assert (
        store._collection_embedders[("acme", "small2")]
        is store._collection_embedders[("acme", "small")]
    )
    assert created == [
        ("openai", "bge-m3", {"dim": 8, "base_url": "http://one/v1"}),
        ("openai", "bge-m3", {"dim": 16, "base_url": "http://one/v1"}),
    ]


def test_vector_space_key_on_detail_default_sbert(client):
    client.post("/v1/collections/acme/space-default")
    detail = client.get("/v1/collections/acme/space-default/detail").json()
    assert detail["vector_space_key"] == "sbert:fake:"


def test_vector_space_key_on_listing(client):
    client.post("/v1/collections/acme/space-a")
    client.post(
        "/v1/collections/acme/space-b",
        json={"embed_model": "sentence-transformers/all-MiniLM-L6-v2"},
    )
    data = client.get("/v1/collections/acme").json()
    by_name = {c["name"]: c for c in data["collections"]}
    assert by_name["space-a"]["vector_space_key"] == "sbert:fake:"
    assert (
        by_name["space-b"]["vector_space_key"]
        == "sbert:sentence-transformers/all-MiniLM-L6-v2:"
    )


def test_vector_space_key_reflects_dim_but_not_base_url(client):
    # base_url is operational (keeps the key); dim is geometric (changes it).
    _set_openai_instance("openai-a", dim=1024, base_url="http://a/v1")
    _set_openai_instance("openai-b", dim=1024, base_url="http://b/v1")
    _set_openai_instance("openai-c", dim=1536, base_url="http://a/v1")

    def key_for(name, dim):
        r = client.post(
            f"/v1/collections/acme/{name}",
            json={
                "embedder_type": "openai",
                "embed_model": "bge-m3",
                "embedder_config": {"dim": dim},
            },
        )
        assert r.status_code == 201
        return client.get(
            f"/v1/collections/acme/{name}/detail"
        ).json()["vector_space_key"]

    key_a = key_for("openai-a", 1024)
    key_b = key_for("openai-b", 1024)
    key_c = key_for("openai-c", 1536)

    assert key_a == "openai:bge-m3:dim=1024"
    assert key_a == key_b  # base_url differs -> same vector space
    assert key_a != key_c  # dim differs -> different vector space


def test_embedder_unavailable_on_create(tmp_path):
    from pave.embedders import EmbedderUnavailableError

    _set_openai_instance("openai-broken", dim=1024)

    def failing_factory(etype, model, cfg):
        raise EmbedderUnavailableError("OPENAI_API_KEY not set")

    store = LocalStore(
        str(tmp_path),
        FakeEmbedder(),
        embedder_factory=failing_factory,
    )
    from pave.service import create_collection as svc_create_collection

    result = svc_create_collection(
        store,
        "acme",
        "broken",
        embedder_type="openai",
        embed_model="bge-m3",
        embedder_config={"dim": 1024},
    )
    assert result["ok"] is False
    assert result["code"] == "embedder_unavailable"
    assert store.list_collections("acme") == []
    assert not (tmp_path / "t_acme" / "c_broken").exists()


def test_create_collection_reports_vector_space_config_for_opaque_store():
    """A store outside the CollectionInfo shape must not widen the response.

    The service falls back to resolving the spec itself, and that spec is the
    runtime one: it carries operational settings such as `base_url` that never
    belong in a collection's embedder_config.
    """
    _set_openai_instance("openai-secret", dim=1024, base_url="http://internal/v1")

    class _OpaqueStore:
        def create_collection(self, tenant, name, **kwargs):
            return None

    from pave.service import create_collection as svc_create_collection

    result = svc_create_collection(
        _OpaqueStore(),
        "acme",
        "opaque",
        embedder="openai-secret",
    )

    assert result["ok"] is True
    assert result["embedder_type"] == "openai"
    assert result["embed_model"] == "bge-m3"
    assert result["embedder_config"] == {"dim": 1024}


def test_create_builds_the_embedder_outside_the_state_lock(tmp_path):
    import threading

    _set_openai_instance("slow-build", dim=4)
    building = threading.Event()
    release = threading.Event()

    def make_embedder(_embedder_type, _embed_model, embedder_config):
        building.set()
        assert release.wait(timeout=5)
        return FakeEmbedder(dim=int(embedder_config["dim"]))

    store = LocalStore(
        str(tmp_path), FakeEmbedder(dim=8), embedder_factory=make_embedder
    )
    creator = threading.Thread(
        target=store.create_collection,
        args=("acme", "slow"),
        kwargs={
            "embedder_type": "openai",
            "embed_model": "bge-m3",
            "embedder_config": {"dim": 4},
        },
    )
    creator.start()
    try:
        assert building.wait(timeout=5)
        # An archive dump/restore takes the state write lock; a model load
        # must not hold it off.
        got_write_lock = threading.Event()

        def take_write_lock():
            with store._state_lock.write_lock():
                got_write_lock.set()

        writer = threading.Thread(target=take_write_lock)
        writer.start()
        assert got_write_lock.wait(timeout=2)
        writer.join()
    finally:
        release.set()
        creator.join(timeout=5)
    assert store.get_collection_vector_dim("acme", "slow") == 4
    store.close()


def test_concurrent_cold_loads_build_the_embedder_once(tmp_path):
    import threading
    import time

    _set_openai_instance("shared-build", dim=4)
    builds = []
    release = threading.Event()

    def make_embedder(_embedder_type, _embed_model, embedder_config):
        builds.append(1)
        assert release.wait(timeout=5)
        return FakeEmbedder(dim=int(embedder_config["dim"]))

    spec = {
        "embedder_type": "openai",
        "embed_model": "bge-m3",
        "embedder_config": {"dim": 4},
    }
    first = LocalStore(
        str(tmp_path), FakeEmbedder(dim=8), embedder_factory=lambda *a: (
            FakeEmbedder(dim=4)
        ),
    )
    first.create_collection("acme", "cold", **spec)
    first.close()

    store = LocalStore(
        str(tmp_path), FakeEmbedder(dim=8), embedder_factory=make_embedder
    )
    searches = [
        threading.Thread(target=store.search, args=("acme", "cold", "q"))
        for _ in range(4)
    ]
    for thread in searches:
        thread.start()
    # Hold the first build open long enough for the others to arrive; without
    # single-flight each of them would call the factory in this window.
    deadline = time.monotonic() + 0.5
    while len(builds) < len(searches) and time.monotonic() < deadline:
        time.sleep(0.01)
    release.set()
    for thread in searches:
        thread.join(timeout=10)
    assert len(builds) == 1
    store.close()
