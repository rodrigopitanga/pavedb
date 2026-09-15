# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import io
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from pave.config import get_cfg
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
    assert any(name.startswith("chunks/") for name in names)
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


def test_collection_archive_restore_replaces_only_target(tmp_path):
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
        "library",
        "old",
        [("0", "old target", {})],
    )

    destination.restore_collection_archive(
        "new-tenant",
        "library",
        archive_bytes,
    )

    assert destination.get_document("new-tenant", "library", "old") is None
    assert destination.get_document("new-tenant", "library", "verne") is not None
    assert destination.search(
        "new-tenant", "library", "submarine", k=1
    )[0].id == "verne::0"
    assert destination.get_document("other", "keep", "sentinel") is not None


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

    with pytest.raises(ValueError, match="no compatible destination embedder"):
        destination.restore_collection_archive("acme", "books", archive_bytes)

    assert destination.get_document("acme", "books", "sentinel") is not None


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
        destination.restore_collection_archive("acme", "books", archive_bytes)

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
    result = client.post(
        "/v1/collections/imported/library/search",
        json={"q": "submarine", "k": 1},
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
