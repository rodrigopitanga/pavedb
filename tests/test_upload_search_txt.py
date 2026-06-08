# (C) 2025 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import pytest

pytestmark = pytest.mark.slow


def test_upload_txt_and_search_post_get(client):
    client.post("/v1/collections/acme/txts")
    content = b"hello world\nthis is a test of patchvec"
    files = {"file": ("sample.txt", content, "text/plain")}
    data = {"docid": "DOC-TXT", "metadata": json.dumps({"lang": "pt"})}
    r = client.post("/v1/collections/acme/txts/documents", files=files, data=data)
    assert r.status_code == 201

    # GET without filters
    s2 = client.get("/v1/collections/acme/txts/search",
                    params={"q": "patchvec", "k": 3})
    print(s2.status_code, s2.json())
    assert s2.status_code == 200 and len(s2.json()["matches"]) >= 1

    # POST without filters
    body = {"q": "world", "k": 5}
    s = client.post("/v1/collections/acme/txts/search", json=body)
    print(s.status_code, s.json())
    assert s.status_code == 200 and len(s.json()["matches"]) >= 1

    # POST with filters
    body = {"q": "world", "k": 5, "filters": {"docid": "DOC-TXT"}}
    s = client.post("/v1/collections/acme/txts/search", json=body)
    print(s.status_code, s.json())
    assert s.status_code == 200 and len(s.json()["matches"]) >= 1

def test_reupload_same_docid_calls_purge_and_reindexes(client):
    client.post("/v1/collections/acme/reup")
    store = client.app.state.store
    # first upload
    r1 = client.post("/v1/collections/acme/reup/documents",
                     files={"file":
                            ("a.txt", b"alpha bravo charlie", "text/plain")},
                     data={"docid": "R-42"})
    assert r1.status_code == 201

    # second upload with same docid -> must call purge
    r2 = client.post("/v1/collections/acme/reup/documents",
                     files={"file":
                            ("a.txt", b"delta echo foxtrot", "text/plain")},
                     data={"docid": "R-42"})
    assert r2.status_code == 201
    # Purge now happens atomically inside index_records (the service no longer
    # issues a separate purge_doc call). Verify the re-ingest path went through
    # index_records twice for the same docid.
    index_calls = [
        c for c in store.calls
        if c[0] == "index_records" and c[1] == "acme"
        and c[2] == "reup" and c[3] == "R-42"
    ]
    assert len(index_calls) == 2
    assert ("purge_doc", "acme", "reup", "R-42") not in store.calls

    # confirm only new content appears
    s = client.post(
        "/v1/collections/acme/reup/search",
        json={"q": "delta", "k": 5,"filters": {"docid": "R-42"}}
    )
    assert s.status_code == 200
    body = " ".join((m.get("text") or "") for m in s.json()["matches"])
    assert "delta" in body.lower() and "alpha" not in body.lower()


def test_upload_diff_docid_same_coll(client):
    client.post("/v1/collections/acme/acoll")
    store = client.app.state.store
    # first upload
    r1 = client.post("/v1/collections/acme/acoll/documents",
                     files={"file":
                            ("a.txt", b"pareciam estar sentados", "text/plain")},
                     data={"docid": "D-41"})
    assert r1.status_code == 201

    s0 = client.post(
        "/v1/collections/acme/acoll/search",
        json={"q": "pareciam", "k": 5}
    )
    assert s0.status_code == 200
    hits = s0.json()["matches"]
    urid0 = hits[0]["id"]
    assert urid0 is not None and urid0 is not ''
    docid0 = hits[0].get("meta", {}).get("docid") or ""
    assert "D-41" == docid0
    text0 = hits[0].get("text") or ""
    assert "foxtrot" not in text0.lower() and "pareciam" in text0.lower()

    # second upload with different docid -> must NOT call purge
    r2 = client.post("/v1/collections/acme/acoll/documents",
                     files={"file":
                            ("b.txt", b"delta echo foxtrot", "text/plain")},
                     data={"docid": "D-42"})
    assert r2.status_code == 201

    # new doc in coll should not purge previous
    assert ("purge_doc", "acme", "acoll", "D-41") not in store.calls

    # confirm new content appears
    s2 = client.post(
        "/v1/collections/acme/acoll/search",
        json={"q": "delta", "k": 5}
    )
    assert s2.status_code == 200
    hits = s2.json()["matches"]
    urid2 = hits[0]["id"]
    assert urid2 is not None and urid2 is not ''
    docid2 = hits[0].get("meta", {}).get("docid") or ""
    assert "D-42" == docid2
    text2 = hits[0].get("text") or ""
    assert "foxtrot" in text2.lower() and "pareciam" not in text2.lower()

    # confirm preexisting content still appears
    s1 = client.post(
        "/v1/collections/acme/acoll/search",
        json={"q": "sentados", "k": 5}
    )
    assert s1.status_code == 200
    hits = s1.json()["matches"]
    urid1 = hits[0]["id"]
    assert urid1 is not None and urid1 is not ''
    docid1 = hits[0].get("meta", {}).get("docid") or ""
    assert "D-41" == docid1
    text1 = hits[0].get("text") or ""
    assert "pareciam" in text1.lower() and "sentados" in text1.lower()
