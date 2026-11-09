# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import os

import pytest

import pave.localclient as localclient_mod
from pave.localclient import (
    LocalClient,
    provider,
)
from pavesdk.errors import (
    Conflict,
    InvalidRequest,
    NotFoundError,
    PaveError,
    Unavailable,
)
from pavesdk.interface import Collection
from pave.config import get_cfg, reload_cfg
from pave.service import ServiceError


def test_local_client_search_common_merge_requires_opt_in(tmp_path, cfg):
    cfg.set("common_enabled", True)
    cfg.set("common_tenant", "global")
    cfg.set("common_collection", "common")

    with provider(tmp_path / "db") as db:
        local = db.create_collection("books")
        db.create_collection("common", tenant="global")

        local.add("captain local", docid="DOC-L")
        db.collection("common", tenant="global").add(
            "captain common",
            docid="DOC-C",
        )

        assert {
            match["meta"]["docid"]
            for match in local.search("captain", k=5)
        } == {"DOC-L"}
        assert {
            match["meta"]["docid"]
            for match in local.search("captain", k=5, include_common=True)
        } == {"DOC-L", "DOC-C"}


def test_provider_target_handling_and_ephemeral_cleanup(tmp_path, monkeypatch):
    persistent = provider(tmp_path / "data")
    try:
        assert isinstance(persistent, LocalClient)
        assert persistent.path == os.fspath(tmp_path / "data")
    finally:
        persistent.close()

    monkeypatch.chdir(tmp_path)
    bare = provider("localhost")
    try:
        assert isinstance(bare, LocalClient)
        assert bare.path == "localhost"
    finally:
        bare.close()

    with pytest.raises(InvalidRequest) as excinfo:
        provider("http://localhost:8080")
    assert excinfo.value.code == "unsupported_scheme"
    assert excinfo.value.message == (
        "network targets are handled by pavesdk.client.connect()"
    )

    ephemeral = provider()
    ephemeral_path = ephemeral.path
    assert os.path.isdir(ephemeral_path)
    ephemeral.close()
    assert not os.path.exists(ephemeral_path)

    with pytest.raises(InvalidRequest) as blank:
        provider("")
    assert blank.value.code == "unsupported_target"


def test_provider_accepts_target_and_tenant(tmp_path):
    persistent = localclient_mod.provider(tmp_path / "data", tenant="acme")
    try:
        assert isinstance(persistent, LocalClient)
        assert persistent.path == os.fspath(tmp_path / "data")
        assert persistent.tenant == "acme"
    finally:
        persistent.close()

    ephemeral = localclient_mod.provider(None, tenant="acme")
    ephemeral_path = ephemeral.path
    try:
        assert ephemeral.tenant == "acme"
        assert os.path.isdir(ephemeral_path)
    finally:
        ephemeral.close()
    assert not os.path.exists(ephemeral_path)


@pytest.mark.parametrize("tenant", ["../escape", "Acme", "a/b", "a\\b", ""])
def test_provider_rejects_invalid_tenant_before_store_creation(tmp_path, tenant):
    path = tmp_path / "data"
    with pytest.raises(InvalidRequest) as excinfo:
        provider(path, tenant=tenant)
    assert excinfo.value.code == "invalid_tenant_name"
    assert not path.exists()

    with pytest.raises(InvalidRequest) as ephemeral_error:
        provider(None, tenant=tenant)
    assert ephemeral_error.value.code == "invalid_tenant_name"


@pytest.mark.parametrize("tenant", ["../escape", "", False, 0])
def test_local_client_rejects_invalid_tenant_override(tmp_path, tenant):
    with provider(tmp_path / "data") as db:
        with pytest.raises(InvalidRequest) as excinfo:
            db.collection("books", tenant=tenant)
        assert excinfo.value.code == "invalid_tenant_name"


def test_local_client_empty_tenant_delete_preserves_default_collection(tmp_path):
    with provider(tmp_path / "data", tenant="acme") as db:
        db.create_collection("books")
        assert db.collection("books", tenant=None).tenant == "acme"
        with pytest.raises(InvalidRequest) as excinfo:
            db.delete_collection("books", tenant="")
        assert excinfo.value.code == "invalid_tenant_name"
        assert [collection.name for collection in db.list_collections()] == ["books"]


