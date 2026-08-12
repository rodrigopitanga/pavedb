<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# PLAN-METRICS-PERSISTENCE — Worker-safe runtime metrics

## Objective

Persist runtime counters from every server worker without writing SQLite on
the request path or replacing one serialized metrics blob.

## Storage model

`catalog.db` owns three normalized tables:

```sql
CREATE TABLE metrics_state (
    singleton     INTEGER PRIMARY KEY CHECK (singleton = 1),
    generation    TEXT NOT NULL,
    last_error    TEXT,
    last_error_at REAL
);

CREATE TABLE metrics_counters (
    name        TEXT NOT NULL,
    labels_json TEXT NOT NULL,
    value       REAL NOT NULL,
    PRIMARY KEY (name, labels_json)
);

CREATE TABLE metrics_latency_samples (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    operation TEXT NOT NULL,
    value     REAL NOT NULL
);
```

`labels_json` is canonical JSON for one small label set, not a serialized
metrics snapshot. Counter updates use SQLite's native additive upsert:

```sql
ON CONFLICT(name, labels_json) DO UPDATE
SET value = value + excluded.value
```

Latency samples remain bounded to the newest 1,000 rows per operation.
Gauges derived from catalog contents stay live and are not persisted.

## Runtime flow

1. Each worker accumulates counter deltas and latency samples in memory.
2. Startup reads the current generation token.
3. The background task swaps out the pending buffer every 10.5 seconds.
4. One `BEGIN IMMEDIATE` transaction verifies the generation, upserts all
   counter deltas, inserts latency samples, prunes old samples, and records the
   newest error.
5. Scrapes read normalized rows and add only the current worker's unflushed
   buffer.

SQLite serializes concurrent worker flushes. Request handling only takes the
existing in-process metrics lock; it never waits for SQLite.

## Reset and restore

Reset replaces the opaque generation token and clears counters, samples, and
the last error in one transaction. A worker flushing an older generation is
rejected and discards that stale buffer, so pre-reset deltas cannot reappear.

Archive restore rotates the restored generation without clearing its stored
metrics. Pending deltas from the replaced runtime therefore cannot enter the
restored catalog in the restoring process. Whole-instance archive replacement
still requires other server workers to be stopped; generation rotation does
not make filesystem replacement safe across processes.

The catalog schema is pre-1.0: this change bumps its version and provides no
migration or compatibility reader. Older data directories must be recreated.

## Accepted ceiling

A worker killed before its next background flush can lose at most one flush
interval of local deltas. Eliminating that window would require request-path
durable writes or an external metrics collector and is out of scope.

Persisted labeled-counter rows follow the number of distinct label sets. This
layer does not silently evict cumulative counters; metric producers remain
responsible for cardinality.

## Verification

- Two independent catalog connections add to the same counter without a lost
  update.
- A stale-generation flush after reset is rejected.
- Restore rotates the generation token.
- Counter rows stay normalized, latency retention stays bounded, and both are
  readable by the metrics endpoints.
- The focused metrics, catalog, and close-race tests pass.
