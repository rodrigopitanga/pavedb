<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Benchmarks

Performance benchmarks for PaveDB.

## Quick start

```bash
# Uses active http://127.0.0.1:8086 if available.
# If not, starts an ephemeral local server with a temporary data_dir.
# In `make benchmark`, each bench gets a fresh ephemeral server.
make benchmark

# or run individually
make bench-latency
make bench-stress

# force ephemeral mode even if :8086 is active
BENCH_FORCE_EPHEMERAL=1 make benchmark

# target a server on another machine
make benchmark BENCH_SERVER_URL=http://10.0.0.42:8086

# if remote server requires bearer auth
make benchmark BENCH_SERVER_URL=http://10.0.0.42:8086 BENCH_API_KEY=your-token
```

---

## search_latency.py

Measures search latency under concurrent load.

### Developer workflow

```bash
# run with defaults (1200 queries, 42 concurrent)
make bench-latency

# or customize
make bench-latency BENCH_QUERIES=500 BENCH_CONCUR=20
```

### PyPI / evaluation workflow

```bash
# script defaults (queries=100, concurrency=10)
python benchmarks/search_latency.py --url http://localhost:8086

# match `make bench-latency` defaults
python benchmarks/search_latency.py --url http://localhost:8086 \
  --queries 1200 --concurrency 42
```

### Options

* `--url` - PaveDB base URL (default: http://localhost:8086)
* `--queries` - Number of queries to run (default: 100)
* `--concurrency` - Concurrent requests (default: 10)
* `--debug` - Print stack traces for setup failures

### Output

Reports min, max, mean, p50, p95, p99 latencies in milliseconds.
Setup requests (collection create + seed ingest) retry a few times before
failing. Timed queries are not retried.

---

## stress.py

Fires random concurrent operations (collection create/delete, document ingest/delete,
search, health checks, archive download/restore) and reports per-operation latency
percentiles plus error rates.

### Developer workflow

```bash
# run with defaults (90s duration, 8 concurrent)
make bench-stress

# or customize
make bench-stress STRESS_DURATION=60 STRESS_CONCUR=30
```

### PyPI / evaluation workflow

```bash
# script defaults (duration=20, concurrency=8)
python benchmarks/stress.py --url http://localhost:8086

# match `make bench-stress` defaults
python benchmarks/stress.py --url http://localhost:8086 --duration 90 \
  --concurrency 8
```

### Options

* `--url` - PaveDB base URL (default: http://localhost:8086)
* `--duration` - Test duration in seconds (default: 20)
* `--concurrency` - Max concurrent operations (default: 8)
* `--debug` - Print stack traces for setup failures

### Output

Reports per-operation counts, error rates, and p50/p95/p99/max latencies.
Seed collection + ingest steps retry a few times before aborting. Timed
ingest/search operations during the run are not retried.

## Saving results

To save outputs with a UTC timestamp and tag:

```bash
make benchmark BENCH_SAVE=1 BENCH_TAG=baseline
```

If `BENCH_TAG` is omitted, a `<branch>-<shortsha>` tag is used. Outputs are saved
under `benchmarks/results/` as:

```
{latency,stress}-YYYY-MM-DD_HHmmss_<tag>.txt
```
