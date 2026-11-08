<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Benchmark CI Gate (P2-30)

Automated benchmark runs in CI with configurable SLO
thresholds. Pipelines fail when latency or error rate
exceeds limits.

## SLO Thresholds

| Variable | Script flag | Scope | Default (CI) | Gate |
|----------|-------------|-------|--------------|------|
| `LAT_SLO_P99_MS` | `--slo-p99-ms` | search p99 latency | 4000 ms | hard |
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

# Tenant rate-limit charge included (always starts an ephemeral server)
LAT_SLO_P99_MS=2000 BENCH_SAVE=1 make bench-latency-rate-limited

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
is set (or set to 0), SLO checks are disabled. The authenticated
variant still fails on setup, query, or rate-limit coverage errors.

## CI Job

The `bench` job runs in a `benchmark` stage between `test`
and `security`:

- **Trigger**: MR pipelines, default branch, tags.
- **Runner**: pinned to the `m4-colima` tag — the runner
  `pytest-arm64` already uses and leaves idle between tags.
  `resource_group: bench` serializes this project's own
  benchmark jobs (two open merge requests, or a tag and a
  merge request, never time that runner side by side); it
  does not hold back whatever else that host runs.
- **Threshold scope**: the p99 ceiling is a property of the
  host that times the run, so it stays sized for the slowest
  host that could still pick the job up if the pin above
  ever comes off — ~2.3 s measured on the shared group
  runner, against ~280 ms on GitLab SaaS. That keeps a red
  gate a statement about the server rather than the host; the
  saved report's `host load` line backs that up from the
  artifact itself — Docker does not namespace
  `/proc/loadavg`, so in CI that figure includes jobs from
  other pipelines and other projects on the same host, which
  `resource_group: bench` does not hold back. Tune the
  threshold by variable (see Tuning below) rather than moving
  the job to a faster box.
- **Server**: ephemeral (fresh data_dir per run).
- **Rate-limit coverage**: after the existing admin-bypass latency and stress
  runs, `bench-latency-rate-limited` repeats the latency variants with static
  auth and a non-admin `bench` tenant. Its per-minute cap is `LAT_LENGTH + 100`,
  leaving headroom for setup and cleanup; concurrent and hourly caps are off.
  The same p99 ceiling includes each request's persistent catalog charge.
  Every query must succeed with moving-window rate-limit headers, otherwise
  the run fails even if its successful samples meet the latency ceiling.
  Reports carry a `-rate-limited` suffix for comparison with the bypass run.
- **Lighter defaults**: 200 queries (not 1200), 30s stress
  (not 90s), lower concurrency — the gate has to fit a CI
  job budget, not a lab run.
- **Artifacts**: `benchmarks/results/` saved for 4 weeks.
  A run that fails with the ephemeral server also saves
  that server's log as `server-pavedb-bench.XXXXXX.log`:
  the `$TMPDIR` path the recipe prints on the way out dies
  with the container, so this is the only copy a reader of
  the artifact can open.
  Nothing is downloaded: `dependencies: []` drops the
  test stage's junit.xml and `.pytest_cache`, and, where
  the announce jobs run, the release title and notes those
  jobs fetch for themselves. The benchmark reads none of
  it, and GitLab fetches all of it before the job starts
  whenever the key is left out. The job still runs after
  the test stage — that ordering is stage order, not
  `needs`, so a red suite keeps the gate from starting.
- **Tuning**: all variables overridable via GitLab CI/CD
  Settings → Variables without code changes.

## Violation Output

```
SLO VIOLATION: p99=5218.4ms > 4000.0ms
    over SLO  : 4 of 200 samples (2.0%)
    slowest   : 5227.5ms, 5222.0ms, 5218.4ms, 5218.3ms, 900.0ms
    mean/p95  : 225.2ms / 295.1ms
    host load : 1m 11.42 (0.71/core over 16), 5m 9.80, 15m 6.31
```

```
ERROR RATE VIOLATION: 3.2% > 1.0%
```

Printed after the normal results table. The full benchmark
output is always produced regardless of pass/fail.

The breakdown under a latency violation is there because
`p99` alone does not identify the fault. Over 200 samples
`percentile()` interpolates between the third- and
second-worst at a hundredth of the gap, so one slow request
barely moves it — a red p99 means at least the slowest
concurrency wave stalled together. `over SLO` and
`mean/p95` then separate a wave the host starved (a handful
of samples over the line, mean near p50, the rest of the
run untouched) from a server that got slower across the
board (most of the run over the line, mean near p95), and
`host load` says which of the two the box can account for.
The block above is illustrative, not a transcript, and it
is sized against the 4000 ms ceiling this job sets: a doc
read when the gate goes red cannot quote a threshold the
job stopped using. Its shape comes from job 17708580396,
which exited on `SLO VIOLATION: p99=3088.3ms` against the
2000 ms ceiling in force then, with p95 295.1 ms, max
3097.5 ms and 0 errors in 200 queries in its table; the
breakdown and `host load` lines postdate that run. A p99
that close to the max is what forced three samples within
9.2 ms of each other at the top of it, and the block keeps
that shape over the current ceiling — the fourth sample
over the line, the 900 ms one under it and the mean are
illustrative, and read as one starved wave rather than a
slower server: a handful over the line, the next one down
at 900 ms, a mean below p95.

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
