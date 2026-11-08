#!/usr/bin/env python3
# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Search latency benchmark for PaveDB.

Usage:
    python benchmarks/search_latency.py [options]

Options:
    --url URL            PaveDB base URL
    --queries N          Number of queries (default 100)
    --concurrency C      Concurrent requests (default 10)
    --filtering MODE     none/exact/wildcard/mixed
                         (default: mixed)
    --mode MODE          vector/hybrid (default: vector)

Indexes sample docs with metadata, fires concurrent
searches, and reports latency percentiles.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import traceback

try:
    import httpx
except ImportError:
    raise SystemExit("httpx required: pip install httpx")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (  # type: ignore[import]  # noqa: E402
    host_load,
    print_run_header,
)

API_PREFIX = "/v1"

SAMPLE_DOCS = [
    (
        "doc1",
        "Machine learning is a subset of artificial intelligence that "
        "enables systems to learn from data.",
        {"lang": "en", "category": "ml"},
    ),
    (
        "doc2",
        "Natural language processing helps computers understand human "
        "language and text.",
        {"lang": "en", "category": "nlp"},
    ),
    (
        "doc3",
        "Deep learning uses neural networks with many layers to model "
        "complex patterns.",
        {"lang": "en", "category": "ml"},
    ),
    (
        "doc4",
        "Vector databases store embeddings for efficient similarity "
        "search operations.",
        {"lang": "en", "category": "infra"},
    ),
    (
        "doc5",
        "Semantic search finds results based on meaning rather than "
        "exact keyword matches.",
        {"lang": "en", "category": "infra"},
    ),
    (
        "doc6",
        "Transformers revolutionized NLP with attention mechanisms and "
        "parallel processing.",
        {"lang": "en", "category": "nlp"},
    ),
    (
        "doc7",
        "Embeddings represent text as dense vectors in high-dimensional "
        "space.",
        {"lang": "en", "category": "infra"},
    ),
    (
        "doc8",
        "Retrieval augmented generation combines search with language "
        "model outputs.",
        {"lang": "en", "category": "nlp"},
    ),
    (
        "doc9",
        "Cosine similarity measures the angle between two vectors for "
        "comparison.",
        {"lang": "pt", "category": "math"},
    ),
    (
        "doc10",
        "Fine-tuning adapts pre-trained models to specific domains and "
        "tasks.",
        {"lang": "pt", "category": "ml"},
    ),
]

EXACT_FILTERS = [
    {"lang": "en"},
    {"category": "ml"},
    {"category": "nlp"},
    {"lang": "en", "category": "infra"},
    {"lang": "pt"},
]

WILDCARD_FILTERS = [
    {"category": "ml*"},
    {"category": "*lp"},
    {"lang": "e*"},
    {"category": "*nfra"},
    {"lang": "p*"},
]

MIXED_FILTERS = [
    {"lang": "en", "category": "ml*"},
    {"lang": "p*", "category": "nlp"},
    {"category": "*nfra", "lang": "e*"},
    {"lang": "en", "category": "*ath"},
    {"lang": "p*", "category": "ml"},
]

_FILTERS_FOR: dict[str, list[dict] | None] = {
    "none": None,
    "exact": EXACT_FILTERS,
    "wildcard": WILDCARD_FILTERS,
    "mixed": MIXED_FILTERS,
}

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
]


def percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * p / 100
    f = int(k)
    c = f + 1 if f + 1 < len(sorted_data) else f
    return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])


def _phase_start() -> tuple[int, int]:
    return time.monotonic_ns(), time.time_ns()


def _phase_done(name: str, started: tuple[int, int]) -> None:
    monotonic_ns, wall_ns = started
    elapsed_ms = (time.monotonic_ns() - monotonic_ns) // 1_000_000
    wall_elapsed_ms = (time.time_ns() - wall_ns) // 1_000_000
    print(
        f"==> latency {name}: elapsed_ms={elapsed_ms} "
        f"wall_elapsed_ms={wall_elapsed_ms}",
        flush=True,
    )
    if abs(wall_elapsed_ms - elapsed_ms) >= 1000:
        print(
            f"WARNING: runner clock divergence during latency {name}",
            flush=True,
        )


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
        if resp.status_code < 400:
            return resp
        if i < attempts - 1:
            await asyncio.sleep(sleep_s * (i + 1))
    assert last_resp is not None
    return last_resp


