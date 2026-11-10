# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import io
import json
import shutil
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from pave.config import get_cfg
from pave.metadb import (
    COLLECTION_SCHEMA_VERSION,
    CollectionExistsError,
    CollectionLimitError,
    TenantChunkLimitError,
)
from pave.stores.local import LocalStore
from pave.version import VERSION
from utils import FakeEmbedder


def _archive_bytes(
    store: LocalStore,
    tenant: str,
    collection: str,
) -> bytes:
    archive_path, tmp_dir = store.dump_collection_archive(tenant, collection)
    try:
        return Path(archive_path).read_bytes()
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _replace_manifest(archive_bytes: bytes, **updates) -> bytes:
    source = zipfile.ZipFile(io.BytesIO(archive_bytes))
    target_bytes = io.BytesIO()
    with source, zipfile.ZipFile(target_bytes, "w") as target:
        for info in source.infolist():
            content = source.read(info.filename)
            if info.filename == "manifest.json":
                manifest = json.loads(content)
                manifest.update(updates)
                content = json.dumps(manifest).encode()
            target.writestr(info, content)
    return target_bytes.getvalue()


def test_collection_archive_manifest_and_members(tmp_path):
    store = LocalStore(str(tmp_path / "data"), FakeEmbedder())
    store.create_collection("acme", "books", display_name="Books")
    store.index_records(
        "acme",
        "books",
        "verne",
        [("0", "Captain Nemo", {"lang": "en"})],
    )

    with zipfile.ZipFile(io.BytesIO(_archive_bytes(store, "acme", "books"))) as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("manifest.json"))

    assert {"manifest.json", "collection.json", "meta.db"} <= names
    assert "catalog.db" not in names
    assert {"index/faiss.index", "index/id_map.json"} <= names
    assert not any(name.startswith("chunks/") for name in names)
    assert manifest["archive_format"] == "pavedb.collection"
    assert manifest["archive_version"] == 1
    assert manifest["pavedb_version"] == VERSION
    assert isinstance(manifest["schema_version"], int)
    assert manifest["source"] == {"tenant": "acme", "collection": "books"}
    assert manifest["collection"]["display_name"] == "Books"
    assert manifest["embedder_spec"] == {
        "embedder_type": "sbert",
        "embed_model": "fake",
        "embedder_config": {},
    }


def test_collection_archive_restores_into_a_new_slot_only(tmp_path):
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    source.index_records(
        "acme",
        "books",
        "verne",
        [("0", "Captain Nemo submarine", {})],
    )
    archive_bytes = _archive_bytes(source, "acme", "books")

    destination = LocalStore(str(tmp_path / "destination"), FakeEmbedder())
    destination.index_records(
        "other",
        "keep",
        "sentinel",
        [("0", "do not replace", {})],
    )
    destination.index_records(
        "new-tenant",
        "taken",
        "old",
        [("0", "old target", {})],
    )

    with pytest.raises(CollectionExistsError):
        destination.restore_collection_archive(
            "new-tenant", "taken", archive_bytes,
        )
    assert destination.get_document("new-tenant", "taken", "old") is not None

    destination.restore_collection_archive(
        "new-tenant",
        "library",
        archive_bytes,
    )

    assert destination.get_document("new-tenant", "library", "verne") is not None
    assert destination.search(
        "new-tenant",
        "library",
        "submarine",
        k=1,
        content_filter={"op": "contains", "value": "Nemo sub"},
    )[0].id == "verne::0"
    assert destination.get_document("other", "keep", "sentinel") is not None


def test_collection_archive_restore_counts_against_the_limit(tmp_path, cfg):
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    source.index_records("acme", "books", "verne", [("0", "Nemo", {})])
    archive_bytes = _archive_bytes(source, "acme", "books")
    cfg.set("tenants.default_max_collections", 1)
    destination = LocalStore(str(tmp_path / "destination"), FakeEmbedder())
    destination.create_collection("acme", "first")

    with pytest.raises(CollectionLimitError):
        destination.restore_collection_archive("acme", "second", archive_bytes)
    assert not (tmp_path / "destination" / "acme" / "second").exists()


