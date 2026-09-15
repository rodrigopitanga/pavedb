# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Compare vector-first overfetch with backend-filtered FAISS search."""

from __future__ import annotations

import argparse
import math
import statistics
import tempfile
import time
import tracemalloc
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypeVar

import numpy as np

from pave.backends.faiss import FaissBackend


T = TypeVar("T")


def _legacy_filtered_search(
    backend: FaissBackend,
    vector: np.ndarray,
    k: int,
    allowed_rids: set[str],
) -> tuple[list[tuple[str, float]], int, int]:
    """Model the vector-first expanding-window fallback."""
    total = backend.count()
    if total <= 0 or not allowed_rids:
        return [], 0, 0

    limit = min(max(50, k * 5), total)
    calls = 0
    while True:
        raw = backend.search(vector, limit)
        calls += 1
        kept = [hit for hit in raw if hit[0] in allowed_rids]
        if len(kept) >= k or limit >= total or len(raw) < limit:
            return kept[:k], calls, limit
        limit = min(max(limit * 2, limit + k), total)


def _measure(
    operation: Callable[[np.ndarray], T],
    queries: Sequence[np.ndarray],
) -> tuple[list[T], list[float], list[int]]:
    outputs: list[T] = []
    elapsed_ms: list[float] = []
    peak_bytes: list[int] = []
    for query in queries:
        tracemalloc.start()
        started = time.perf_counter()
        outputs.append(operation(query))
        elapsed_ms.append((time.perf_counter() - started) * 1000.0)
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        peak_bytes.append(peak)
    return outputs, elapsed_ms, peak_bytes


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return float(ordered[index])


def _parse_selectivities(value: str) -> list[float]:
    try:
        ratios = [
            float(part.strip())
            for part in value.split(",")
            if part.strip()
        ]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "selectivities must be comma-separated values in (0, 1]"
        ) from exc
    if not ratios or any(ratio <= 0.0 or ratio > 1.0 for ratio in ratios):
        raise argparse.ArgumentTypeError(
            "selectivities must be comma-separated values in (0, 1]"
        )
    return ratios


def run_benchmark(
    *,
    count: int,
    dimension: int,
    k: int,
    query_count: int,
    selectivities: Sequence[float],
    seed: int,
) -> list[dict[str, float | int]]:
    rng = np.random.default_rng(seed)
    vectors = rng.standard_normal((count, dimension)).astype(np.float32)
    queries = [
        row
        for row in rng.standard_normal((query_count, dimension)).astype(
            np.float32
        )
    ]
    rids = [f"rid-{index}" for index in range(count)]
    order = rng.permutation(count)
    rows: list[dict[str, float | int]] = []

    with tempfile.TemporaryDirectory(prefix="pavedb-filtered-search-") as tmp:
        backend = FaissBackend(dimension, storage_dir=Path(tmp) / "index")
        backend.add(rids, vectors)
        try:
            for ratio in selectivities:
                eligible = min(count, max(k, math.ceil(count * ratio)))
                allowed = {rids[int(index)] for index in order[:eligible]}

                legacy, legacy_ms, legacy_peak = _measure(
                    lambda query: _legacy_filtered_search(
                        backend,
                        query,
                        k,
                        allowed,
                    ),
                    queries,
                )
                filtered, filtered_ms, filtered_peak = _measure(
                    lambda query: backend.search_filtered(
                        query,
                        k,
                        allowed,
                    ),
                    queries,
                )

                for legacy_result, filtered_result in zip(
                    legacy,
                    filtered,
                    strict=False,
                ):
                    if [rid for rid, _score in legacy_result[0]] != [
                        rid for rid, _score in filtered_result
                    ]:
                        raise RuntimeError(
                            "filtered search changed the result ordering"
                        )

                calls = [result[1] for result in legacy]
                rows.append(
                    {
                        "selectivity": eligible / count,
                        "eligible": eligible,
                        "legacy_overfetch": statistics.mean(calls) - 1.0,
                        "legacy_vector_work": count * sum(calls),
                        "filtered_vector_work": eligible * query_count,
                        "legacy_p50_ms": statistics.median(legacy_ms),
                        "filtered_p50_ms": statistics.median(filtered_ms),
                        "legacy_p95_ms": _percentile(legacy_ms, 0.95),
                        "filtered_p95_ms": _percentile(filtered_ms, 0.95),
                        "legacy_peak_kib": max(legacy_peak) / 1024.0,
                        "filtered_peak_kib": max(filtered_peak) / 1024.0,
                    }
                )
        finally:
            backend.discard()
            backend.close()
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=20_000)
    parser.add_argument("--dimension", type=int, default=128)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--queries", type=int, default=20)
    parser.add_argument(
        "--selectivities",
        type=_parse_selectivities,
        default=_parse_selectivities("0.001,0.01,0.1,0.5"),
    )
    parser.add_argument("--seed", type=int, default=71)
    args = parser.parse_args()
    if min(args.count, args.dimension, args.k, args.queries) <= 0:
        parser.error("count, dimension, k, and queries must be positive")
    if args.k > args.count:
        parser.error("k cannot exceed count")

    rows = run_benchmark(
        count=args.count,
        dimension=args.dimension,
        k=args.k,
        query_count=args.queries,
        selectivities=args.selectivities,
        seed=args.seed,
    )
    columns = list(rows[0])
    print("\t".join(columns))
    for row in rows:
        print(
            "\t".join(
                f"{row[column]:.3f}"
                if isinstance(row[column], float)
                else str(row[column])
                for column in columns
            )
        )


if __name__ == "__main__":
    main()
