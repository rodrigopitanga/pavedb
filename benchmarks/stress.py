#!/usr/bin/env python3
# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Stress test for PaveDB.

Usage:
    python benchmarks/stress.py [--url URL] [--duration SECS] [--concurrency C]

Fires random concurrent operations (collection create/delete, document
ingest/delete, search, health checks, archive download/restore) and reports
per-operation latency percentiles plus error rates.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import random
import string
import sys
import time
import traceback
from collections import defaultdict
from dataclasses import dataclass, field

try:
    import httpx
except ImportError:
    raise SystemExit("httpx required: pip install httpx")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import print_run_header  # type: ignore[import]  # noqa: E402

# ---------------------------------------------------------------------------
# Corpus of sample documents for ingestion
# ---------------------------------------------------------------------------
SAMPLE_TEXTS = [
    "Machine learning is a subset of artificial intelligence that enables "
    "systems to learn from data.",
    "Natural language processing helps computers understand human language "
    "and text.",
    "Deep learning uses neural networks with many layers to model complex "
    "patterns.",
    "Vector databases store embeddings for efficient similarity search "
    "operations.",
    "Semantic search finds results based on meaning rather than exact keyword "
    "matches.",
    "Transformers revolutionized NLP with attention mechanisms and parallel "
    "processing.",
    "Embeddings represent text as dense vectors in high-dimensional space.",
    "Retrieval augmented generation combines search with language model "
    "outputs.",
    "Cosine similarity measures the angle between two vectors for comparison.",
    "Fine-tuning adapts pre-trained models to specific domains and tasks.",
    "Convolutional neural networks excel at image recognition and computer "
    "vision tasks.",
    "Recurrent neural networks process sequential data like time series and "
    "text.",
    "Generative adversarial networks create realistic synthetic data through "
    "competition.",
    "Reinforcement learning trains agents through reward signals in "
    "environments.",
    "Transfer learning leverages knowledge from one task to improve another.",
]

LONG_TEXT = (
    "This is a longer document intended to trigger chunking behaviour in the "
    "ingestion pipeline. " * 80
)

QUERIES = [
    "machine learning artificial intelligence",
    "natural language understanding",
    "neural networks deep learning",
    "vector similarity search",
    "semantic meaning search",
    "transformer attention mechanism",
    "text embeddings representation",
    "retrieval generation",
    "similarity comparison",
    "model fine-tuning",
    "image recognition",
    "sequential data processing",
    "synthetic data generation",
    "reward based training",
    "knowledge transfer",
]

TENANT = "stress"
API_PREFIX = "/v1"


def _rand_name(n: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=n))


# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------
@dataclass
class OpResult:
    op: str
    latency_ms: float
    ok: bool
    detail: str = ""


@dataclass
class Stats:
    results: list[OpResult] = field(default_factory=list)

    def record(self, r: OpResult):
        self.results.append(r)

    def summary(self) -> dict:
        by_op: dict[str, list[OpResult]] = defaultdict(list)
        for r in self.results:
            by_op[r.op].append(r)

        out = {}
        for op, items in sorted(by_op.items()):
            lats = [i.latency_ms for i in items]
            ok_count = sum(1 for i in items if i.ok)
            err_count = len(items) - ok_count
            sorted_lats = sorted(lats)
            out[op] = {
                "count": len(items),
                "ok": ok_count,
                "errors": err_count,
                "min_ms": min(sorted_lats) if sorted_lats else 0,
                "max_ms": max(sorted_lats) if sorted_lats else 0,
                "p50_ms": _percentile(sorted_lats, 50),
                "p95_ms": _percentile(sorted_lats, 95),
                "p99_ms": _percentile(sorted_lats, 99),
            }
        return out


def _percentile(sorted_data: list[float], p: float) -> float:
    if not sorted_data:
        return 0.0
    k = (len(sorted_data) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(sorted_data) - 1)
    return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])