def test_collection_archive_restore_tags_the_destination_row(tmp_path):
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    source.index_records(
        "acme", "books", "verne", [("0", "Captain Nemo", {})]
    )
    archive_bytes = _archive_bytes(source, "acme", "books")

    destination = LocalStore(str(tmp_path / "destination"), FakeEmbedder())
    destination.index_records(
        "acme", "books", "sentinel", [("0", "replace me", {})]
    )

    destination.restore_collection_archive(
        "acme", "books", archive_bytes, replace=True,
    )
    destination.restore_collection_archive("imported", "library", archive_bytes)

    # Restore is same-version only, so a collection that travelled reports
    # the tags a locally created one reports instead of reading as a row
    # that predates them (P2-25).
    for tenant, name in (("acme", "books"), ("imported", "library")):
        detail = destination.get_collection_detail(tenant, name)
        assert detail["pavedb_version"] == VERSION
        assert detail["schema_version"] == COLLECTION_SCHEMA_VERSION


def test_collection_archive_rejects_version_before_replacement(tmp_path):
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    source.index_records(
        "acme", "books", "verne", [("0", "archive text", {})]
    )
    archive_bytes = _replace_manifest(
        _archive_bytes(source, "acme", "books"),
        pavedb_version="999.0",
    )
    destination = LocalStore(str(tmp_path / "destination"), FakeEmbedder())
    destination.index_records(
        "acme", "books", "sentinel", [("0", "keep me", {})]
    )

    with pytest.raises(ValueError, match="PaveDB version mismatch"):
        destination.restore_collection_archive("acme", "books", archive_bytes)

    assert destination.get_document("acme", "books", "sentinel") is not None


def test_collection_archive_resolves_embedder_before_replacement(tmp_path):
    cfg = get_cfg()
    cfg.set(
        "embedder.instances",
        {
            "archive-text": {
                "type": "openai",
                "model": "bge-m3",
                "config": {"dim": 4, "base_url": "https://archive.invalid"},
                "api_key": "not-archived",
            }
        },
    )
    cfg.set("embedder.default", "archive-text")
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder(dim=4))
    source.index_records(
        "acme", "books", "verne", [("0", "archive text", {})]
    )
    archive_bytes = _archive_bytes(source, "acme", "books")
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        assert all(
            b"not-archived" not in archive.read(name)
            for name in archive.namelist()
            if not name.endswith("/")
        )

    cfg.set(
        "embedder.instances",
        {
            "native": {
                "type": "sbert",
                "model": "fake",
                "config": {"runtime": "auto"},
            }
        },
    )
    cfg.set("embedder.default", "native")
    destination = LocalStore(str(tmp_path / "destination"), FakeEmbedder())
    destination.index_records(
        "acme", "books", "sentinel", [("0", "keep me", {})]
    )

    with pytest.raises(ValueError, match="not configured"):
        destination.restore_collection_archive("acme", "books", archive_bytes)

    assert destination.get_document("acme", "books", "sentinel") is not None