async def setup_collection(
    client: httpx.AsyncClient,
    tenant: str,
    collection: str,
    attempts: int = 3,
):
    """Create collection and index sample documents."""
    resp = await _post_with_retries(
        client,
        f"{API_PREFIX}/collections/{tenant}/{collection}",
        attempts=attempts,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"create collection failed: {_parse_error(resp)}"
        )
    for index, (docid, text, meta) in enumerate(SAMPLE_DOCS, 1):
        print(
            f"==> latency seed {index}/{len(SAMPLE_DOCS)}: {docid}",
            flush=True,
        )
        seed_start = _phase_start()
        data = {"docid": docid, "metadata": json.dumps(meta)}
        try:
            resp = await _post_with_retries(
                client,
                f"{API_PREFIX}/collections/{tenant}/{collection}/documents",
                attempts=attempts,
                files={
                    "file": (
                        f"{docid}.txt",
                        text.encode(),
                        "text/plain",
                    )
                },
                data=data,
            )
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"seed ingest failed: {_parse_error(resp)}"
                )
        finally:
            _phase_done(f"seed {docid}", seed_start)


def _parse_error(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        code = payload.get("code")
        error = payload.get("error")
        if code or error:
            return (
                f"{code or 'error'}: "
                f"{error or 'request failed'}"
            )
    return f"http_{resp.status_code}"


async def search(
    client: httpx.AsyncClient,
    tenant: str,
    collection: str,
    query: str,
    filters: dict | None = None,
    mode: str = "vector",
    require_rate_limit: bool = False,
) -> tuple[float, bool | None, str, list[dict]]:
    """Return (latency_ms, ok, detail, hits).

    ok=None for 429 (rate-limited).
    """
    body: dict[str, object] = {"q": query, "k": 5, "mode": mode}
    if filters:
        body["filters"] = filters

    start = time.perf_counter()
    r = await client.post(
        f"{API_PREFIX}/collections/{tenant}/{collection}/search",
        json=body,
    )
    latency_ms = (time.perf_counter() - start) * 1000
    if r.status_code == 429:
        return latency_ms, None, "rate_limited", []
    if r.status_code >= 400:
        return latency_ms, False, _parse_error(r), []
    if require_rate_limit:
        try:
            limit = int(r.headers["X-RateLimit-Limit"])
            window = int(r.headers["X-RateLimit-Window"])
            remaining = int(r.headers["X-RateLimit-Remaining"])
        except (KeyError, ValueError):
            return latency_ms, False, "missing request-rate limit headers", []
        if limit <= 0 or window != 60 or not 0 <= remaining < limit:
            return latency_ms, False, "invalid request-rate limit headers", []
    return latency_ms, True, "", r.json().get("matches", [])


async def run_benchmark(
    base_url: str,
    num_queries: int,
    concurrency: int,
    api_key: str | None = None,
    debug: bool = False,
    *,
    filtering: str = "mixed",
    mode: str = "vector",
    summary_line: str | None = None,
    slo_p99_ms: float = 0,
    require_rate_limit: bool = False,
) -> list[float] | None:
    bench_start = time.perf_counter()
    tenant = "bench"
    collection = f"lat-{int(time.time())}"
    headers = (
        {"Authorization": f"Bearer {api_key}"}
        if api_key else {}
    )
    filters_pool = _FILTERS_FOR[filtering]
    label = (
        "search" if filtering == "none"
        else f"search_{filtering}"
    )
    if mode != "vector":
        label = f"{mode}_{label}"

    if require_rate_limit:
        label = f"rate_limited_{label}"

    print(f"==> latency starting: {label}", flush=True)
    async with httpx.AsyncClient(
        base_url=base_url, timeout=30.0, headers=headers
    ) as client:
        phase_start = _phase_start()
        try:
            await print_run_header(
                client, base_url, "search_latency_rate_limited"
                if require_rate_limit else "search_latency"
            )
        finally:
            _phase_done("header", phase_start)
        print(
            f"Setting up collection {tenant}/{collection}..."
        )
        phase_start = _phase_start()
        try:
            try:
                await setup_collection(
                    client, tenant, collection
                )
            finally:
                _phase_done("setup", phase_start)
        except RuntimeError as exc:
            print(f"Setup failed: {exc}")
            if debug:
                print(traceback.format_exc())
            return None if require_rate_limit else []
        print(f"Indexed {len(SAMPLE_DOCS)} documents.")
        print(
            f"Running {num_queries} queries "
            f"with concurrency={concurrency} mode={mode}..."
        )

        semaphore = asyncio.Semaphore(concurrency)

        async def bounded_search(
            query: str, filt: dict | None,
        ) -> tuple[float, bool | None, str, list[dict]]:
            async with semaphore:
                return await search(
                    client, tenant, collection,
                    query, filters=filt, mode=mode,
                    require_rate_limit=require_rate_limit,
                )

        query_list = [
            QUERIES[i % len(QUERIES)]
            for i in range(num_queries)
        ]
        if filters_pool:
            filter_list: list[dict | None] = [
                filters_pool[i % len(filters_pool)]
                for i in range(num_queries)
            ]
        else:
            filter_list = [None] * num_queries

        tasks = [
            bounded_search(q, f)
            for q, f in zip(query_list, filter_list)
        ]
        phase_start = _phase_start()
        try:
            results = await asyncio.gather(*tasks)
        finally:
            _phase_done("queries", phase_start)

        latencies: list[float] = []
        errors: list[str] = []
        rate_limited = 0
        total_hits = 0
        samples: dict[str, list[dict]] = {}

        for (lat, ok, detail, hits), query in zip(
            results, query_list
        ):
            if ok is True:
                latencies.append(lat)
                total_hits += len(hits)
                if (len(samples) < 3
                        and query not in samples and hits):
                    samples[query] = hits
            elif ok is None:
                rate_limited += 1
            else:
                errors.append(detail)

        # Report
        elapsed = time.perf_counter() - bench_start
        total = len(results)
        err_count = len(errors)
        err_pct = 100 * err_count / max(total, 1)

        print()
        print("=" * 94)
        print(
            f"  SEARCH LATENCY RESULTS  "
            f"({elapsed:.1f}s elapsed)"
        )
        print("=" * 94)
        print(f"  Total queries  : {total}")
        print(f"  Total hits     : {total_hits}")
        thrpt = total / elapsed if elapsed > 0 else 0
        print(f"  Throughput     : {thrpt:.1f} ops/s")
        print(f"  Concurrency    : {concurrency}")
        if rate_limited:
            rl_pct = 100 * rate_limited / max(total, 1)
            print(
                f"  Rate limited   : {rate_limited}"
                f" ({rl_pct:.1f}%)"
                " - raise tenants.default_max_concurrent"
                " or use auth.mode=none"
            )
        print(
            f"  Errors         : {err_count}"
            f" ({err_pct:.1f}%)"
        )
        print()

        header = (
            f"{'Operation':<22} {'Count':>6} {'OK':>6} "
            f"{'Hits':>8} {'Err (%)':>11} "
            f"{'Min':>9} {'p50':>9} {'p95':>9} "
            f"{'p99':>9} {'Max':>9}"
        )
        print(header)
        print("-" * len(header))
        if latencies:
            ok_count = len(latencies)
            e_str = f"{err_count} ({err_pct:.1f}%)"
            print(
                f"{label:<22} {total:>6} "
                f"{ok_count:>6} {total_hits:>8} "
                f"{e_str:>11} "
                f"{min(latencies):>8.1f}ms "
                f"{percentile(latencies, 50):>8.1f}ms "
                f"{percentile(latencies, 95):>8.1f}ms "
                f"{percentile(latencies, 99):>8.1f}ms "
                f"{max(latencies):>8.1f}ms"
            )
        else:
            print("  No successful queries to report.")
        print("-" * len(header))
        print()

        if samples:
            print("Sample results:")
            for query, hits in samples.items():
                print(f'  q: "{query}"')
                for hit in hits[:2]:
                    text = hit.get("text") or ""
                    excerpt = (
                        text[:90] + "..."
                        if len(text) > 90 else text
                    )
                    rid = hit.get("id", "?")
                    score = hit.get("score", 0)
                    print(
                        f"     [{rid}  {score:.3f}]"
                        f"  {excerpt}"
                    )
            print()

        if errors:
            print("Sample errors:")
            for detail in errors[:5]:
                print(f"  - {detail}")
            print()

        # Cleanup
        phase_start = _phase_start()
        try:
            await client.delete(
                f"{API_PREFIX}/collections/{tenant}/{collection}"
            )
        finally:
            _phase_done("cleanup", phase_start)
        print(
            f"\nCleaned up collection"
            f" {tenant}/{collection}"
        )

        if summary_line:
            with open(summary_line, "a") as sl:
                if latencies:
                    sl.write(
                        f"{label}|{total}"
                        f"|{len(latencies)}"
                        f"|{total_hits}"
                        f"|{err_count}"
                        f"|{err_pct:.1f}"
                        f"|{min(latencies):.1f}"
                        f"|{percentile(latencies, 50):.1f}"
                        f"|{percentile(latencies, 95):.1f}"
                        f"|{percentile(latencies, 99):.1f}"
                        f"|{max(latencies):.1f}"
                        f"|{thrpt:.1f}\n"
                    )
                else:
                    sl.write(
                        f"{label}|{total}|0"
                        f"|{total_hits}"
                        f"|{err_count}"
                        f"|{err_pct:.1f}"
                        f"|0|0|0|0|0|0\n"
                    )

        if require_rate_limit and (
            not latencies or errors or rate_limited
        ):
            print("RATE-LIMIT COVERAGE VIOLATION: every query must succeed "
                  "with request-rate limit headers")
            return None

        violation = _latency_slo_violation(
            latencies,
            slo_p99_ms,
        )
        if violation is not None:
            print(f"\n{violation}")
            print(_slo_tail_report(latencies, slo_p99_ms))
            return None

        return latencies


def _latency_slo_violation(
    latencies: list[float],
    slo_p99_ms: float,
) -> str | None:
    if slo_p99_ms <= 0 or not latencies:
        return None
    p99 = percentile(latencies, 99)
    if p99 <= slo_p99_ms:
        return None
    return (
        f"SLO VIOLATION: p99={p99:.1f}ms"
        f" > {slo_p99_ms:.1f}ms"
    )


def _slo_tail_report(
    latencies: list[float],
    slo_p99_ms: float,
    slowest: int = 5,
) -> str:
    """Describe the tail that broke the gate, under the verdict.

    p99 over the gate's 200 samples interpolates between the third- and
    second-worst, at a hundredth of the gap, so a lone slow request barely
    moves it: a red p99 here means at least the slowest concurrency wave
    stalled together. Whether the server stalled it or the box it shares
    did is the first question, and the results table stops at p95/p99/max —
    it says neither how many samples cleared the threshold nor how far the
    mean sits above p95. Both go here, with the host queue that decides it.
    """
    total = len(latencies)
    if not total:
        return ""
    ranked = sorted(latencies, reverse=True)
    over = [lat for lat in ranked if lat > slo_p99_ms]
    lines = [
        f"    over SLO  : {len(over)} of {total} samples"
        f" ({100 * len(over) / total:.1f}%)",
        "    slowest   : "
        + ", ".join(f"{lat:.1f}ms" for lat in ranked[:slowest]),
        f"    mean/p95  : {sum(latencies) / total:.1f}ms"
        f" / {percentile(latencies, 95):.1f}ms",
    ]
    load = host_load()
    if load:
        lines.append(f"    host load : {load}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="PaveDB search latency benchmark"
    )
    parser.add_argument(
        "--url",
        default="http://localhost:8086",
        help="PaveDB base URL",
    )
    parser.add_argument(
        "--queries",
        type=int,
        default=100,
        help="Number of queries",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=10,
        help="Concurrent requests",
    )
    parser.add_argument(
        "--filtering",
        choices=["none", "exact", "wildcard", "mixed"],
        default="mixed",
        help="Filter mode (default: mixed)",
    )
    parser.add_argument(
        "--mode",
        choices=["vector", "hybrid"],
        default="vector",
        help="Candidate generation mode (default: vector)",
    )
    parser.add_argument(
        "--summary-line",
        default=None,
        help="Append pipe-delimited summary to file",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help=(
            "Bearer token for the 'bench' tenant "
            "(omit when server uses auth.mode=none)"
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print stack traces for setup failures",
    )
    parser.add_argument(
        "--slo-p99-ms",
        type=float,
        default=0,
        help=(
            "Fail (exit 1) if p99 exceeds this (ms)."
            " 0 = disabled."
        ),
    )
    parser.add_argument(
        "--require-rate-limit",
        action="store_true",
        help="Require every query to succeed through a per-minute tenant limit",
    )
    args = parser.parse_args()

    result = asyncio.run(run_benchmark(
        args.url,
        args.queries,
        args.concurrency,
        api_key=args.api_key,
        debug=args.debug,
        filtering=args.filtering,
        mode=args.mode,
        summary_line=args.summary_line,
        slo_p99_ms=args.slo_p99_ms,
        require_rate_limit=args.require_rate_limit,
    ))
    if result is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