def test_provider_target_beats_ambient_config_data_dir(tmp_path, monkeypatch):
    ambient_data = tmp_path / "ambient-data"
    config = tmp_path / "config.yml"
    config.write_text(f"data_dir: '{ambient_data}'\n", encoding="utf-8")
    monkeypatch.setenv("PAVEDB_CONFIG", str(config))
    reload_cfg()
    get_cfg().set("dev", True)
    stores = []

    class CapturingStore:
        def __init__(self, data_dir, *_args, **_kwargs):
            self.data_dir = data_dir
            stores.append(self)

        def warm(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(localclient_mod, "LocalStore", CapturingStore)
    target = tmp_path / "target-data"

    client = provider(target)
    try:
        assert stores[0].data_dir == str(target)
    finally:
        client.close()


def test_provider_warms_store_when_warmup_enabled(tmp_path, monkeypatch):
    cfg = get_cfg()
    cfg.set("dev", True)
    cfg.set("embedder.warmup_default", True)
    stores = []

    class WarmStore:
        def __init__(self, *args, **kwargs):
            self.close_count = 0
            self.warm_count = 0
            stores.append(self)

        def close(self):
            self.close_count += 1

        def warm(self):
            self.warm_count += 1

    monkeypatch.setattr(localclient_mod, "LocalStore", WarmStore)

    client = provider(tmp_path / "data")
    try:
        assert stores[0].warm_count == 1
    finally:
        client.close()
    assert stores[0].close_count == 1


def test_provider_auto_warmup_skips_dev_store(tmp_path, monkeypatch):
    cfg = get_cfg()
    cfg.set("dev", True)
    cfg.set("embedder.warmup_default", None)

    class ColdStore:
        def __init__(self, *args, **kwargs):
            self.close_count = 0

        def close(self):
            self.close_count += 1

        def warm(self):
            raise AssertionError("auto warmup must skip dev local stores")

    monkeypatch.setattr(localclient_mod, "LocalStore", ColdStore)

    client = provider(tmp_path / "data")
    client.close()


def test_local_client_close_delegates_store_close(tmp_path):
    class ClosingStore:
        def __init__(self):
            self.close_count = 0
            self.flush_count = 0

        def close(self):
            self.close_count += 1

        def _flush_caches(self):
            self.flush_count += 1

    store = ClosingStore()
    client = LocalClient(tmp_path / "data", store=store)

    client.close()
    client.close()

    assert store.close_count == 1
    assert store.flush_count == 0


def test_local_byov_vector_round_trip(tmp_path):
    # Identity collection: bulk-ingest raw vectors via the public add_many,
    # search by raw vector through the public Collection surface.
    with provider(tmp_path / "db") as db:
        vecs = db.create_collection(
            "vecs",
            embedder_type="identity",
            embedder_config={"dim": 4},
        )
        added = vecs.add(vector=[1.0, 0.0, 0.0, 0.0], docid="A")
        assert added["docid"] == "A"
        batch = vecs.add_many([
            {"vector": [0.0, 1.0, 0.0, 0.0], "docid": "B"},
        ])
        assert batch["succeeded"] == 1
        assert batch["failed"] == 0

        hits = vecs.search(vector=[1.0, 0.0, 0.0, 0.0], k=2)
        assert hits
        assert hits[0]["meta"]["docid"] == "A"

        # Text on a vector-only collection is rejected.
        with pytest.raises(InvalidRequest):
            vecs.add("plain text", docid="C")


def test_local_client_search_accepts_content_filter(tmp_path):
    with provider(tmp_path / "db") as db:
        books = db.create_collection("books")
        books.add("Captain Nemo commands the Nautilus", docid="BOOK-1")

        hits = db._search(
            "default",
            "books",
            "captain",
            content_filter={"op": "contains", "value": "Nemo"},
        )

        assert [match["meta"]["docid"] for match in hits] == ["BOOK-1"]


def test_collection_surface_returns_clean_data(tmp_path):
    file_path = tmp_path / "book.txt"
    file_path.write_text("Captain Nemo commands the Nautilus", encoding="utf-8")
    archive_path = tmp_path / "dump.zip"

    with provider(tmp_path / "db") as db:
        books = db.create_collection("books", display_name="Books")
        assert isinstance(books, Collection)
        assert books.tenant == "default"
        assert books.name == "books"

        acme = db.create_collection("docs", tenant="acme")
        assert acme.tenant == "acme"
        assert [coll.name for coll in db.list_collections()] == ["books"]
        assert [coll.name for coll in db.list_collections(tenant="acme")] == [
            "docs",
        ]
        assert db.list_tenants() == ["acme", "default"]
        inventory = db.embedders(tenant="acme")
        assert inventory["tenant"] == "acme"
        assert inventory["embedders"] == [
            {
                "key": "native",
                "vector_key": "sbert:fake:",
                "embedder_type": "sbert",
                "embed_model": "fake",
            }
        ]
        assert inventory["default"]["instance_keys"] == ["native"]

        ingest = books.ingest(
            file_path,
            docid="BOOK-1",
            metadata={"kind": "novel"},
        )
        assert ingest == {
            "tenant": "default",
            "collection": "books",
            "docid": "BOOK-1",
            "chunks": 1,
        }

        added = books.add(
            "Submarine captains study ocean charts",
            docid="NOTE-1",
        )
        assert added["docid"] == "NOTE-1"
        batch = books.add_many([
            ("The Nautilus dives beneath the ice.", "NOTE-2", None),
            {
                "text": "Nemo studies ocean currents.",
                "docid": "NOTE-3",
                "metadata": {"kind": "note"},
            },
        ])
        assert batch["succeeded"] == 2
        assert batch["failed"] == 0

        hits = books.search("captain", k=3)
        assert isinstance(hits, list)
        assert hits
        assert "ok" not in hits[0]
        assert "BOOK-1" in {match["meta"]["docid"] for match in hits}

        document = books.get("BOOK-1")
        assert document["docid"] == "BOOK-1"
        assert document["metadata"]["kind"] == "novel"
        note = books.get("NOTE-1")
        assert "filename" not in note["metadata"]

        documents = books.list_documents()
        assert {doc["docid"] for doc in documents} == {
            "BOOK-1",
            "NOTE-1",
            "NOTE-2",
            "NOTE-3",
        }

        chunks = books.list_chunks("BOOK-1")
        assert len(chunks) == 1
        rid = chunks[0]["rid"]
        assert books.get_chunk(rid)["rid"] == rid
        assert books.get_chunk_content(rid)["content"].startswith(b"Captain")

        detail = books.detail()
        assert detail["name"] == "books"
        assert detail["display_name"] == "Books"
        assert detail["doc_count"] == 4

        queries = books.queries()
        assert len(queries) == 1
        query_id = queries[0]["query_id"]
        assert books.get_query(query_id)["query_id"] == query_id
        replay_hits = books.replay(query_id)
        assert "BOOK-1" in {match["meta"]["docid"] for match in replay_hits}

        updated = books.update(display_name="Renamed Books")
        assert updated["display_name"] == "Renamed Books"
        assert books.detail()["display_name"] == "Renamed Books"

        returned = books.rename("library")
        assert returned is books
        assert books.name == "library"
        assert books.detail()["name"] == "library"

        deleted = books.delete("NOTE-1")
        assert deleted["docid"] == "NOTE-1"
        assert books.get("BOOK-1")["docid"] == "BOOK-1"

        archive = db.dump_archive(archive_path)
        assert archive == os.fspath(archive_path)
        assert archive_path.exists()

        db.delete_collection("library")
        with pytest.raises(NotFoundError):
            db.collection("library").detail()

        db.restore_archive(archive_path.read_bytes())
        assert db.collection("library").detail()["name"] == "library"

        with db.collection("library") as handle:
            assert handle.detail()["name"] == "library"


def test_exception_translation_from_service_results(tmp_path):
    with provider(tmp_path / "db") as db:
        with pytest.raises(NotFoundError) as missing:
            db.collection("missing").detail()
        assert missing.value.code == "collection_not_found"

        with pytest.raises(InvalidRequest) as invalid:
            db.create_collection("Bad Name")
        assert invalid.value.code == "invalid_collection_name"

        db.create_collection("source")
        db.create_collection("target")
        with pytest.raises(Conflict) as conflict:
            db.collection("source").rename("target")
        assert conflict.value.code == "collection_conflict"


def test_exception_translation_from_service_errors(tmp_path, monkeypatch):
    with provider(tmp_path / "db") as db:
        coll = db.create_collection("books")

        def raise_unavailable(*_args, **_kwargs):
            raise ServiceError("embedder_unavailable", "embedder offline")

        monkeypatch.setattr("pave.localclient.svc_search", raise_unavailable)

        with pytest.raises(Unavailable) as unavailable:
            coll.search("captain")
        assert unavailable.value.code == "embedder_unavailable"
        assert unavailable.value.message == "embedder offline"

        def raise_unknown(*_args, **_kwargs):
            raise ServiceError("unexpected_failure", "boom")

        monkeypatch.setattr("pave.localclient.svc_search", raise_unknown)

        with pytest.raises(PaveError) as generic:
            coll.search("captain")
        assert type(generic.value) is PaveError
        assert generic.value.code == "unexpected_failure"


def test_get_query_and_replay_are_collection_scoped(tmp_path):
    with provider(tmp_path / "db") as db:
        books = db.create_collection("books")
        other = db.create_collection("other")
        books.add("captain nemo", docid="DOC-1")
        books.search("captain", k=1)
        qid = books.queries()[0]["query_id"]

        assert books.get_query(qid)["query_id"] == qid
        with pytest.raises(NotFoundError):
            other.get_query(qid)
        with pytest.raises(NotFoundError):
            other.replay(qid)


def test_local_provider_rename_returns_none_like_http_client(tmp_path):
    with provider(tmp_path / "db") as db:
        db.create_collection("books")

        assert db._rename("default", "books", "library") is None
        assert db.collection("library").detail()["name"] == "library"


def test_sdk_empty_text_raises_invalid_request(tmp_path):
    with provider(tmp_path / "db") as db:
        books = db.create_collection("books")
        with pytest.raises(InvalidRequest) as exc:
            books.add("")
        assert exc.value.code == "no_text_extracted"


def test_local_client_covers_the_097_collection_surface(tmp_path, cfg):
    cfg.set("embedder.instances.v2.type", "sbert")
    cfg.set("embedder.instances.v2.model", "fake-v2")
    with provider(tmp_path / "db") as db:
        books = db.create_collection(
            "books",
            search_mode="boost",
            chunking={"strategy": "none"},
            priority_key="rank",
        )
        detail = books.detail()
        assert detail["search_mode"] == "boost"
        assert detail["chunking"]["strategy"] == "none"
        assert detail["priority_key"] == "rank"

        books.add("Captain Nemo commands the Nautilus", docid="BOOK-1")
        books.dump_archive(tmp_path / "books.zip")
        data = (tmp_path / "books.zip").read_bytes()
        db.collection("copy").restore_archive(data)
        with pytest.raises(Conflict):
            db.collection("copy").restore_archive(data)
        books.restore_archive(data, replace=True)
        with pytest.raises(NotFoundError):
            db.collection("absent").restore_archive(data, replace=True)

        job = books.reindex(embed_model="fake-v2")
        assert books.reindex_job(job["job_id"])["job_id"] == job["job_id"]
        with pytest.raises(NotFoundError):
            db.collection("copy").reindex_job(job["job_id"])
        with pytest.raises(NotFoundError):
            db.collection("copy").cancel_reindex(job["job_id"])
        books.cancel_reindex(job["job_id"])


def test_local_reindex_provider_supports_selector_and_job_listing(tmp_path, cfg):
    cfg.set("embedder.instances.v2.type", "sbert")
    cfg.set("embedder.instances.v2.model", "fake-v2")
    with provider(tmp_path / "db") as db:
        db.create_collection("books", tenant="acme")
        # Exercise provider hooks independently of the installed SDK version.
        assert db._list_reindex_jobs() == {"jobs": []}
        job = db._reindex("acme", "books", embedder="v2")
        assert job["target"]["embed_model"] == "fake-v2"
        assert [
            (item["job_id"], item["tenant"], item["collection"])
            for item in db._list_reindex_jobs()["jobs"]
        ] == [(job["job_id"], "acme", "books")]
