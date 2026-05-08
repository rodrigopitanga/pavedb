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


# ---------------------------------------------------------------------------
# Individual operations
# ---------------------------------------------------------------------------
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
        stats.record(OpResult("search", lat, True))
    except Exception as e:
        lat = (time.perf_counter() - t0) * 1000
        stats.record(OpResult("search", lat, False, str(e)))


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
# Weighted operation dispatcher
# ---------------------------------------------------------------------------
# Weights control how often each operation is chosen.
OPERATIONS = [
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


def _pick_operation():
    ops, _, weights = zip(*OPERATIONS)
    return random.choices(ops, weights=weights, k=1)[0]


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
) -> Stats | None:
    stats = Stats()
    world = World()
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async with httpx.AsyncClient(
        base_url=base_url, timeout=60.0, headers=headers
    ) as client:
        await print_run_header(client, base_url, "stress")
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
            f"concurrency={concurrency}..."
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
                    op = _pick_operation()
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
        missed = [(op, name) for op, name, _ in OPERATIONS if name not in seen_ops]
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
            "Fail (exit 1) if error % exceeds this."
            " 0 = disabled."
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
        ))
    except Exception:
        if args.debug:
            print(traceback.format_exc())
        raise
    if result is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
