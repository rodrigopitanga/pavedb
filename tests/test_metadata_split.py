# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import json

from pave.service import ingest_document as svc_ingest_document


def _ingest_txt(app, tenant: str, collection: str, docid: str):
    response = svc_ingest_document(
        app.state.store,
        tenant,
        collection,
        "meta.txt",
        b"Documento com metadados separados.",
        docid,
        {"lang": "pt", "source": "api"},
    )
    assert response["ok"] is True


def test_ingest_keeps_document_and_chunk_metadata_split(app):
    tenant, collection, docid = "acme", "meta_split", "DOCMETA-SPLIT"
    _ingest_txt(app, tenant, collection, docid)

    store = app.state.store.impl
    col_db = store._dbs[(tenant, collection)]
    conn = col_db._conn
    assert conn is not None

    chunk_row = conn.execute(
        "SELECT meta_json FROM chunks WHERE rid=?",
        (f"{docid}::chunk_0",),
    ).fetchone()
    assert chunk_row is not None and chunk_row[0]
    assert json.loads(chunk_row[0]) == {"offset": 0}

    chunk_kv = dict(conn.execute(
        "SELECT key, value FROM chunk_meta WHERE rid=?",
        (f"{docid}::chunk_0",),
    ).fetchall())
    assert ("offset", "0") in chunk_kv.items()
    doc_only = {"docid", "filename", "lang", "source", "ingested_at"}
    assert not doc_only.intersection(chunk_kv), (
        "doc-level keys must not appear in chunk_meta"
    )

    doc_meta = json.loads(
        conn.execute(
            "SELECT meta_json FROM documents WHERE docid=?",
            (docid,),
        ).fetchone()[0]
    )
    assert doc_meta["docid"] == docid
    assert doc_meta["filename"] == "meta.txt"
    assert doc_meta["lang"] == "pt"
    assert doc_meta["source"] == "api"
    assert doc_meta["ingested_at"].endswith("Z")

    doc_kv = conn.execute(
        "SELECT key, value FROM document_meta WHERE docid=? ORDER BY key",
        (docid,),
    ).fetchall()
    assert ("docid", docid) in doc_kv
    assert ("filename", "meta.txt") in doc_kv
    assert ("lang", "pt") in doc_kv
    assert ("source", "api") in doc_kv


def test_search_merges_document_and_chunk_metadata_after_ingest(app):
    tenant, collection, docid = "acme", "meta_split_search", "DOCMETA-SEARCH"
    _ingest_txt(app, tenant, collection, docid)

    matches = app.state.store.search(
        tenant,
        collection,
        "Documento",
        k=5,
        filters={"source": "api"},
    )
    assert len(matches) == 1

    meta = matches[0].meta
    assert meta["docid"] == docid
    assert meta["filename"] == "meta.txt"
    assert meta["lang"] == "pt"
    assert meta["source"] == "api"
    assert meta["offset"] == 0
    assert meta["ingested_at"].endswith("Z")
