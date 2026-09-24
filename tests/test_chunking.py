# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Per-collection chunking inside operator bounds (P1-78)."""

import pytest

from pave.config import get_cfg
from pave.config.config import _validate_chunking
from pave.preprocess import ChunkingError, preprocess_text, resolve_chunking
from pave.stores.local import LocalStore
from utils import FakeEmbedder

BOUNDS = {
    "strategies": ["fixed", "none"],
    "min_size": 200,
    "max_size": 8000,
    "min_overlap": 0,
    "max_overlap": 2000,
    "default_strategy": "fixed",
    "default_size": 1000,
    "default_overlap": 200,
}


def test_shipped_bounds_are_sane():
    _validate_chunking({"chunking": dict(BOUNDS)})


@pytest.mark.parametrize(
    "change, message",
    [
        ({"min_size": 0}, "min_size"),
        ({"min_size": 9000}, "min_size"),
        ({"min_overlap": -1}, "min_overlap"),
        ({"min_overlap": 3000}, "min_overlap"),
        ({"strategies": []}, "strategies"),
        ({"strategies": ["sentence"]}, "strategies"),
        ({"default_strategy": "none", "strategies": ["fixed"]}, "default_strategy"),
        ({"default_size": 100}, "default_size"),
        ({"default_overlap": 300}, "1/4"),
        ({"max_size": "big"}, "max_size"),
    ],
)
def test_insane_operator_bounds_fail_at_startup(change, message):
    with pytest.raises(ValueError, match=message):
        _validate_chunking({"chunking": {**BOUNDS, **change}})


def test_resolve_fills_defaults_and_checks_bounds():
    assert resolve_chunking(None) == {
        "strategy": "fixed", "size": 1000, "overlap": 200,
    }
    assert resolve_chunking({"size": 800}) == {
        "strategy": "fixed", "size": 800, "overlap": 200,
    }
    # A chosen size keeps the default overlap only while it fits the size.
    assert resolve_chunking({"size": 500}) == {
        "strategy": "fixed", "size": 500, "overlap": 125,
    }
    assert resolve_chunking({"strategy": "none"}) == {"strategy": "none"}
    for spec in (
        {"size": 100},
        {"size": 1000, "overlap": 300},
        {"strategy": "none", "size": 500},
        {"strategy": "sentence"},
        {"width": 5},
    ):
        with pytest.raises(ChunkingError) as info:
            resolve_chunking(spec)
        assert info.value.code == "invalid_chunking"
    get_cfg().set("chunking.strategies", ["fixed"])
    with pytest.raises(ChunkingError):
        resolve_chunking({"strategy": "none"})


def test_fixed_and_none_chunk_as_declared():
    text = "abcdefghij" * 100  # 1000 characters
    fixed = list(
        preprocess_text(
            text, chunking={"strategy": "fixed", "size": 400, "overlap": 100}
        )
    )
    assert [meta["offset"] for _id, _chunk, meta in fixed] == [0, 300, 600, 900]
    assert all(len(chunk) <= 400 for _id, chunk, _meta in fixed)

    whole = list(preprocess_text(text, chunking={"strategy": "none"}))
    assert whole == [("chunk_0", text, {"offset": 0})]

    get_cfg().set("chunking.max_size", 999)
    with pytest.raises(ChunkingError) as info:
        list(preprocess_text(text, chunking={"strategy": "none"}))
    assert info.value.code == "chunk_too_large"


def test_collection_keeps_its_chunking(temp_data_dir):
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    spec = {"strategy": "fixed", "size": 400, "overlap": 100}

    created = store.create_collection("acme", "books", chunking=spec)
    assert created["chunking"] == spec
    assert store.collection_chunking("acme", "books") == spec
    assert store.get_collection_detail("acme", "books")["chunking"] == spec
    with pytest.raises(ChunkingError) as info:
        store.create_collection("acme", "books", chunking={"strategy": "none"})
    assert info.value.code == "chunking_conflict"


def test_auto_created_and_legacy_collections_record_their_chunking(
    temp_data_dir,
):
    get_cfg().set("chunking.default_size", 600)
    get_cfg().set("chunking.default_overlap", 100)
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    store.index_records("acme", "auto", "D", [("D::0", "text", {})])
    assert store.collection_chunking("acme", "auto")["size"] == 600

    # A collection with chunks and no record predates P1-78: it was chunked
    # with the shipped 1000/200, whatever the operator default is now.
    db = store._dbs[("acme", "auto")]
    with db._writer() as conn, conn:
        conn.execute("DELETE FROM settings WHERE key='chunking'")
    store.close()
    reopened = LocalStore(str(temp_data_dir), FakeEmbedder())
    assert reopened.collection_chunking("acme", "legacy-absent")["size"] == 600
    reopened.search("acme", "auto", "text", k=1)
    assert reopened.collection_chunking("acme", "auto") == {
        "strategy": "fixed", "size": 1000, "overlap": 200,
    }


def test_http_chunking_round_trip(client):
    created = client.post(
        "/v1/collections/acme/notes",
        json={"chunking": {"strategy": "none"}},
    )
    assert created.status_code == 201, created.text
    assert created.json()["chunking"] == {"strategy": "none"}
    detail = client.get("/v1/collections/acme/notes/detail").json()
    assert detail["chunking"] == {"strategy": "none"}

    ok = client.post(
        "/v1/collections/acme/notes/documents",
        json={"text": "one short note", "docid": "N1"},
    )
    assert ok.status_code == 201, ok.text
    too_long = client.post(
        "/v1/collections/acme/notes/documents",
        json={"text": "x" * 8001, "docid": "N2"},
    )
    assert too_long.status_code == 400
    assert too_long.json()["code"] == "chunk_too_large"

    bad = client.post(
        "/v1/collections/acme/bad",
        json={"chunking": {"size": 100}},
    )
    assert bad.status_code == 400
    assert bad.json()["code"] == "invalid_chunking"


def test_unknown_chunking_key_is_not_echoed(client):
    key = "y" * 5000
    response = client.post(
        "/v1/collections/acme/keys", json={"chunking": {key: 1}}
    )

    assert response.status_code == 422
    assert key[:100] not in response.text
