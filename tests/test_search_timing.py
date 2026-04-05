# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from pave.service import search as svc_search
from pave.stores.base import SearchOutput, SearchResult


def test_search_response_includes_timing_breakdown(client):
    client.post("/v1/collections/acme/timing")
    client.post(
        "/v1/collections/acme/timing/documents",
        files={"file": ("a.txt", b"hello timing world", "text/plain")},
        data={"docid": "D1"},
    )

    response = client.post(
        "/v1/collections/acme/timing/search",
        json={"q": "hello", "k": 5},
    )

    assert response.status_code == 200
    data = response.json()
    timing = data["timing"]
    assert set(timing) == {
        "embed_ms",
        "search_ms",
        "filter_ms",
        "hydrate_ms",
    }
    assert all(value >= 0.0 for value in timing.values())
    assert sum(timing.values()) <= data["latency_ms"] + 0.05


def test_search_response_timing_documented_in_openapi(app):
    schema = app.openapi()
    components = schema["components"]["schemas"]

    assert "SearchTiming" in components
    props = components["SearchResponse"]["properties"]
    assert "timing" in props
    assert props["timing"]["anyOf"][0]["$ref"].endswith("/SearchTiming")


def test_common_search_merge_sums_phase_timing():
    class TimingStore:
        def search(self, tenant, collection, q, k=5, filters=None):
            if (tenant, collection) == ("acme", "docs"):
                return SearchOutput(
                    matches=[
                        SearchResult(
                            id="local::chunk_0",
                            score=0.9,
                            text="local hit",
                            tenant=tenant,
                            collection=collection,
                            meta={},
                            match_reason="semantic similarity 90%",
                        )
                    ],
                    timing={
                        "embed_ms": 1.0,
                        "search_ms": 2.0,
                        "filter_ms": 3.0,
                        "hydrate_ms": 4.0,
                    },
                )
            return SearchOutput(
                matches=[
                    SearchResult(
                        id="common::chunk_0",
                        score=0.8,
                        text="common hit",
                        tenant=tenant,
                        collection=collection,
                        meta={},
                        match_reason="semantic similarity 80%",
                    )
                ],
                timing={
                    "embed_ms": 10.0,
                    "search_ms": 20.0,
                    "filter_ms": 30.0,
                    "hydrate_ms": 40.0,
                },
            )

    result = svc_search(
        TimingStore(),
        "acme",
        "docs",
        "hello",
        k=1,
        include_common=True,
        common_tenant="global",
        common_collection="common",
        request_id="req-1",
    )

    assert result["ok"] is True
    assert result["request_id"] == "req-1"
    assert result["matches"][0]["id"] == "local::chunk_0"
    assert result["timing"] == {
        "embed_ms": 11.0,
        "search_ms": 22.0,
        "filter_ms": 33.0,
        "hydrate_ms": 44.0,
    }