@pytest.mark.parametrize("embedder_type", ["sbert", "openai"])
@pytest.mark.parametrize("replace", [False, True])
def test_tenant_archive_rejects_unconfigured_model(
    client, cfg, tmp_path, embedder_type, replace,
):
    cfg.set("embedder.instances.native.type", embedder_type)
    cfg.set("embedder.instances.native.model", "unconfigured-model")
    cfg.set(
        "embedder.instances.native.config",
        {"dim": 64} if embedder_type == "openai" else {},
    )
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    source.index_records("source", "books", "new", [("0", "archive", {})])
    archive = _archive_bytes(source, "source", "books")
    cfg.set("embedder.instances.native.model", "fake")
    store = client.app.state.store
    if replace:
        store.index_records("acme", "books", "old", [("0", "keep", {})])
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", "root")
    cfg.set("auth.api_keys", {"acme": "tenant-key"})

    response = client.request(
        "PUT" if replace else "POST",
        "/v1/collections/acme/books/archive",
        headers={"Authorization": "Bearer tenant-key"},
        files={"file": ("books.zip", archive, "application/zip")},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "collection_archive_invalid"
    assert "not configured" in response.json()["error"]
    if replace:
        assert store.get_document("acme", "books", "old") is not None
    else:
        assert store.get_collection_detail("acme", "books") is None


@pytest.mark.parametrize("replace", [False, True])
@pytest.mark.parametrize("override", [False, True])
@pytest.mark.parametrize(
    ("field", "limit", "code"),
    [
        ("max_chunks_per_collection", 1, "chunk_limit_reached"),
        ("max_chunks_per_tenant", 2, "tenant_chunk_limit_reached"),
    ],
)
def test_tenant_archive_enforces_chunk_limits(
    client, cfg, tmp_path, replace, override, field, limit, code,
):
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    source.index_records(
        "source", "books", "new", [("0", "one", {}), ("1", "two", {})],
    )
    archive = _archive_bytes(source, "source", "books")
    store = client.app.state.store
    store.index_records("acme", "other", "keep", [("0", "other", {})])
    if replace:
        store.index_records("acme", "books", "old", [("0", "keep", {})])
    config_key = f"tenants.{'acme.' if override else 'default_'}{field}"
    cfg.set(config_key, limit)
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", "root")
    cfg.set("auth.api_keys", {"acme": "tenant-key"})

    response = client.request(
        "PUT" if replace else "POST",
        "/v1/collections/acme/books/archive",
        headers={"Authorization": "Bearer tenant-key"},
        files={"file": ("books.zip", archive, "application/zip")},
    )

    assert response.status_code == 429
    assert response.json()["code"] == code
    assert store.get_document("acme", "other", "keep") is not None
    if replace:
        assert store.get_document("acme", "books", "old") is not None
    else:
        assert store.get_collection_detail("acme", "books") is None


def test_collection_restore_replacement_uses_incoming_chunk_count(tmp_path, cfg):
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    source.index_records("source", "books", "new", [("0", "new", {})])
    archive = _archive_bytes(source, "source", "books")
    destination = LocalStore(str(tmp_path / "destination"), FakeEmbedder())
    destination.index_records(
        "acme", "books", "old", [("0", "one", {}), ("1", "two", {})],
    )
    destination.index_records("acme", "other", "keep", [("0", "other", {})])
    cfg.set("tenants.default_max_chunks_per_collection", 1)
    cfg.set("tenants.default_max_chunks_per_tenant", 2)

    destination.restore_collection_archive("acme", "books", archive, replace=True)

    assert destination.tenant_chunk_count("acme") == 2
    assert destination.get_document("acme", "books", "new") is not None
    assert destination.get_document("acme", "books", "old") is None


def test_collection_restore_accepts_model_less_vectors(tmp_path):
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    source.create_collection(
        "source", "vectors", embedder_type="identity", embedder_config={"dim": 2},
    )
    source.index_records(
        "source", "vectors", "doc", [("0", "", {})], vectors=[[1.0, 0.0]],
    )
    archive = _archive_bytes(source, "source", "vectors")
    destination = LocalStore(str(tmp_path / "destination"), FakeEmbedder())

    destination.restore_collection_archive("acme", "vectors", archive)

    assert destination.get_document("acme", "vectors", "doc") is not None
    assert destination.tenant_chunk_count("acme") == 1


def test_concurrent_restores_share_the_tenant_chunk_budget(tmp_path, cfg):
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    source.index_records("source", "books", "new", [("0", "new", {})])
    archive = _archive_bytes(source, "source", "books")
    destination = LocalStore(str(tmp_path / "destination"), FakeEmbedder())
    cfg.set("tenants.default_max_chunks_per_tenant", 1)
    start = Barrier(2)

    def restore(name):
        start.wait(timeout=5)
        try:
            destination.restore_collection_archive("acme", name, archive)
        except TenantChunkLimitError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(restore, ["one", "two"]))

    assert sorted(results) == [False, True]
    assert destination.tenant_chunk_count("acme") == 1


