# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read paths must not bring a collection into existence (B33).

Auto-vivification on read is what let a capped tenant mint unlimited
directories, catalog rows and metric series by searching invented names.
The shared corpus, which used to depend on it, is created at startup now.
"""

from fastapi.testclient import TestClient

from pave.config import get_cfg
import pave.main as main_mod


def _names(client, tenant):
    r = client.get(f"/v1/collections/{tenant}")
    assert r.status_code == 200, r.text
    return [c["name"] for c in r.json()["collections"]]


def test_search_on_a_missing_collection_creates_nothing(client):
    """The answer is unchanged -- 200 with no matches. Only the side effect
    of bringing the collection into existence is gone."""
    r = client.post("/v1/collections/ghostco/nosuch/search", json={"q": "x"})
    assert r.status_code == 200, r.text
    assert r.json()["matches"] == []
    assert _names(client, "ghostco") == []


def test_delete_document_on_a_missing_collection_creates_nothing(client):
    # The documented contract stays: idempotent 200 with chunks_deleted=0
    # (tests/test_delete_document.py). Only the side effect goes away.
    r = client.delete("/v1/collections/ghostco2/nosuch/documents/d1")
    assert r.status_code == 200, r.text
    assert r.json()["chunks_deleted"] == 0
    assert _names(client, "ghostco2") == []


def test_byov_probe_on_a_missing_collection_still_reports_dim_mismatch(client):
    # The dim probe must answer without creating. Declining to answer would
    # skip the check and let the wrong-length vector fail as a 500 instead.
    r = client.post(
        "/v1/collections/ghostco3/nosuch/search", json={"v": [1.0, 2.0]}
    )
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "dim_mismatch"
    assert _names(client, "ghostco3") == []


def test_startup_creates_the_configured_common_corpus(temp_data_dir):
    """Shared search still works without read-path auto-creation."""
    cfg = get_cfg()
    cfg.set("common_enabled", True)
    cfg.set("common_tenant", "globalco")
    cfg.set("common_collection", "shared")

    app = main_mod.build_app(cfg)
    main_mod.metrics_initialize(app.state.store)
    client = TestClient(app)
    try:
        assert _names(client, "globalco") == ["shared"]
        r = client.post("/v1/search", json={"q": "anything", "k": 3})
        assert r.status_code == 200, r.text
        # Idempotent: a second worker/restart converges on the same row.
        second = main_mod.build_app(cfg)
        assert _names(TestClient(second), "globalco") == ["shared"]
    finally:
        client.close()


def test_scoped_search_survives_a_missing_shared_corpus(client, cfg):
    """The shared corpus enriches a search; it must not be able to fail it."""
    cfg.set("common_enabled", True)
    cfg.set("common_tenant", "global")
    cfg.set("common_collection", "neverbuilt")

    client.post("/v1/collections/acme/mine")
    client.post(
        "/v1/collections/acme/mine/documents",
        json={"text": "hello world", "docid": "d1"},
    )

    r = client.post(
        "/v1/collections/acme/mine/search",
        json={"q": "hello", "k": 3, "include_common": True},
    )
    assert r.status_code == 200, r.text
    assert [m["meta"]["docid"] for m in r.json()["matches"]] == ["d1"]


def test_restart_does_not_recreate_the_common_corpus(tmp_path, monkeypatch):
    """create_collection sizes the index from the embedder; a restart must
    not pay that when the corpus already exists."""
    import pave.main as m
    from pave.config import get_cfg

    cfg = get_cfg()
    cfg.set("data_dir", str(tmp_path))
    cfg.set("common_enabled", True)
    cfg.set("common_tenant", "global")
    cfg.set("common_collection", "shared")

    first = m.build_app(cfg)
    names = {c["name"] for c in first.state.store.list_collections("global")}
    assert "shared" in names

    calls = []
    real = type(first.state.store).create_collection

    def counting(self, *a, **kw):
        calls.append(a[:2])
        return real(self, *a, **kw)

    monkeypatch.setattr(type(first.state.store), "create_collection", counting)
    m.build_app(cfg)
    assert calls == [], calls
