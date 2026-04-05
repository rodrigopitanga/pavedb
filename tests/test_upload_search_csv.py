# (C) 2025 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import pytest

pytestmark = pytest.mark.slow


def test_upload_csv_and_search(client):
    # create collection
    client.post("/v1/collections/acme/csvs")

    # CSV with header + 2 rows
    csv_bytes = b"id,name,qty\n1,banana,3\n2,abacaxi,7\n"
    files = {"file": ("items.csv", csv_bytes, "text/csv")}
    data = {"docid": "DOC-CSV"}

    r = client.post("/v1/collections/acme/csvs/documents", files=files, data=data)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["ok"] is True
    # Preprocess makes one chunk per row (ignore header)
    assert out["chunks"] == 2

    # POST search with filters by docid — should find "banana"
    body = {"q": "banana", "k": 5, "filters": {"docid": "DOC-CSV"}}
    s = client.post("/v1/collections/acme/csvs/search", json=body)
    assert s.status_code == 200
    texts = " ".join(m.get("text") or "" for m in s.json()["matches"])
    assert "banana" in texts.lower()

    # GET search (no filters)
    s2 = client.get(
        "/v1/collections/acme/csvs/search",
        params={"q": "abacaxi", "k": 5}
    )
    assert s2.status_code == 200
    texts2 = " ".join(m.get("text") or "" for m in s2.json()["matches"])
    assert "abacaxi" in texts2.lower()
