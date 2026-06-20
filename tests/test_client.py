# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import os

import pytest

import pave
import pave.client as client_mod
from pave.client import (
    Collection,
    Conflict,
    InvalidRequest,
    LocalClient,
    NotFoundError,
    PaveError,
    Unavailable,
)
from pave.service import ServiceError


def test_connect_dispatch_and_ephemeral_cleanup(tmp_path, monkeypatch):
    persistent = pave.connect(tmp_path / "data")
    try:
        assert isinstance(persistent, LocalClient)
        assert persistent.path == os.fspath(tmp_path / "data")
    finally:
        persistent.close()

    monkeypatch.chdir(tmp_path)
    bare = pave.connect("localhost")
    try:
        assert isinstance(bare, LocalClient)
        assert bare.path == "localhost"
    finally:
        bare.close()

    with pytest.raises(InvalidRequest) as excinfo:
        pave.connect("http://localhost:8080")
    assert excinfo.value.code == "unsupported_scheme"
    assert excinfo.value.message == (
        "network targets are handled by the pavedb-client package"
    )

    ephemeral = pave.connect()
    ephemeral_path = ephemeral.path
    assert os.path.isdir(ephemeral_path)
    ephemeral.close()
    assert not os.path.exists(ephemeral_path)

    with pytest.raises(InvalidRequest) as blank:
        pave.connect("")
    assert blank.value.code == "unsupported_target"


def test_local_provider_accepts_target_and_tenant(tmp_path):
    persistent = client_mod.local_provider(tmp_path / "data", tenant="acme")
    try:
        assert isinstance(persistent, LocalClient)
        assert persistent.path == os.fspath(tmp_path / "data")
        assert persistent.tenant == "acme"
    finally:
        persistent.close()

    ephemeral = client_mod.local_provider(None, tenant="acme")
    ephemeral_path = ephemeral.path
    try:
        assert ephemeral.tenant == "acme"
        assert os.path.isdir(ephemeral_path)
    finally:
        ephemeral.close()
    assert not os.path.exists(ephemeral_path)


def test_collection_surface_returns_clean_data(tmp_path):
    file_path = tmp_path / "book.txt"
    file_path.write_text("Captain Nemo commands the Nautilus", encoding="utf-8")
    archive_path = tmp_path / "dump.zip"

    with pave.connect(tmp_path / "db") as db:
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
    with pave.connect(tmp_path / "db") as db:
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
    with pave.connect(tmp_path / "db") as db:
        coll = db.create_collection("books")

        def raise_unavailable(*_args, **_kwargs):
            raise ServiceError("embedder_unavailable", "embedder offline")

        monkeypatch.setattr("pave.client.svc_search", raise_unavailable)

        with pytest.raises(Unavailable) as unavailable:
            coll.search("captain")
        assert unavailable.value.code == "embedder_unavailable"
        assert unavailable.value.message == "embedder offline"

        def raise_unknown(*_args, **_kwargs):
            raise ServiceError("unexpected_failure", "boom")

        monkeypatch.setattr("pave.client.svc_search", raise_unknown)

        with pytest.raises(PaveError) as generic:
            coll.search("captain")
        assert type(generic.value) is PaveError
        assert generic.value.code == "unexpected_failure"
