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

# compare hybrid retrieval with the default vector run
python benchmarks/search_latency.py --url http://localhost:8086 --mode hybrid
```

### Options

* `--url` - PaveDB base URL (default: http://localhost:8086)
* `--queries` - Number of queries to run (default: 100)
* `--concurrency` - Concurrent requests (default: 10)
* `--mode` - Candidate generation mode: `vector` or `hybrid` (default: vector)
* `--debug` - Print stack traces for setup failures

### Output

Reports min, max, mean, p50, p95, p99 latencies in milliseconds.
Setup requests (collection create + seed ingest) retry a few times before
failing. Timed queries are not retried.

---

## filtered_search.py

Compares the vector-first expanding-window fallback with P1-71 backend-aware
filtered search across several allowed-ID selectivities:

```bash
PYTHONPATH=. python benchmarks/filtered_search.py

# smaller local smoke run
PYTHONPATH=. python benchmarks/filtered_search.py \
  --count 5000 --dimension 64 --queries 10 \
  --selectivities 0.001,0.01,0.1
```

The report includes overfetch count, estimated vector-score work, p50/p95
latency, and peak traced Python/NumPy allocation for both paths. It also checks
that every measured query returns the same ordered IDs.

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

---

## chaos.py

Correctness + concurrency suite for P1-52. It checks the invariants that
`stress.py` cannot see: count drift, search visibility, cross-collection bleed,
clean 4xx handling for malformed inputs, and stable-doc retrieval under churn.
Scheduled CI runs it nightly without blocking release pipelines, and managed
runs also check catalog rows against collection directories after churn.

```bash
# run with defaults (seed=1337, 120s chaos phase, 24 churn threads)
make chaos

# customize (CHAOS_SEED makes a run reproducible)
make chaos CHAOS_SEED=42 CHAOS_DURATION=60 CHAOS_CONCUR=16

# or directly against a running server
python benchmarks/chaos.py --url http://localhost:8086 --seed 1337 \
  --duration 20 --concurrency 8
```

Thread interleaving is inherently nondeterministic; `--seed` fixes the collection
names and op distribution so a finding is reproducible.

## Saving results

To save outputs with a UTC timestamp and tag:

```bash
make benchmark BENCH_SAVE=1 BENCH_TAG=baseline
```

If `BENCH_TAG` is omitted, a `<branch>-<shortsha>` tag is used. Outputs are saved
under `benchmarks/results/` as:

```
{latency,stress}-YYYY-MM-DD_HHmmss_<tag>.txt
server-pavedb-bench.XXXXXX.log                 # failed runs only
```

A run that ends non-zero against its own ephemeral server keeps that server's
data_dir and prints the `$TMPDIR` path of its log; with `BENCH_SAVE=1` the log
is copied in beside the results too, which is the only copy that outlives a CI
container.

Health probes have a two-second request/connect limit. Managed startup uses
an elapsed `BENCH_STARTUP_TIMEOUT_S` budget (30 seconds locally, 120 in CI),
including probe time, and fails early if the server exits. Teardown gives only
the server started by the harness five seconds after TERM, then sends KILL
and checks for exit for one more second. Startup and teardown failures save
the server log as above; cleanup retains an earlier failure status.
Deadlines use a monotonic clock: wall-clock corrections cannot shorten startup
or extend teardown. Phase diagnostics print monotonic elapsed milliseconds,
wall elapsed/Unix milliseconds and Bash `SECONDS`, so guest clock changes can
be distinguished from runner trace timestamps. A completed model-loading
progress bar does not establish that warmup finished or HTTP became available.

Stress keeps its 60-second HTTP I/O timeout and gives the complete setup
(header, coverage discovery, and seeding) 120 seconds. `STR_LENGTH` still
sets only the worker duration. Workers have 30 seconds to drain and five
seconds to finish cancellation; unfinished or failed workers fail the run.
The complete coverage pass has 120 seconds, collection cleanup 30 seconds,
and HTTP client close five seconds. These deadlines fail the benchmark;
they do not skip operations or relax the error-rate gate. Phase progress,
elapsed times, and failures are flushed through `tee`, with stderr included
in the saved stress report. Failed managed runs retain their server log.

Generated outputs are gitignored: keep them local or publish them as CI
artifacts; commit only curated benchmark comparisons to documentation.
