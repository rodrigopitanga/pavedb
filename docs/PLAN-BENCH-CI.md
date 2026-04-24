<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Benchmark CI Gate (P2-30)

Automated benchmark runs in CI with configurable SLO
thresholds. Pipelines fail when latency or error rate
exceeds limits.

## SLO Thresholds

| Variable | Script flag | Scope | Default (CI) | Gate |
|----------|-------------|-------|--------------|------|
| `LAT_SLO_P99_MS` | `--slo-p99-ms` | search p99 latency | 2000 ms | hard |
| `STR_MAX_ERROR_PCT` | `--max-error-pct` | stress error rate | 1% | hard |

Both gates are hard failures — search latency regressions
and stress errors (which indicate locking/concurrency bugs)
block the pipeline equally.

Disabled by default locally (`0` = off). CI sets non-zero
defaults via the `gitlab-ci.yml` job definition.

## Usage

### Local (manual)

```bash
# Run with SLO enforcement
LAT_SLO_P99_MS=1500 STR_MAX_ERROR_PCT=1 make benchmark

# Latency only, save results, gate at 2s
LAT_SLO_P99_MS=2000 BENCH_SAVE=1 make bench-latency

# Stress only, fail on any errors
STR_MAX_ERROR_PCT=0.1 make bench-stress
```

### Script-level flags

```bash
# Direct script invocation
python benchmarks/search_latency.py \
    --slo-p99-ms 1500 --queries 200 --concurrency 8

python benchmarks/stress.py \
    --max-error-pct 1 --duration 30 --concurrency 4
```

Exit code 1 on violation, 0 otherwise. When no threshold
is set (or set to 0), scripts always exit 0 (current
behavior preserved).

## CI Job

The `bench` job runs in a `benchmark` stage between `test`
and `security`:

- **Trigger**: MR pipelines, default branch, tags.
- **Server**: ephemeral (fresh data_dir per run).
- **Lighter defaults**: 200 queries (not 1200), 30s stress
  (not 90s), lower concurrency — CI runners are shared.
- **Artifacts**: `benchmarks/results/` saved for 4 weeks.
- **Tuning**: all variables overridable via GitLab CI/CD
  Settings → Variables without code changes.

## Violation Output

```
SLO VIOLATION: p99=2134.5ms > 2000.0ms
```

```
ERROR RATE VIOLATION: 3.2% > 1.0%
```

Printed after the normal results table. The full benchmark
output is always produced regardless of pass/fail.

## Failure Interpretation

These gates are meant to surface real runtime faults, not
just slow runners. Treat repeated stress failures such as:

- `no such table: chunks`
- `Directory not empty` during collection delete

as store lifecycle bugs first. The benchmark mixes create,
delete, ingest, search, and archive operations on purpose,
so these signatures usually point to TOCTOU issues in the
SQLite/file-store layer rather than mere CI noise.

## Not in Scope

- Recall/relevance SLO (needs P2-29 fixtures first).
- Stress throughput SLO (too runner-dependent).
- Historical trend tracking / dashboard (future).
- Benchmark results posted to MR comments (future).
