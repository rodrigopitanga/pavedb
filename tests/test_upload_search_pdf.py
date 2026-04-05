# (C) 2025 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest

pytestmark = pytest.mark.slow


def test_upload_pdf_and_search(client):
    # create collection
    client.post("/v1/collections/acme/pdfs")

    # Minimal 1-page PDF (no visible text). Still yields one chunk per page.
    pdf_bytes = (
        b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
        b"1 0 obj<<>>endobj\n"
        b"2 0 obj<< /Type /Catalog /Pages 3 0 R >>endobj\n"
        b"3 0 obj<< /Type /Pages /Kids [4 0 R] /Count 1 >>endobj\n"
        b"4 0 obj<< /Type /Page /Parent 3 0 R /MediaBox [0 0 200 200] >>endobj\n"
        b"xref\n0 5\n0000000000 65535 f \n0000000015 00000 n \n0000000049 00000 n \n0000000098 00000 n \n0000000158 00000 n \n"
        b"trailer<< /Root 2 0 R /Size 5 >>\nstartxref\n220\n%%EOF\n"
    )

    files = {"file": ("blank.pdf", pdf_bytes, "application/pdf")}
    data = {"docid": "DOC-PDF"}

    r = client.post("/v1/collections/acme/pdfs/documents", files=files, data=data)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["ok"] is True
    assert out["chunks"] >= 1

    # POST search (filters by docid). DummyStore returns results even when text doesn't match.
    body = {"q": "anything", "k": 5, "filters": {"docid": "DOC-PDF"}}
    s = client.post("/v1/collections/acme/pdfs/search", json=body)
    assert s.status_code == 200
    assert len(s.json()["matches"]) >= 1

    # GET search (no filters)
    s2 = client.get("/v1/collections/acme/pdfs/search", params={"q": "anything", "k": 5})
    assert s2.status_code == 200
    assert len(s2.json()["matches"]) >= 1