def _parse_error(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        code = payload.get("code")
        error = payload.get("error")
        if code or error:
            return f"{code or 'error'}: {error or 'request failed'}"
    return f"http_{resp.status_code}"


def _ok_response(resp: httpx.Response) -> bool:
    return resp.status_code < 400


def _is_rate_limited(resp: httpx.Response) -> bool:
    return resp.status_code == 429


def _ensure_ok(resp: httpx.Response, label: str) -> None:
    if _ok_response(resp):
        return
    raise RuntimeError(f"{label} failed: {_parse_error(resp)}")


async def _post_with_retries(
    client: httpx.AsyncClient,
    url: str,
    attempts: int = 3,
    sleep_s: float = 0.5,
    **kwargs,
) -> httpx.Response:
    last_resp: httpx.Response | None = None
    for i in range(attempts):
        resp = await client.post(url, **kwargs)
        last_resp = resp
        if _ok_response(resp):
            return resp
        if i < attempts - 1:
            await asyncio.sleep(sleep_s * (i + 1))
    assert last_resp is not None
    return last_resp


def _record_rate_limited(stats: Stats, lat: float) -> None:
    """Record a 429 response as its own category, not as an op error."""
    stats.record(OpResult("rate_limited", lat, True))


# ---------------------------------------------------------------------------
# Shared mutable state for the stress workers
# ---------------------------------------------------------------------------
@dataclass
class World:
    """Tracks live collections and documents so workers pick valid targets."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    collections: dict[str, list[str]] = field(default_factory=dict)
    # collections[name] -> list of docids
    query_ids: list[tuple[str, str]] = field(default_factory=list)
    # bounded ring buffer of (collection, query_id) seen via op_search
    _query_id_cap: int = 256

    async def add_collection(self, name: str):
        async with self.lock:
            self.collections.setdefault(name, [])

    async def remove_collection(self, name: str) -> bool:
        async with self.lock:
            return self.collections.pop(name, None) is not None

    async def add_doc(self, collection: str, docid: str):
        async with self.lock:
            if collection in self.collections:
                self.collections[collection].append(docid)

    async def pick_collection(self) -> str | None:
        async with self.lock:
            if not self.collections:
                return None
            return random.choice(list(self.collections.keys()))

    async def pick_doc(self) -> tuple | None:
        async with self.lock:
            candidates = [
                (c, d)
                for c, docs in self.collections.items()
                for d in docs
            ]
            if not candidates:
                return None
            return random.choice(candidates)

    async def remove_doc(self, collection: str, docid: str):
        async with self.lock:
            if collection in self.collections:
                try:
                    self.collections[collection].remove(docid)
                except ValueError:
                    pass

    async def snapshot_collections(self) -> list[str]:
        async with self.lock:
            return list(self.collections.keys())

    async def add_query_id(self, collection: str, query_id: str):
        async with self.lock:
            self.query_ids.append((collection, query_id))
            if len(self.query_ids) > self._query_id_cap:
                # Drop oldest to bound memory under long runs.
                self.query_ids = self.query_ids[-self._query_id_cap:]

    async def pick_query_id(self) -> tuple[str, str] | None:
        async with self.lock:
            if not self.query_ids:
                return None
            return random.choice(self.query_ids)


# ---------------------------------------------------------------------------
# Individual operations
# ---------------------------------------------------------------------------
# Each op_* function declares the (METHOD, path-template) routes it exercises
# via @_covers(...). The startup coverage check compares the union of these
# against /openapi.json and warns about any uncovered endpoint. Forgetting
# the decorator on a new op means the coverage check will surface the gap.

def _covers(*routes: tuple[str, str]):
    """Mark which (METHOD, path-template) routes an op_* exercises."""
    def deco(fn):
        fn.__bench_covers__ = tuple(routes)
        return fn
    return deco


@_covers(("POST", "/v1/collections/{tenant}/{name}"))
async def op_create_collection(client: httpx.AsyncClient, world: World, stats: Stats):
    name = f"s_{_rand_name()}"
    t0 = time.perf_counter()
    try:
        r = await client.post(f"{API_PREFIX}/collections/{TENANT}/{name}")
        lat = (time.perf_counter() - t0) * 1000
        if _is_rate_limited(r):
            _record_rate_limited(stats, lat)
            return
        if not _ok_response(r):
            stats.record(OpResult("collection_create", lat, False,
                                  _parse_error(r)))
            return
        await world.add_collection(name)
        stats.record(OpResult("collection_create", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("collection_create", lat, False, str(e)))


@_covers(("DELETE", "/v1/collections/{tenant}/{name}"))
async def op_delete_collection(client: httpx.AsyncClient, world: World, stats: Stats):
    name = await world.pick_collection()
    if name is None:
        return  # nothing to delete
    # Remove from world only after a successful HTTP delete so that world
    # does not empty out prematurely when the request fails or is slow.
    t0 = time.perf_counter()
    try:
        r = await client.delete(f"{API_PREFIX}/collections/{TENANT}/{name}")
        lat = (time.perf_counter() - t0) * 1000
        if _is_rate_limited(r):
            _record_rate_limited(stats, lat)
            return
        if not _ok_response(r):
            stats.record(OpResult("collection_delete", lat, False,
                                  _parse_error(r)))
            return
        await world.remove_collection(name)
        stats.record(OpResult("collection_delete", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("collection_delete", lat, False, str(e)))


@_covers(("POST", "/v1/collections/{tenant}/{collection}/documents"))
async def op_ingest_small(client: httpx.AsyncClient, world: World, stats: Stats):
    """Ingest a short document (no chunking)."""
    coll = await world.pick_collection()
    if coll is None:
        return
    text = random.choice(SAMPLE_TEXTS)
    docid = f"doc_{_rand_name()}"
    t0 = time.perf_counter()
    try:
        r = await client.post(
            f"{API_PREFIX}/collections/{TENANT}/{coll}/documents",
            files={"file": (f"{docid}.txt", text.encode(), "text/plain")},
            data={"docid": docid},
        )
        lat = (time.perf_counter() - t0) * 1000
        if _is_rate_limited(r):
            _record_rate_limited(stats, lat)
            return
        if not _ok_response(r):
            stats.record(OpResult("ingest_small", lat, False, _parse_error(r)))
            return
        await world.add_doc(coll, docid)
        stats.record(OpResult("ingest_small", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("ingest_small", lat, False, str(e)))


@_covers(("POST", "/v1/collections/{tenant}/{collection}/documents"))
async def op_ingest_large(client: httpx.AsyncClient, world: World, stats: Stats):
    """Ingest a larger document that triggers chunking."""
    coll = await world.pick_collection()
    if coll is None:
        return
    docid = f"big_{_rand_name()}"
    t0 = time.perf_counter()
    try:
        r = await client.post(
            f"{API_PREFIX}/collections/{TENANT}/{coll}/documents",
            files={"file": (f"{docid}.txt", LONG_TEXT.encode(), "text/plain")},
            data={"docid": docid},
        )
        lat = (time.perf_counter() - t0) * 1000
        if _is_rate_limited(r):
            _record_rate_limited(stats, lat)
            return
        if not _ok_response(r):
            stats.record(OpResult("ingest_chunked", lat, False,
                                  _parse_error(r)))
            return
        await world.add_doc(coll, docid)
        stats.record(OpResult("ingest_chunked", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("ingest_chunked", lat, False, str(e)))


@_covers(("DELETE", "/v1/collections/{tenant}/{collection}/documents/{docid}"))
async def op_delete_document(client: httpx.AsyncClient, world: World, stats: Stats):
    pair = await world.pick_doc()
    if pair is None:
        return
    coll, docid = pair
    await world.remove_doc(coll, docid)
    t0 = time.perf_counter()
    try:
        r = await client.delete(
            f"{API_PREFIX}/collections/{TENANT}/{coll}/documents/{docid}"
        )
        lat = (time.perf_counter() - t0) * 1000
        if _is_rate_limited(r):
            _record_rate_limited(stats, lat)
            return
        if not _ok_response(r):
            stats.record(OpResult("doc_delete", lat, False, _parse_error(r)))
            return
        stats.record(OpResult("doc_delete", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("doc_delete", lat, False, str(e)))


@_covers(("POST", "/v1/collections/{tenant}/{name}/search"))
async def op_search(client: httpx.AsyncClient, world: World, stats: Stats):
    coll = await world.pick_collection()
    if coll is None:
        return
    query = random.choice(QUERIES)
    t0 = time.perf_counter()
    try:
        r = await client.post(
            f"{API_PREFIX}/collections/{TENANT}/{coll}/search",
            json={"q": query, "k": 5},
        )
        lat = (time.perf_counter() - t0) * 1000
        if _is_rate_limited(r):
            _record_rate_limited(stats, lat)
            return
        if not _ok_response(r):
            stats.record(OpResult("search", lat, False, _parse_error(r)))
            return
        try:
            payload = r.json()
        except Exception:
            payload = {}
        qid = payload.get("query_id") if isinstance(payload, dict) else None
        if qid:
            await world.add_query_id(coll, qid)
        stats.record(OpResult("search", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("search", lat, False, str(e)))


@_covers(("GET", "/v1/admin/archive"))
async def op_archive_download(client: httpx.AsyncClient, _: World, stats: Stats):
    t0 = time.perf_counter()
    try:
        r = await client.get(f"{API_PREFIX}/admin/archive")
        lat = (time.perf_counter() - t0) * 1000
        if not _ok_response(r):
            stats.record(OpResult("archive_download", lat, False,
                                  _parse_error(r)))
            return
        size_kb = len(r.content) / 1024
        stats.record(OpResult("archive_download", lat, True, f"{size_kb:.1f}KB"))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("archive_download", lat, False, str(e)))


@_covers(("GET", "/health"))
async def op_health(client: httpx.AsyncClient, _: World, stats: Stats):
    """GET /health — triggers .writetest write+delete and metrics inc."""
    t0 = time.perf_counter()
    try:
        r = await client.get("/health")
        lat = (time.perf_counter() - t0) * 1000
        if not _ok_response(r):
            stats.record(OpResult("health", lat, False, _parse_error(r)))
            return
        stats.record(OpResult("health", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("health", lat, False, str(e)))


@_covers(("GET", "/health/ready"))
async def op_health_ready(client: httpx.AsyncClient, _: World, stats: Stats):
    """GET /health/ready — same .writetest race plus vector backend init."""
    t0 = time.perf_counter()
    try:
        await client.get("/health/ready")
        lat = (time.perf_counter() - t0) * 1000
        # 503 is valid (degraded), only network errors are failures
        stats.record(OpResult("health_ready", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("health_ready", lat, False, str(e)))


@_covers(("GET", "/health/live"))
async def op_health_live(client: httpx.AsyncClient, _: World, stats: Stats):
    """GET /health/live — no I/O, but still triggers metrics inc+save."""
    t0 = time.perf_counter()
    try:
        r = await client.get("/health/live")
        lat = (time.perf_counter() - t0) * 1000
        if not _ok_response(r):
            stats.record(OpResult("health_live", lat, False, _parse_error(r)))
            return
        stats.record(OpResult("health_live", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("health_live", lat, False, str(e)))


@_covers(("GET", "/health/metrics"))
async def op_health_metrics(client: httpx.AsyncClient, _: World, stats: Stats):
    """GET /health/metrics — reads counters+latencies, triggers metrics save."""
    t0 = time.perf_counter()
    try:
        r = await client.get("/health/metrics")
        lat = (time.perf_counter() - t0) * 1000
        if not _ok_response(r):
            stats.record(OpResult("health_metrics", lat, False,
                                  _parse_error(r)))
            return
        stats.record(OpResult("health_metrics", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("health_metrics", lat, False, str(e)))


@_covers(
    ("GET", "/v1/admin/archive"),
    ("PUT", "/v1/admin/archive"),
)
async def op_archive_restore(client: httpx.AsyncClient, _: World, stats: Stats):
    # First download an archive, then restore it
    t0 = time.perf_counter()
    try:
        dl = await client.get(f"{API_PREFIX}/admin/archive")
        lat = (time.perf_counter() - t0) * 1000
        if not _ok_response(dl):
            stats.record(OpResult("archive_restore", lat, False,
                                  f"download: {_parse_error(dl)}"))
            return
        archive_bytes = dl.content
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("archive_restore", lat, False, f"download: {e}"))
        return

    t0 = time.perf_counter()
    try:
        r = await client.put(
            f"{API_PREFIX}/admin/archive",
            files={"file": ("archive.zip", archive_bytes, "application/zip")},
        )
        lat = (time.perf_counter() - t0) * 1000
        if not _ok_response(r):
            stats.record(OpResult("archive_restore", lat, False,
                                  _parse_error(r)))
            return
        stats.record(OpResult("archive_restore", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("archive_restore", lat, False, str(e)))


# ---------------------------------------------------------------------------
# Critical-suite ops: exercise endpoints that base does not, focused on
# race-prone paths (multi-collection write lock, reads racing with delete,
# query-log retry path, sidecar chunk read).
# ---------------------------------------------------------------------------
@_covers(("PUT", "/v1/collections/{tenant}/{name}"))
async def op_rename_collection(client: httpx.AsyncClient, world: World, stats: Stats):
    """PUT /collections/{tenant}/{old} {new_name} — variadic write lock path."""
    old = await world.pick_collection()
    if old is None:
        return
    new = f"r_{_rand_name()}"
    t0 = time.perf_counter()
    try:
        r = await client.put(
            f"{API_PREFIX}/collections/{TENANT}/{old}",
            json={"new_name": new},
        )
        lat = (time.perf_counter() - t0) * 1000
        if _is_rate_limited(r):
            _record_rate_limited(stats, lat)
            return
        # Expected race outcomes under stress: 404 (source gone — another
        # worker deleted/renamed it first) and 409 (target name just got
        # taken). Count as race-wins so genuine 5xx stand out.
        if r.status_code in (404, 409):
            stats.record(OpResult(
                "rename_collection", lat, True, f"http_{r.status_code}",
            ))
            return
        if not _ok_response(r):
            stats.record(OpResult("rename_collection", lat, False,
                                  _parse_error(r)))
            return
        # Mirror the rename in world so workers stop targeting the old name.
        await world.remove_collection(old)
        await world.add_collection(new)
        stats.record(OpResult("rename_collection", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("rename_collection", lat, False, str(e)))


@_covers(("GET", "/v1/collections/{tenant}/{collection}/documents"))
async def op_list_documents(client: httpx.AsyncClient, world: World, stats: Stats):
    """GET /collections/{tenant}/{coll}/documents — read racing with delete."""
    coll = await world.pick_collection()
    if coll is None:
        return
    t0 = time.perf_counter()
    try:
        r = await client.get(
            f"{API_PREFIX}/collections/{TENANT}/{coll}/documents",
        )
        lat = (time.perf_counter() - t0) * 1000
        if _is_rate_limited(r):
            _record_rate_limited(stats, lat)
            return
        if not _ok_response(r):
            stats.record(OpResult("list_documents", lat, False,
                                  _parse_error(r)))
            return
        stats.record(OpResult("list_documents", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("list_documents", lat, False, str(e)))


@_covers(("GET", "/v1/collections/{tenant}"))
async def op_list_collections(client: httpx.AsyncClient, _: World, stats: Stats):
    """GET /collections/{tenant} — catalog read racing with create/delete."""
    t0 = time.perf_counter()
    try:
        r = await client.get(f"{API_PREFIX}/collections/{TENANT}")
        lat = (time.perf_counter() - t0) * 1000
        if _is_rate_limited(r):
            _record_rate_limited(stats, lat)
            return
        if not _ok_response(r):
            stats.record(OpResult("list_collections", lat, False,
                                  _parse_error(r)))
            return
        stats.record(OpResult("list_collections", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("list_collections", lat, False, str(e)))


@_covers(("GET", "/v1/collections/{tenant}/{name}/queries"))
async def op_query_log_list(client: httpx.AsyncClient, world: World, stats: Stats):
    """GET /collections/{tenant}/{coll}/queries — query log read path."""
    coll = await world.pick_collection()
    if coll is None:
        return
    t0 = time.perf_counter()
    try:
        r = await client.get(
            f"{API_PREFIX}/collections/{TENANT}/{coll}/queries",
        )
        lat = (time.perf_counter() - t0) * 1000
        if _is_rate_limited(r):
            _record_rate_limited(stats, lat)
            return
        if not _ok_response(r):
            stats.record(OpResult("query_log_list", lat, False,
                                  _parse_error(r)))
            return
        stats.record(OpResult("query_log_list", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("query_log_list", lat, False, str(e)))


@_covers(("POST", "/v1/collections/{tenant}/{name}/queries/{query_id}/replay"))
async def op_query_replay(client: httpx.AsyncClient, world: World, stats: Stats):
    """POST /collections/{tenant}/{coll}/queries/{id}/replay — exercises the
    log_query retry pattern and read-lock-then-write-lock loop."""
    pair = await world.pick_query_id()
    if pair is None:
        return
    coll, qid = pair
    t0 = time.perf_counter()
    try:
        r = await client.post(
            f"{API_PREFIX}/collections/{TENANT}/{coll}/queries/{qid}/replay",
        )
        lat = (time.perf_counter() - t0) * 1000
        if _is_rate_limited(r):
            _record_rate_limited(stats, lat)
            return
        if r.status_code == 404:
            # Query log entry was purged by archive_restore or delete; not a
            # bug, just a race that's expected under stress.
            stats.record(OpResult("query_replay", lat, True, "404"))
            return
        if not _ok_response(r):
            stats.record(OpResult("query_replay", lat, False, _parse_error(r)))
            return
        stats.record(OpResult("query_replay", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("query_replay", lat, False, str(e)))


@_covers(
    ("GET", "/v1/collections/{tenant}/{collection}/chunks/{rid}/content"),
)
async def op_get_chunk_content(
    client: httpx.AsyncClient, world: World, stats: Stats,
):
    """GET /collections/{tenant}/{coll}/chunks/{rid}/content — sidecar file
    read racing with purge_doc/delete_collection."""
    pair = await world.pick_doc()
    if pair is None:
        return
    coll, docid = pair
    # Bench-ingested docs (op_ingest_small + seeds) produce a single chunk
    # whose rid is "{docid}::chunk_0"; large docs may not, but the 404 path
    # is also a valid race outcome here.
    rid = f"{docid}::chunk_0"
    t0 = time.perf_counter()
    try:
        r = await client.get(
            f"{API_PREFIX}/collections/{TENANT}/{coll}/chunks/{rid}/content",
        )
        lat = (time.perf_counter() - t0) * 1000
        if _is_rate_limited(r):
            _record_rate_limited(stats, lat)
            return
        if r.status_code == 404:
            stats.record(OpResult("get_chunk_content", lat, True, "404"))
            return
        if not _ok_response(r):
            stats.record(OpResult("get_chunk_content", lat, False,
                                  _parse_error(r)))
            return
        stats.record(OpResult("get_chunk_content", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("get_chunk_content", lat, False, str(e)))


# ---------------------------------------------------------------------------
# Full-suite ops: rest of the API surface. Lower individual weights since
# they are mostly thin reads; here for coverage, not for stress shape.
# ---------------------------------------------------------------------------
@_covers(("GET", "/v1/collections/{tenant}/{name}/detail"))
async def op_get_collection_detail(
    client: httpx.AsyncClient, world: World, stats: Stats,
):
    coll = await world.pick_collection()
    if coll is None:
        return
    t0 = time.perf_counter()
    try:
        r = await client.get(
            f"{API_PREFIX}/collections/{TENANT}/{coll}/detail",
        )
        lat = (time.perf_counter() - t0) * 1000
        if _is_rate_limited(r):
            _record_rate_limited(stats, lat)
            return
        if r.status_code == 404:
            stats.record(OpResult("get_collection_detail", lat, True, "404"))
            return
        if not _ok_response(r):
            stats.record(OpResult("get_collection_detail", lat, False,
                                  _parse_error(r)))
            return
        stats.record(OpResult("get_collection_detail", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("get_collection_detail", lat, False, str(e)))


@_covers(("GET", "/v1/collections/{tenant}/{collection}/documents/{docid}"))
async def op_get_document(client: httpx.AsyncClient, world: World, stats: Stats):
    pair = await world.pick_doc()
    if pair is None:
        return
    coll, docid = pair
    t0 = time.perf_counter()
    try:
        r = await client.get(
            f"{API_PREFIX}/collections/{TENANT}/{coll}/documents/{docid}",
        )
        lat = (time.perf_counter() - t0) * 1000
        if _is_rate_limited(r):
            _record_rate_limited(stats, lat)
            return
        if r.status_code == 404:
            stats.record(OpResult("get_document", lat, True, "404"))
            return
        if not _ok_response(r):
            stats.record(OpResult("get_document", lat, False, _parse_error(r)))
            return
        stats.record(OpResult("get_document", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("get_document", lat, False, str(e)))


@_covers(
    ("GET", "/v1/collections/{tenant}/{collection}/documents/{docid}/chunks"),
)
async def op_list_chunks(client: httpx.AsyncClient, world: World, stats: Stats):
    pair = await world.pick_doc()
    if pair is None:
        return
    coll, docid = pair
    t0 = time.perf_counter()
    try:
        r = await client.get(
            f"{API_PREFIX}/collections/{TENANT}/{coll}/documents/{docid}/chunks",
        )
        lat = (time.perf_counter() - t0) * 1000
        if _is_rate_limited(r):
            _record_rate_limited(stats, lat)
            return
        if r.status_code == 404:
            stats.record(OpResult("list_chunks", lat, True, "404"))
            return
        if not _ok_response(r):
            stats.record(OpResult("list_chunks", lat, False, _parse_error(r)))
            return
        stats.record(OpResult("list_chunks", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("list_chunks", lat, False, str(e)))


@_covers(("GET", "/v1/collections/{tenant}/{collection}/chunks/{rid}"))
async def op_get_chunk(client: httpx.AsyncClient, world: World, stats: Stats):
    pair = await world.pick_doc()
    if pair is None:
        return
    coll, docid = pair
    rid = f"{docid}::chunk_0"
    t0 = time.perf_counter()
    try:
        r = await client.get(
            f"{API_PREFIX}/collections/{TENANT}/{coll}/chunks/{rid}",
        )
        lat = (time.perf_counter() - t0) * 1000
        if _is_rate_limited(r):
            _record_rate_limited(stats, lat)
            return
        if r.status_code == 404:
            stats.record(OpResult("get_chunk", lat, True, "404"))
            return
        if not _ok_response(r):
            stats.record(OpResult("get_chunk", lat, False, _parse_error(r)))
            return
        stats.record(OpResult("get_chunk", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("get_chunk", lat, False, str(e)))


@_covers(("GET", "/v1/collections/{tenant}/{name}/search"))
async def op_search_get(client: httpx.AsyncClient, world: World, stats: Stats):
    """GET /collections/{tenant}/{coll}/search — query-string variant."""
    coll = await world.pick_collection()
    if coll is None:
        return
    query = random.choice(QUERIES)
    t0 = time.perf_counter()
    try:
        r = await client.get(
            f"{API_PREFIX}/collections/{TENANT}/{coll}/search",
            params={"q": query, "k": 5},
        )
        lat = (time.perf_counter() - t0) * 1000
        if _is_rate_limited(r):
            _record_rate_limited(stats, lat)
            return
        if not _ok_response(r):
            stats.record(OpResult("search_get", lat, False, _parse_error(r)))
            return
        stats.record(OpResult("search_get", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("search_get", lat, False, str(e)))


@_covers(("POST", "/v1/search"))
async def op_global_search(client: httpx.AsyncClient, _: World, stats: Stats):
    """POST /search — cross-collection global search."""
    query = random.choice(QUERIES)
    t0 = time.perf_counter()
    try:
        r = await client.post(
            f"{API_PREFIX}/search",
            json={"q": query, "k": 5},
        )
        lat = (time.perf_counter() - t0) * 1000
        if _is_rate_limited(r):
            _record_rate_limited(stats, lat)
            return
        if not _ok_response(r):
            stats.record(OpResult("global_search", lat, False, _parse_error(r)))
            return
        stats.record(OpResult("global_search", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("global_search", lat, False, str(e)))


@_covers(("GET", "/v1/admin/tenants"))
async def op_admin_tenants(client: httpx.AsyncClient, _: World, stats: Stats):
    t0 = time.perf_counter()
    try:
        r = await client.get(f"{API_PREFIX}/admin/tenants")
        lat = (time.perf_counter() - t0) * 1000
        if _is_rate_limited(r):
            _record_rate_limited(stats, lat)
            return
        if not _ok_response(r):
            stats.record(OpResult("admin_tenants", lat, False, _parse_error(r)))
            return
        stats.record(OpResult("admin_tenants", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("admin_tenants", lat, False, str(e)))


@_covers(("GET", "/v1/admin/queries/{query_id}"))
async def op_admin_queries_get(
    client: httpx.AsyncClient, world: World, stats: Stats,
):
    pair = await world.pick_query_id()
    if pair is None:
        return
    _coll, qid = pair
    t0 = time.perf_counter()
    try:
        r = await client.get(f"{API_PREFIX}/admin/queries/{qid}")
        lat = (time.perf_counter() - t0) * 1000
        if _is_rate_limited(r):
            _record_rate_limited(stats, lat)
            return
        if r.status_code == 404:
            stats.record(OpResult("admin_queries_get", lat, True, "404"))
            return
        if not _ok_response(r):
            stats.record(OpResult("admin_queries_get", lat, False,
                                  _parse_error(r)))
            return
        stats.record(OpResult("admin_queries_get", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("admin_queries_get", lat, False, str(e)))


@_covers(("GET", "/metrics"))
async def op_prometheus_metrics(
    client: httpx.AsyncClient, _: World, stats: Stats,
):
    """GET /metrics — Prometheus scrape endpoint."""
    t0 = time.perf_counter()
    try:
        r = await client.get("/metrics")
        lat = (time.perf_counter() - t0) * 1000
        if not _ok_response(r):
            stats.record(OpResult("prometheus_metrics", lat, False, _parse_error(r)))
            return
        stats.record(OpResult("prometheus_metrics", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("prometheus_metrics", lat, False, str(e)))


@_covers(("GET", "/v1/search"))
async def op_global_search_get(client: httpx.AsyncClient, _: World, stats: Stats):
    """GET /search — cross-collection global search, query-string variant."""
    query = random.choice(QUERIES)
    t0 = time.perf_counter()
    try:
        r = await client.get(
            f"{API_PREFIX}/search",
            params={"q": query, "k": 5},
        )
        lat = (time.perf_counter() - t0) * 1000
        if _is_rate_limited(r):
            _record_rate_limited(stats, lat)
            return
        if not _ok_response(r):
            stats.record(OpResult("global_search_get", lat, False,
                                  _parse_error(r)))
            return
        stats.record(OpResult("global_search_get", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("global_search_get", lat, False, str(e)))


@_covers(("GET", "/v1/collections/{tenant}/{name}/queries/{query_id}"))
async def op_get_query_log_entry(
    client: httpx.AsyncClient, world: World, stats: Stats,
):
    """GET /collections/{tenant}/{coll}/queries/{id} — single log entry."""
    pair = await world.pick_query_id()
    if pair is None:
        return
    coll, qid = pair
    t0 = time.perf_counter()
    try:
        r = await client.get(
            f"{API_PREFIX}/collections/{TENANT}/{coll}/queries/{qid}",
        )
        lat = (time.perf_counter() - t0) * 1000
        if _is_rate_limited(r):
            _record_rate_limited(stats, lat)
            return
        if r.status_code == 404:
            stats.record(OpResult("get_query_log_entry", lat, True, "404"))
            return
        if not _ok_response(r):
            stats.record(OpResult("get_query_log_entry", lat, False,
                                  _parse_error(r)))
            return
        stats.record(OpResult("get_query_log_entry", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("get_query_log_entry", lat, False, str(e)))


@_covers(("POST", "/v1/admin/queries/{query_id}/replay"))
async def op_admin_replay_query(
    client: httpx.AsyncClient, world: World, stats: Stats,
):
    """POST /admin/queries/{id}/replay — exercises catalog.resolve_query_home
    to look up the collection, then replays. 404 is a valid race outcome."""
    pair = await world.pick_query_id()
    if pair is None:
        return
    _coll, qid = pair
    t0 = time.perf_counter()
    try:
        r = await client.post(f"{API_PREFIX}/admin/queries/{qid}/replay")
        lat = (time.perf_counter() - t0) * 1000
        if _is_rate_limited(r):
            _record_rate_limited(stats, lat)
            return
        if r.status_code == 404:
            stats.record(OpResult("admin_replay_query", lat, True, "404"))
            return
        if not _ok_response(r):
            stats.record(OpResult("admin_replay_query", lat, False,
                                  _parse_error(r)))
            return
        stats.record(OpResult("admin_replay_query", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("admin_replay_query", lat, False, str(e)))


@_covers(("DELETE", "/v1/admin/metrics"))
async def op_admin_reset_metrics(
    client: httpx.AsyncClient, _: World, stats: Stats,
):
    """DELETE /admin/metrics — zero out in-memory counters. Destructive on
    metrics state, so should run at a very low weight."""
    t0 = time.perf_counter()
    try:
        r = await client.delete(f"{API_PREFIX}/admin/metrics")
        lat = (time.perf_counter() - t0) * 1000
        if _is_rate_limited(r):
            _record_rate_limited(stats, lat)
            return
        if not _ok_response(r):
            stats.record(OpResult("admin_reset_metrics", lat, False,
                                  _parse_error(r)))
            return
        stats.record(OpResult("admin_reset_metrics", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("admin_reset_metrics", lat, False, str(e)))


# ---------------------------------------------------------------------------
# Weighted operation suites
# ---------------------------------------------------------------------------
# Each suite is a complete (func, name, weight) list. Weights are tuned for
# the suite's goal, not normalised across suites.
#
# - base: production-shaped traffic, kept stable so its throughput numbers
#   stay comparable to prior runs.
# - critical: same writers as base plus the race-prone paths that base
#   doesn't reach (rename via variadic write lock, reads racing with delete,
#   query-log retry, sidecar reads). Search weight is dropped to leave
#   headroom for the new ops.
# - full: exercises every reachable endpoint at least occasionally. Numbers
#   only comparable to other full runs.

OPERATIONS_BASE = [
    # (func, op_name, weight)
    # Read-heavy production shape, biased toward write pressure for stress.
    (op_search,              "search",            60),  # ~59%
    (op_ingest_small,        "ingest_small",      14),  # ~14%
    (op_ingest_large,        "ingest_chunked",     5),  # ~5%
    (op_delete_document,     "doc_delete",         6),  # ~6%
    (op_create_collection,   "collection_create",  4),  # ~4%
    (op_delete_collection,   "collection_delete",  2),  # ~2%
    (op_health_live,         "health_live",        4),  # ~4%
    (op_health,              "health",             2),  # ~2%
    (op_health_ready,        "health_ready",       1),  # ~1%
    (op_health_metrics,      "health_metrics",     1),  # ~1%
    (op_archive_download,    "archive_download",   1),  # ~1%
    (op_archive_restore,     "archive_restore",    1),  # ~1%
]

OPERATIONS_CRITICAL = [
    # Background load (lower than base so the race ops get real airtime).
    (op_search,              "search",            25),
    (op_ingest_small,        "ingest_small",      10),
    (op_ingest_large,        "ingest_chunked",     4),
    (op_delete_document,     "doc_delete",         8),
    (op_create_collection,   "collection_create",  5),
    (op_delete_collection,   "collection_delete",  4),
    # The race-critical adds. Recorded names match function name minus
    # `op_` prefix so that auto-discovery in `full` lines up.
    (op_rename_collection,   "rename_collection",  8),
    (op_list_documents,      "list_documents",     8),
    (op_list_collections,    "list_collections",   5),
    (op_query_log_list,      "query_log_list",     4),
    (op_query_replay,        "query_replay",       6),
    (op_get_chunk_content,   "get_chunk_content",  6),
    # Kept low: state-lock races + smoke.
    (op_archive_restore,     "archive_restore",    2),
    (op_archive_download,    "archive_download",   1),
    (op_health_live,         "health_live",        2),
    (op_health,              "health",             1),
    (op_health_ready,        "health_ready",       1),
    (op_health_metrics,      "health_metrics",     1),
]

# full is built by discovery: every module-level `op_*` coroutine that isn't
# already in critical gets added with _FULL_DEFAULT_WEIGHT. This way adding
# a new `async def op_xyz(...)` automatically extends `full` coverage without
# touching the suite list. Convention: the recorded OpResult name should be
# the function name minus the `op_` prefix.
_FULL_DEFAULT_WEIGHT = 2


def _discover_remaining_ops(already_in_suite, namespace):
    seen = {func for func, _name, _weight in already_in_suite}
    extras = []
    for name, obj in sorted(namespace.items()):
        if not name.startswith("op_"):
            continue
        if not asyncio.iscoroutinefunction(obj):
            continue
        if obj in seen:
            continue
        extras.append((obj, name[len("op_"):], _FULL_DEFAULT_WEIGHT))
    return extras


OPERATIONS_FULL = OPERATIONS_CRITICAL + _discover_remaining_ops(
    OPERATIONS_CRITICAL,
    globals(),
)

SUITES = {
    "base": OPERATIONS_BASE,
    "critical": OPERATIONS_CRITICAL,
    "full": OPERATIONS_FULL,
}


def _pick_operation(operations):
    ops, _, weights = zip(*operations)
    return random.choices(ops, weights=weights, k=1)[0]


# ---------------------------------------------------------------------------
# OpenAPI coverage check
# ---------------------------------------------------------------------------
# Endpoints intentionally skipped: FastAPI/Starlette built-ins, UI routes,
# segmented openapi files, and the root redirect — none worth bench coverage.
_COVERAGE_EXCLUDE_PATHS = {
    "/",
    "/favicon.ico",
}
_COVERAGE_EXCLUDE_PREFIXES = (
    "/openapi",
    "/docs",
    "/redoc",
    "/ui",
)


def _gather_covered_routes(namespace) -> set[tuple[str, str]]:
    """Union the @_covers(...) declarations from every op_* in the module."""
    covered: set[tuple[str, str]] = set()
    for name, obj in namespace.items():
        if not name.startswith("op_"):
            continue
        for method, path in getattr(obj, "__bench_covers__", ()):
            covered.add((method.upper(), path))
    return covered


def _is_excluded_path(path: str) -> bool:
    if path in _COVERAGE_EXCLUDE_PATHS:
        return True
    return any(path.startswith(p) for p in _COVERAGE_EXCLUDE_PREFIXES)


async def _print_coverage_gap(client: httpx.AsyncClient) -> None:
    """Fetch /openapi.json and warn for any endpoint no op_* declares."""
    try:
        r = await client.get("/openapi.json")
        if not _ok_response(r):
            return
        spec = r.json()
    except Exception:
        return
    paths = spec.get("paths") or {}
    covered = _gather_covered_routes(globals())
    gaps: list[tuple[str, str]] = []
    for path, methods in paths.items():
        if _is_excluded_path(path):
            continue
        for method in methods.keys():
            m = method.upper()
            if m in ("OPTIONS", "HEAD"):
                continue
            if (m, path) not in covered:
                gaps.append((m, path))
    if not gaps:
        return
    print(
        f"WARNING: {len(gaps)} endpoint(s) not covered by any op_* "
        "(add @_covers(...) on a new or existing op):"
    )
    for method, path in sorted(gaps):
        print(f"  {method:<6} {path}")
    print()


# ---------------------------------------------------------------------------
# Main driver
# ---------------------------------------------------------------------------
async def run_stress(
    base_url: str,
    duration_secs: int,
    concurrency: int,
    api_key: str | None = None,
    debug: bool = False,
    max_error_pct: float = 0,
    suite: str = "base",
) -> Stats | None:
    operations = SUITES.get(suite)
    if operations is None:
        raise ValueError(
            f"unknown suite '{suite}'; choose one of {sorted(SUITES)}"
        )
    stats = Stats()
    world = World()
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async with httpx.AsyncClient(
        base_url=base_url, timeout=60.0, headers=headers
    ) as client:
        await print_run_header(client, base_url, "stress")
        await _print_coverage_gap(client)
        # Seed a few collections so workers have something to target
        print(f"Seeding initial collections...")
        for i in range(3):
            name = f"s_seed{i}"
            r = await _post_with_retries(
                client,
                f"{API_PREFIX}/collections/{TENANT}/{name}",
            )
            _ensure_ok(r, "seed collection")
            await world.add_collection(name)
            # Ingest a few docs into each
            for j in range(3):
                docid = f"seed_{i}_{j}"
                text = SAMPLE_TEXTS[j % len(SAMPLE_TEXTS)]
                r = await _post_with_retries(
                    client,
                    f"{API_PREFIX}/collections/{TENANT}/{name}/documents",
                    files={"file": (f"{docid}.txt", text.encode(), "text/plain")},
                    data={"docid": docid},
                )
                _ensure_ok(r, "seed ingest")
                await world.add_doc(name, docid)

        print(f"Seeded 3 collections with 3 docs each.")
        print(
            f"Running stress test for {duration_secs}s with "
            f"concurrency={concurrency} suite={suite}..."
        )
        print()

        sem = asyncio.Semaphore(concurrency)
        stop = asyncio.Event()
        ops_started = 0

        async def worker():
            nonlocal ops_started
            while not stop.is_set():
                async with sem:
                    if stop.is_set():
                        break
                    op = _pick_operation(operations)
                    ops_started += 1
                    await op(client, world, stats)

        t_start = time.perf_counter()

        # Launch workers (more than concurrency so the semaphore is always saturated)
        tasks = [asyncio.create_task(worker()) for _ in range(concurrency * 3)]

        # Run for the specified duration
        await asyncio.sleep(duration_secs)
        stop.set()

        # Let in-flight operations finish (up to 30s grace)
        _, pending = await asyncio.wait(tasks, timeout=30)
        for t in pending:
            t.cancel()

        elapsed = time.perf_counter() - t_start

        # Coverage pass: run any op that was never picked during the timed phase.
        seen_ops = {r.op for r in stats.results}
        missed = [(op, name) for op, name, _ in operations if name not in seen_ops]
        if missed:
            missed_names = ", ".join(name for _op, name in missed)
            print(
                f"Coverage pass ({len(missed)} op(s) not seen): "
                f"{missed_names}"
            )
            for op, name in missed:
                before = len(stats.results)
                t_cov = time.perf_counter()
                print(f"  -> coverage op: {name}")
                await op(client, world, stats)
                cov_ms = (time.perf_counter() - t_cov) * 1000
                recorded = len(stats.results) > before
                status = "recorded" if recorded else "no-result"
                print(f"  <- coverage op: {name} [{status}] {cov_ms:.1f}ms")

        # Cleanup: delete all test collections
        print("Cleaning up...")
        for name in await world.snapshot_collections():
            try:
                await client.delete(f"{API_PREFIX}/collections/{TENANT}/{name}")
            except Exception:
                pass

    # ---------------------------------------------------------------------------
    # Report
    # ---------------------------------------------------------------------------
    summary = stats.summary()
    total_ops = sum(v["count"] for v in summary.values())
    total_errs = sum(v["errors"] for v in summary.values())

    print()
    print("=" * 94)
    print(f"  STRESS TEST RESULTS  ({elapsed:.1f}s elapsed)")
    print("=" * 94)
    print(f"  Total operations : {total_ops}")
    print(f"  Throughput       : {total_ops / elapsed:.1f} ops/s")
    err_rate = 100 * total_errs / max(total_ops, 1)
    print(f"  Total errors     : {total_errs}  ({err_rate:.1f}%)")
    print()

    header = (
        f"{'Operation':<22} {'Count':>6} {'OK':>6} {'Err (%)':>11} "
        f"{'Min':>9} {'p50':>9} {'p95':>9} {'p99':>9} {'Max':>9}"
    )
    print(header)
    print("-" * len(header))
    for op, s in summary.items():
        err_str = f"{s['errors']} ({100 * s['errors'] / max(s['count'], 1):.1f}%)"
        print(
            f"{op:<22} {s['count']:>6} {s['ok']:>6} {err_str:>11} "
            f"{s['min_ms']:>8.1f}ms {s['p50_ms']:>8.1f}ms "
            f"{s['p95_ms']:>8.1f}ms {s['p99_ms']:>8.1f}ms {s['max_ms']:>8.1f}ms"
        )
    print("-" * len(header))
    print()

    if total_errs > 0:
        print("Sample errors:")
        errors = [r for r in stats.results if not r.ok]
        for e in errors[:10]:
            print(f"  [{e.op}] {e.detail[:120]}")
        if len(errors) > 10:
            print(f"  ... and {len(errors) - 10} more")
        print()

    violation = _error_rate_violation(
        total_ops,
        err_rate,
        max_error_pct,
    )
    if violation is not None:
        print(f"\n{violation}")
        return None

    return stats


def _error_rate_violation(
    total_ops: int,
    err_rate: float,
    max_error_pct: float,
) -> str | None:
    if max_error_pct <= 0 or total_ops <= 0:
        return None
    if err_rate <= max_error_pct:
        return None
    return (
        "ERROR RATE VIOLATION:"
        f" {err_rate:.1f}%"
        f" > {max_error_pct:.1f}%"
    )


def main():
    parser = argparse.ArgumentParser(description="PaveDB stress test")
    parser.add_argument(
        "--url",
        default="http://localhost:8086",
        help="PaveDB base URL",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=20,
        help="Test duration in seconds",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="Max concurrent operations",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help=(
            "Bearer token for the 'stress' tenant "
            "(omit when server uses auth.mode=none)"
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print stack traces for setup failures",
    )
    parser.add_argument(
        "--max-error-pct",
        type=float,
        default=0,
        help=(
            "Fail (exit 1) if error %% exceeds this."
            " 0 = disabled."
        ),
    )
    parser.add_argument(
        "--suite",
        choices=sorted(SUITES),
        default="base",
        help=(
            "Operation suite. 'base' is the historical mix (comparable to "
            "prior runs). 'critical' adds race-prone reads + rename. 'full' "
            "exercises every API endpoint."
        ),
    )
    args = parser.parse_args()

    try:
        result = asyncio.run(run_stress(
            args.url,
            args.duration,
            args.concurrency,
            api_key=args.api_key,
            debug=args.debug,
            max_error_pct=args.max_error_pct,
            suite=args.suite,
        ))
    except Exception:
        if args.debug:
            print(traceback.format_exc())
        raise
    if result is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
