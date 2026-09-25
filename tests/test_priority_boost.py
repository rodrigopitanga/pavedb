# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Metadata priority boost: score * (1 + priority) under a per-collection key
(P2-11)."""

import re

import pytest

from pave.stores.base import PriorityKeyError
from pave.stores.local import LocalStore
from utils import FakeEmbedder


@pytest.mark.parametrize(
    ("raw", "factor"),
    [
        (0.5, 0.5),
        ("-0.25", -0.25),
        (3, 1.0),
        (-7.5, -1.0),
        (True, 0.0),
        ("high", 0.0),
        ("nan", 0.0),
        (float("inf"), 0.0),
        (None, 0.0),
        ([1], 0.0),
    ],
)
def test_priority_input_is_clamped_and_junk_is_ignored(raw, factor):
    assert LocalStore._priority_factor({"rank": raw}, "rank") == factor


def test_boost_is_proportional_and_keeps_its_sign():
    boosted, hits = LocalStore._apply_priority_boost(
        [("a", 0.8), ("b", 0.5), ("c", -0.4)],
        {"b": {"rank": 1}, "c": {"rank": 0.5}},
        "rank",
    )

    assert boosted == [
        ("b", pytest.approx(1.0)),
        ("a", pytest.approx(0.8)),
        ("c", pytest.approx(-0.2)),
    ]
    assert hits == {"b": (0.5, 1.0), "c": (-0.4, 0.5)}


def _store(temp_data_dir, name, cfg, **create):
    cfg.set("search.modes", ["vector", "boost", "hybrid"])
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    store.create_collection("acme", name, **create)
    store.index_records(
        "acme",
        name,
        "DOC",
        [
            ("DOC::strong", "alpha beta", {"lang": "en", "priority": 1}),
            ("DOC::weak", "alpha gamma delta", {"lang": "en", "rank": 1}),
            ("DOC::buried", "alpha beta", {"lang": "en", "rank": -1}),
        ],
    )
    return store


@pytest.mark.parametrize(
    ("mode", "filters"),
    [("vector", None), ("vector", {"lang": "en"}), ("boost", None),
     ("hybrid", None)],
)
def test_priority_reranks_every_search_path(temp_data_dir, cfg, mode, filters):
    store = _store(temp_data_dir, "ranked", cfg, priority_key="rank")

    hits = store.search(
        "acme", "ranked", "alpha beta", k=3, mode=mode, filters=filters,
    )

    assert [hit.id for hit in hits] == ["DOC::weak", "DOC::strong",
                                        "DOC::buried"]
    for hit in hits:
        found = re.search(
            r"; priority: rank ([+-][\d.]+), (-?[\d.]+) -> (-?[\d.]+)$",
            hit.match_reason,
        )
        if hit.id == "DOC::strong":
            assert found is None, hit.match_reason
            continue
        assert found, hit.match_reason
        factor, before, after = map(float, found.groups())
        assert after == pytest.approx(hit.score, abs=2e-3)
        assert before * (1 + factor) == pytest.approx(after, abs=2e-3)


def test_default_key_ignores_other_fields(temp_data_dir, cfg):
    store = _store(temp_data_dir, "plain", cfg)

    hits = store.search("acme", "plain", "alpha beta", k=3)

    assert store.get_collection_detail("acme", "plain")["priority_key"] == (
        "prio_boost"
    )
    assert "priority" not in hits[0].match_reason
    assert {hit.id for hit in hits[:2]} == {"DOC::strong", "DOC::buried"}


def test_priority_key_is_fixed_at_creation(temp_data_dir, cfg):
    store = _store(temp_data_dir, "fixed", cfg, priority_key="rank")

    assert store.create_collection("acme", "fixed")["priority_key"] == "rank"
    assert store.get_collection_detail("acme", "fixed")["priority_key"] == (
        "rank"
    )
    with pytest.raises(PriorityKeyError):
        store.create_collection("acme", "fixed", priority_key="other")


def test_http_priority_key_round_trip(client):
    bad = client.post(
        "/v1/collections/acme/badkey", json={"priority_key": "a.b"},
    )
    assert bad.status_code == 400, bad.text
    assert bad.json()["code"] == "invalid_priority_key"

    created = client.post(
        "/v1/collections/acme/keyed", json={"priority_key": "rank"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["priority_key"] == "rank"
    detail = client.get("/v1/collections/acme/keyed/detail").json()
    assert detail["priority_key"] == "rank"

    conflict = client.post(
        "/v1/collections/acme/keyed", json={"priority_key": "other"},
    )
    assert conflict.status_code == 400, conflict.text
    assert conflict.json()["code"] == "priority_key_conflict"