def test_restore_and_ingest_share_the_tenant_chunk_budget(tmp_path, cfg):
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    source.index_records(
        "source", "books", "new",
        [("0", "one", {}), ("1", "two", {})],
    )
    archive = _archive_bytes(source, "source", "books")
    destination = LocalStore(str(tmp_path / "destination"), FakeEmbedder())
    cfg.set("tenants.default_max_chunks_per_tenant", 2)
    start = Barrier(2)

    def ingest():
        start.wait(timeout=10)
        try:
            destination.index_records(
                "acme", "live", "doc",
                [("0", "three", {}), ("1", "four", {})],
            )
        except TenantChunkLimitError:
            return False
        return True

    def restore():
        start.wait(timeout=10)
        try:
            destination.restore_collection_archive("acme", "books", archive)
        except TenantChunkLimitError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda fn: fn(), (ingest, restore)))

    assert sorted(results) == [False, True]
    assert destination.tenant_chunk_count("acme") == 2


def test_collection_archive_rolls_back_catalog_publish_failure(
    tmp_path,
    monkeypatch,
):
    source = LocalStore(str(tmp_path / "source"), FakeEmbedder())
    source.index_records(
        "acme", "books", "verne", [("0", "replacement", {})]
    )
    archive_bytes = _archive_bytes(source, "acme", "books")
    destination = LocalStore(str(tmp_path / "destination"), FakeEmbedder())
    destination.index_records(
        "acme", "books", "sentinel", [("0", "keep me", {})]
    )

    def fail_restore(*_args, **_kwargs):
        raise OSError("catalog publish failed")

    monkeypatch.setattr(
        destination._catalog,
        "restore_collection",
        fail_restore,
    )

    with pytest.raises(OSError, match="catalog publish failed"):
        destination.restore_collection_archive(
            "acme", "books", archive_bytes, replace=True,
        )

    assert destination.get_document("acme", "books", "sentinel") is not None
    assert destination.get_document("acme", "books", "verne") is None


def test_collection_archive_http_round_trip(client):
    client.post("/v1/collections/acme/books")
    client.post(
        "/v1/collections/acme/books/documents",
        files={"file": ("book.txt", b"Captain Nemo submarine", "text/plain")},
        data={"docid": "verne"},
    )

    dumped = client.get("/v1/admin/collections/acme/books/archive")
    assert dumped.status_code == 200
    assert dumped.headers["content-type"] == "application/zip"

    restored = client.post(
        "/v1/admin/collections/imported/library/archive",
        files={"file": ("books.zip", dumped.content, "application/zip")},
    )
    assert restored.status_code == 200
    assert restored.json()["tenant"] == "imported"
    assert restored.json()["collection"] == "library"

    archive = {"file": ("books.zip", dumped.content, "application/zip")}
    path = "/v1/admin/collections/imported/{}/archive"
    assert client.post(path.format("library"), files=archive).status_code == 409
    assert client.put(path.format("absent"), files=archive).status_code == 404
    assert client.put(path.format("library"), files=archive).status_code == 200
    result = client.post(
        "/v1/collections/imported/library/search",
        json={
            "q": "submarine",
            "k": 1,
            "content_filter": {"op": "prefix", "value": "captain ne"},
        },
    )
    assert result.status_code == 200
    assert result.json()["matches"][0]["id"] == "verne::chunk_0"


def test_collection_archive_requires_admin(client, cfg):
    client.post("/v1/collections/acme/books")
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", "root")
    cfg.set("auth.api_keys", {"acme": "tenant-key"})

    response = client.get(
        "/v1/admin/collections/acme/books/archive",
        headers={"Authorization": "Bearer tenant-key"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "admin_required"
