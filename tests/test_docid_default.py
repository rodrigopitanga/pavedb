# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest
from pave.service import ingest_document as svc_ingest, _default_docid
from pave.stores.local import LocalStore
from utils import FakeEmbedder

@pytest.fixture
def store(tmp_path):
    return LocalStore(str(tmp_path / "data"), FakeEmbedder())


def _b(s: str) -> bytes:
    return s.encode("utf-8")

def test_default_docid_from_filename_rules():
    assert _default_docid("bncc_ef.pdf") == "BNCC_EF_PDF"
    assert _default_docid("bncc ef v2.csv") == "BNCC_EF_V2_CSV"
    assert _default_docid("bncc-ef!.txt") == "BNCC_EF_TXT"
    assert _default_docid("/tmp/shadow/bncc-ef!.txt") == "BNCC_EF_TXT"
    val = _default_docid("...")
    assert val.startswith("PVDOC_") and len(val) > 6

def test_ingest_without_docid_uses_filename_based_docid(store):
    out = svc_ingest(
        store,
        "t",
        "c",
        "bncc_ef.csv",
        _b("x,y\n1,2\n"),
        None,
        None,
        csv_options={"has_header": "yes"},
    )
    assert out["ok"]
    assert out["docid"] == "BNCC_EF_CSV"


def test_ingest_with_explicit_docid_wins(store):
    out = svc_ingest(
        store,
        "t2",
        "c2",
        "whatever.txt",
        _b("hello"),
        "DOC123",
        None,
    )
    assert out["ok"] and out["docid"] == "DOC123"
