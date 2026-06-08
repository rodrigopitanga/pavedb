# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import types
import builtins
import os
import io
import pytest
from pathlib import Path

pytestmark = pytest.mark.slow
# --- Fixtures ----------------------------------------------------------------
@pytest.fixture(autouse=True)
def store(app):
    return app.state.store

# --- Tests -------------------------------------------------------------------
def test_index_and_search_pt_text(store):
    # Portuguese content; ensure non-null texts returned
    recs = [
        {
            "id": "doc::0",
            "content": "Um avião sobrevoa o oceano.",
            "metadata": {"lang": "pt"},
        },
        {
            "id": "doc::1",
            "content": "Mapas do fundo do mar são fascinantes.",
            "metadata": {"lang": "pt"},
        },
    ]
    n = store.index_records("acme", "undersea", "d1", recs)
    assert n.indexed_chunks == 2

    hits = store.search("acme", "undersea", "avião", k=5)
    assert len(hits) >= 1
    text = hits[0].text or ""
    assert "avião" in text.lower() or "aviao" in text.lower()

def test_index_two_docs_no_purge(store):
    # Portuguese content; ensure non-null texts returned
    recs1 = [
        {"id": "doc1::0", "content": "Submarino amarelo.", "metadata": {"lang": "pt"}},
    ]
    recs2 = [
        {"id": "doc2::0", "content": "Veludosas vozes.", "metadata": {"lang": "pt"}},
    ]
    n = store.index_records("acme", "undersea2", "doc1", recs1)
    assert n.indexed_chunks == 1

    n = store.index_records("acme", "undersea2", "doc2", recs2)
    assert n.indexed_chunks == 1

    hits = store.search("acme", "undersea2", "amarelo", k=5)
    assert ("purge_doc", "acme", "undersea2", "doc1") not in store.calls
    assert len(hits) >= 1
    assert hits[0].text is not None and "submarino" in hits[0].text.lower()

    recs3 = [
        {"id": "doc3::0", "content": "Som amarelo.", "metadata": {"lang": "pt"}},
    ]
    n = store.index_records("acme", "undersea2", "doc3", recs3)
    assert n.indexed_chunks == 1

    hits = store.search("acme", "undersea2", "amarelo", k=5)
    assert len(hits) >= 2
    assert hits[0].text is not None and "amarelo" in hits[0].text.lower()

def test_index_adds_docid_prefix(store):
    recs = [
        {"id": "0", "content": "bicicleta verde.", "metadata": {"lang": "pt"}},
    ]
    n = store.index_records("acme", "cycling", "docbike", recs)
    assert n.indexed_chunks == 1
    hits = store.search("acme", "cycling", "bicicleta", k=5)
    assert hits[0].text is not None and "bicicleta" in hits[0].text.lower()
    assert hits[0].text is not None and "verde" in hits[0].text.lower()
    assert hits[0].id is not None and "docbike::0" == hits[0].id

def test_chunk_sidecar_preserves_crlf(store):
    text = "First line\r\nSecond line\r\n"
    recs = [
        {"id": "0", "content": text, "metadata": {"lang": "en"}},
    ]

    n = store.index_records("acme", "crlf", "doccrlf", recs)
    assert n.indexed_chunks == 1

    stored = store.impl._load_chunk_text("acme", "crlf", "doccrlf::0")
    assert stored == text


def test_load_chunk_text_tolerates_toctou_delete(monkeypatch, store):
    recs = [
        {"id": "0", "content": "chunk text", "metadata": {"lang": "en"}},
    ]
    store.index_records("acme", "race", "docrace", recs)
    target = os.path.join(
        store.impl._chunks_dir("acme", "race"),
        store.impl._urid_to_fname("docrace::0"),
    )
    real_open = builtins.open

    def flaky_open(path, mode="r", *args, **kwargs):
        if "rb" in mode and str(path) == target:
            raise FileNotFoundError(str(path))
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", flaky_open)
    assert store.impl._load_chunk_text("acme", "race", "docrace::0") is None


def test_meta_json_and_filters(store):
    recs = [
        {"id": "docx::0", "content": "Olá mundo", "metadata": {"lang": "pt"}},
        {"id": "docx::1", "content": "Hello world", "metadata": {"lang": "en"}},
    ]
    store.index_records("ten", "c1", "docx", recs)

    # Filters should select only lang=en
    hits = store.search("ten", "c1", "world", k=5, filters={"lang": "en"})
    assert len(hits) == 1
    print(f"debug:: HITS: {hits}")
    assert hits[0].meta["lang"] == "en"

def test_doc_level_meta_persists_in_documents_table(store):
    tenant, coll, docid = "acme", "docmeta", "DOCMETA"
    recs = [
        {
            "id": "0",
            "content": "Documento com metadados.",
            "metadata": {"lang": "pt", "chunk": 0},
        },
    ]
    doc_meta = {
        "docid": docid,
        "filename": "meta.txt",
        "lang": "pt",
        "source": "api",
    }

    n = store.index_records(tenant, coll, docid, recs, doc_meta=doc_meta)
    assert n.indexed_chunks == 1

    col_db = store.impl._dbs[(tenant, coll)]
    conn = col_db._conn
    assert conn is not None
    row = conn.execute(
        "SELECT meta_json FROM documents WHERE docid=?",
        (docid,),
    ).fetchone()
    assert row is not None and row[0]
    assert json.loads(row[0]) == doc_meta

def test_purge_doc_removes_ids(store):
    recs = [
        {"id": "y::0", "content": "primeiro", "metadata": {}},
        {"id": "y::1", "content": "segundo", "metadata": {}},
    ]
    store.index_records("ten", "c2", "docy", recs)
    # sanity: present
    assert store.search("ten", "c2", "primeiro", k=3)

    removed = store.purge_doc("ten", "c2", "docy")
    assert removed == 2

    # now no matches
    hits = store.search("ten", "c2", "primeiro", k=3)
    assert hits == []

def test_load_or_init_handles_empty_index_dir(store, tmp_path):
    """
    Repro of FAISS crash: empty ./data/T/C/index/ existed before backend load.
    Expectation: store should initialize fresh instead of failing.
    """
    tenant, coll = "tnew", "cnew"

    # Pre-create empty index dir to mimic the broken state
    base = os.path.join(tmp_path, "data", tenant, coll)
    os.makedirs(os.path.join(base, "index"), exist_ok=True)

    # Ingest one record; should not raise, and should persist fake index json
    recs = [{"id": "r::0", "content": "hello world", "metadata": {"lang": "en"}}]
    n = store.index_records(tenant, coll, "DOC", recs)
    assert n.indexed_chunks == 1

    # force a save; backend writes persisted FAISS artifacts
    store.impl._save(tenant, coll)

    # resolve base via the store (avoid tmp_path vs CFG.data_dir drift)
    # ok to use a protected helper in tests - remember we're using SpyStore
    base = store.impl._base_path(tenant, coll)
    f_index = os.path.join(base, "index", "faiss.index")
    f_map = os.path.join(base, "index", "id_map.json")
    assert os.path.isfile(f_index), "FAISS index file must exist after save"
    assert os.path.isfile(f_map), "ID map file must exist after save"
