# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest
from pave.filters import sanit_sql
from pave.service import ingest_document as svc_ingest, ServiceError
from pave.stores.local import LocalStore
from utils import FakeEmbedder

@pytest.fixture
def store(tmp_path):
    return LocalStore(str(tmp_path / "data"), FakeEmbedder())

def _csv_bytes(s: str) -> bytes:
    return s.encode("utf-8")

def test_csv_default_include_all_minus_meta(store):
    # header: a,b,c ; meta=b ; include not given => text must contain a,c and NOT b
    csv = "a,b,c\nx,metaY,z\n"
    out = svc_ingest(
        store, "t", "c", "f.csv", _csv_bytes(csv), "D", {"lang": "pt"},
        csv_options={"has_header": "yes", "meta_cols": "b", "include_cols": ""}
    )
    assert out["ok"] and out["chunks"] == 1

    # text assertions (default include excludes meta)
    hits = store.search("t", "c", "x", k=5)
    assert hits
    txt = hits[0].text
    assert "a: x" in txt
    assert "c: z" in txt
    assert "b:" not in txt

    # meta persisted: filter by meta should find the row
    hits_meta = store.search("t", "c", "x", k=5, filters={"b": "metaY"})
    assert hits_meta

def test_csv_include_by_indices_no_header(store):
    # no header; include 1,3 ; meta 2
    csv = "x,metaY,z\n"
    out = svc_ingest(
        store, "t2", "c2", "f2.csv", _csv_bytes(csv), "D2", None,
        csv_options={"has_header": "no", "meta_cols": "2", "include_cols": "1,3"}
    )
    assert out["ok"] and out["chunks"] == 1

    hits = store.search("t2", "c2", "x", k=5)
    assert hits
    txt = hits[0].text
    assert "col_1: x" in txt
    assert "col_3: z" in txt
    assert "col_2:" not in txt  # excluded as meta

    # meta persisted under the 1-based synthesized header (matches --csv-*-cols)
    hits_meta = store.search("t2", "c2", "x", k=5, filters={"col_2": "metaY"})
    assert hits_meta

def test_csv_refuse_names_without_header(store):
    # names specified but header disabled -> must raise
    csv = "x,metaY,z\n"
    with pytest.raises(ServiceError) as excinfo:
        svc_ingest(
            store, "t3", "c3", "f3.csv", _csv_bytes(csv), "D3", None,
            csv_options={"has_header": "no", "meta_cols": "b", "include_cols": ""}
        )
    assert excinfo.value.code == "invalid_csv_options"


def test_ingest_rejects_colliding_sanitized_doc_metadata_keys(store):
    with pytest.raises(ServiceError) as excinfo:
        svc_ingest(
            store,
            "t4",
            "c4",
            "f4.txt",
            _csv_bytes("hello world"),
            "D4",
            {".docid.": "shadow-docid"},
        )
    assert excinfo.value.code == "invalid_metadata_keys"
    assert "sanitize to 'docid'" in excinfo.value.message


def test_csv_ingest_rejects_colliding_sanitized_chunk_metadata_keys(store):
    csv = "a!,a?\nleft,right\n"
    with pytest.raises(ServiceError) as excinfo:
        svc_ingest(
            store,
            "t5",
            "c5",
            "f5.csv",
            _csv_bytes(csv),
            "D5",
            None,
            csv_options={
                "has_header": "yes",
                "meta_cols": "a!,a?",
                "include_cols": "",
            },
        )
    assert excinfo.value.code == "invalid_metadata_keys"
    assert "sanitize to 'a'" in excinfo.value.message


def test_ingest_rejects_metadata_key_sanitized_to_reserved_text(store):
    with pytest.raises(ServiceError) as excinfo:
        svc_ingest(
            store,
            "t6",
            "c6",
            "f6.txt",
            _csv_bytes("hello world"),
            "D6",
            {" text ": "shadow-text"},
        )
    assert excinfo.value.code == "invalid_metadata_keys"
    assert "reserved key 'text'" in excinfo.value.message


def test_ingest_sanitizes_sqlish_doc_metadata_value_and_exact_filter_matches(store):
    raw_value = "x'; DROP TABLE chunk_meta; --"
    out = svc_ingest(
        store,
        "t7",
        "c7",
        "f7.txt",
        _csv_bytes("hello world"),
        "D7",
        {"so urce": raw_value},
    )
    assert out["ok"] is True

    hits = store.search(
        "t7",
        "c7",
        "hello",
        k=5,
        filters={"so urce": raw_value},
    )
    assert len(hits) == 1
    assert hits[0].meta["so_urce"] == sanit_sql(raw_value)


def test_doc_metadata_key_space_collapses_to_underscore(store):
    # "doc id" is now a valid, distinct key (doc_id); no longer shadows docid
    out = svc_ingest(
        store, "tk", "ck", "fk.txt", _csv_bytes("hello world"), "DK",
        {"doc id": "v1"},
    )
    assert out["ok"] is True
    hits = store.search("tk", "ck", "hello", k=5, filters={"doc id": "v1"})
    assert hits
    assert hits[0].meta["doc_id"] == "v1"


def test_csv_blank_header_cell_becomes_col_n_1based(store):
    # middle header is blank -> indexed under col_2 (1-based), value not dropped
    csv = "a,,c\nx,y,z\n"
    out = svc_ingest(
        store, "te", "ce", "fe.csv", _csv_bytes(csv), "DE", None,
        csv_options={"has_header": "yes", "meta_cols": "", "include_cols": ""},
    )
    assert out["ok"] and out["chunks"] == 1
    txt = store.search("te", "ce", "y", k=5)[0].text
    assert "a: x" in txt
    assert "col_2: y" in txt
    assert "c: z" in txt


def test_csv_duplicate_header_deduped_keeps_both_columns(store):
    # two "date" columns -> date, date_2; the second value used to be lost
    csv = "date,date\n2021,2022\n"
    out = svc_ingest(
        store, "td", "cd", "fd.csv", _csv_bytes(csv), "DD", None,
        csv_options={"has_header": "yes", "meta_cols": "", "include_cols": ""},
    )
    assert out["ok"] and out["chunks"] == 1
    txt = store.search("td", "cd", "2021", k=5)[0].text
    assert "date: 2021" in txt
    assert "date_2: 2022" in txt


def test_csv_duplicate_header_select_first_by_name(store):
    # --csv-meta-cols date selects the first "date"; date_2 stays indexed
    csv = "date,date\n2021,2022\n"
    out = svc_ingest(
        store, "tn", "cn", "fn.csv", _csv_bytes(csv), "DN", None,
        csv_options={"has_header": "yes", "meta_cols": "date", "include_cols": ""},
    )
    assert out["ok"] and out["chunks"] == 1
    hits = store.search("tn", "cn", "2022", k=5)
    assert "date_2: 2022" in hits[0].text
    assert "date: 2021" not in hits[0].text
    assert store.search("tn", "cn", "2022", k=5, filters={"date": "2021"})
