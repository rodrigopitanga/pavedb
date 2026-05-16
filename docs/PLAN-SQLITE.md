<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# PLAN-SQLITE — Internal SQLite Store Roadmap

PaveDB replaces ad-hoc JSON/filesystem metadata with a layered SQLite store.
Each phase adds a new layer; earlier phases are prerequisites for later ones.

Runtime store is `LocalStore` (`pave/stores/local.py`) with
`FaissBackend` + `CollectionDB` per collection. The PLAN-STORE
migration (P1-29b/P1-29c/P1-31) is complete; txtai is no longer
a runtime dependency.

Roadmap ownership:
- Phase 1 → `P1-09`
- Phase 2 → `P1-33` (PLAN-STORE Step 5 dependency, for `P1-32`)
- Phase 2 builds on the current `LocalStore` + `CollectionDB`
  architecture.

---

## Phase 1 — Per-Collection Metadata Store (catalog + meta.json replacement)

**Target: v0.5.8**

### Problem

`TxtaiStore` (as of 0.5.6) maintains two JSON files per collection:

- `catalog.json` — `{docid: [rid, ...]}` — which chunk IDs belong to a document
- `meta.json` — `{rid: {k: v, ...}}` — metadata dict per chunk

Both are protected by a `threading.Lock` per collection. The critical path issue
is in `search()`:

```python
with collection_lock(tenant, collection):   # acquired
    raw = em.search(sql)                    # FAISS (under the hood) vector search — can be 100ms+
    ...
    meta = self._load_meta(tenant, collection)  # full meta.json load — INSIDE lock
```

Consequences:
1. Concurrent searches on the same collection are fully serialized
2. `_load_meta` deserializes the entire collection's metadata on every search,
   O(N chunks)
3. No ACID on ingest/purge — two separate `os.replace()` calls, no transaction

### Schema

We want collections to be binary-portable (at least within version-compatible pave/faiss
installs), so one `meta.db` per collection at `{data_dir}/t_{tenant}/c_{collection}/meta.db`.

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    docid       TEXT PRIMARY KEY,
    version     INTEGER NOT NULL DEFAULT 1,
    ingested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    meta_json   TEXT    -- doc-level metadata: filename, content_type, custom fields
);

CREATE TABLE IF NOT EXISTS chunks (
    docid       TEXT NOT NULL,
    rid         TEXT PRIMARY KEY,
    chunk_path  TEXT,                        -- path to sidecar .txt file on disk
    meta_json   TEXT NOT NULL DEFAULT '{}',  -- per-chunk only: page, position, etc.
    ingested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS chunks_docid ON chunks (docid);
```

`chunks` replaces both JSON files:
- catalog role: `SELECT rid FROM chunks WHERE docid = ?`
- metadata role: `SELECT rid, meta_json FROM chunks WHERE rid IN (...)`

`documents` tracks re-ingest history and document-level state:
- `version` increments on each purge+reinsert:
  `COALESCE((SELECT version FROM documents WHERE docid=?), 0) + 1`
- `ingested_at` reflects the most recent ingest timestamp
- Not a full version history (that's Phase 4) — just a monotonic counter for
  visibility and debugging
- `meta_json` — document-level metadata (filename, content_type, and any custom
  fields passed at ingest). Chunks carry only genuinely per-chunk fields (page,
  position, section). **Migration note:** current `chunks.meta_json` holds a mix
  of doc-level and chunk-level fields as a workaround (no document row existed
  before); those doc-level fields migrate here when `TxtaiStore` is updated.
- Original file retention is a P3-30 concern (opt-in); field naming TBD there.
  Any derivative document produced mid-pipeline either becomes chunks or is
  referenced via metadata — no separate file row needed.

**`docid` assignment rules** (consistent across file and non-file sources):
- Explicit caller-provided `docid` → used as-is
- File source, no docid → derived from filename (current behaviour)
- Non-file source (string, stream), no docid → UUID generated at ingest time

**Content hash** — deferred. Same content can represent two distinct entities so
a content hash must never be used as an identity. It is still valuable as a
fingerprint for smart re-ingest skipping (avoid re-chunking and re-embedding
unchanged content when the model has not changed), but that requires keeping
the original document on disk. Tracked under P3-30 (retain original uploaded
files, opt-in).

### Chunk vs document metadata split

`chunks.meta_json` holds only per-chunk fields (page, position, section).
Doc-level fields (filename, content_type, ingest-time custom fields) live in
`documents.meta_json` — not replicated on every chunk row.

Trade-off: hydrating a chunk with doc-level metadata requires a JOIN
(`chunks JOIN documents ON chunks.docid = documents.docid`). In practice this
is rarely needed on the hot path. In Phase 1, txtai returns indexed metadata and
`get_meta_batch` (per-chunk WAL read, no JOIN) covers the common search case.
After PLAN-STORE P1-29c, CollectionDB becomes the first local pushdown
implementation. Filter pushdown remains capability-based by backend, and
this split still holds.

### Why JSON blob and not K/V rows

In Phase 1, pre-filters go into txtai internal SQL via `em.search()`. Our store
is not queried for pre-filtering yet. Only post-filter matching (wildcards,
comparisons) and result hydration use our metadata. This changes in PLAN-STORE
P1-29c, where CollectionDB provides the first local pushdown path; other
backends may implement pushdown differently.

### Migration system

Integer-versioned DDL applied on first `open()`. Version state in
`schema_migrations`. Clean start only: no legacy JSON import (acceptable for current
user base).

**Legacy JSON detection:** if `catalog.json` or `meta.json` exist in the collection
directory, raise `LegacyMetadataError` with a clear message. Prevents silent data
loss on upgrade.

### New module: `pave/metadb.py`

```python
class CollectionDB:
    def open(self, path: Path) -> None
        # Opens/creates meta.db. Detects legacy JSON. Applies migrations.
        # Pragmas: WAL, busy_timeout=5000, synchronous=NORMAL.

    def upsert_chunks(
        self,
        docid: str,
        chunks: list[tuple[str, str | None, dict]],  # (rid, chunk_path, per-chunk meta)
        doc_meta: dict | None = None,                # doc-level meta
                                                     # → documents.meta_json
    ) -> None
        # INSERT OR REPLACE chunks via executemany.
        # Upserts documents row: bumps version, refreshes ingested_at, stores doc_meta.
        # All in one transaction. Inside collection_lock.

    def delete_doc(self, docid: str) -> list[str]
        # SELECT rid WHERE docid=? then DELETE chunks WHERE docid=?.
        # Deletes documents row too (purge is a full removal).
        # No RETURNING (requires SQLite ≥3.35). Inside collection_lock.

    def has_doc(self, docid: str) -> bool
    def get_rids_for_doc(self, docid: str) -> list[str]
    def get_doc_version(self, docid: str) -> int | None
        # Returns current version or None if docid not found.

    def get_meta_batch(self, rids: list[str]) -> dict[str, dict]
        # Short-circuits on empty list.
        # Chunks IN list at 999 to respect SQLite variable limit.
        # Called OUTSIDE collection_lock.

    def close(self) -> None
```

Connection: one persistent connection per instance, `check_same_thread=False`,
WAL mode, `busy_timeout=5000ms`.

### Key change in `TxtaiStore.search()`

```python
# BEFORE: meta load inside lock — serializes all concurrent searches
with collection_lock(tenant, collection):
    raw = em.search(sql)
    meta = self._load_meta(...)   # O(N), INSIDE lock

# AFTER: FAISS inside lock, meta read outside
with collection_lock(tenant, collection):
    raw = em.search(sql)          # lock covers FAISS only

meta_batch = col_db.get_meta_batch(candidate_rids)   # WAL read, concurrent
```

### Concurrency model

| Operation | Lock held | Meta I/O |
|-----------|-----------|----------|
| `index_records` | `collection_lock` | SQLite write inside lock |
| `purge_doc` | `collection_lock` | SQLite write inside lock |
| `has_doc` | none | SQLite WAL read |
| `search` — FAISS | `collection_lock` | FAISS only |
| `search` — meta | none | SQLite WAL read, concurrent |

### Files changed

| File | Change |
|------|--------|
| `pave/metadb.py` | New — `CollectionDB` |
| `pave/stores/txtai_store.py` | Replace JSON I/O with `CollectionDB` |
| `tests/test_meta_store.py` | New — unit tests |

### What this does not change

- `service.py`, `main.py`, `BaseStore` — no signature changes
- `DummyStore` / `SpyStore` — unchanged
- Chunk text sidecars (`chunks/*.txt`) — unchanged
- Filter architecture (Phase 1) — pre-filters still go to txtai SQL
  (first local pushdown in CollectionDB at P1-29c; capability-based overall)
- `list_tenants` / `list_collections` — still filesystem walk

### Performance expectations

- **p50**: modest improvement (JSON parse eliminated from search hot path)
- **p95/p99**: significant improvement (concurrent searches no longer serialize
  on meta load)

### Benchmark protocol (required)

- Run `benchmarks/search_latency.py` and `benchmarks/stress.py` before and after
  each phase. Save the raw outputs in `benchmarks/results/` with clear names:
  `phase-1-before-<date>.txt`, `phase-1-after-<date>.txt`, etc.
- Keep the same parameters for the before/after pair.
- Tune parameters until p95/p99 visibly separate from p50 (avoid masked results).
  Increase `--concurrency` and `--queries`/`--duration` as needed.

Example (adjust as needed for your machine):

```bash
python benchmarks/search_latency.py --queries 400 --concurrency 32 \
  | tee benchmarks/results/phase-1-before-2026-02-25.txt
python benchmarks/stress.py --duration 180 --concurrency 24 \
  | tee benchmarks/results/phase-1-before-2026-02-25.stress.txt
```

### Phase 1 benchmark results

Three `CollectionDB` implementations were evaluated (branches
`sql-phase1-impl0/1/2`, all carrying the same `service.py` and
`benchmarks/stress.py` bug-fixes as main). Tests: 126/126 pass on all three.

**Implementation strategies**

| Impl | `CollectionDB` connection model |
|------|---------------------------------|
| impl0 | Single persistent `_conn`, `_write_lock` guards writes; reads are lockless |
| impl1 | `_tls` threading.local read connections + dedicated `_wconn` for writes |
| impl2 | Two connections `_rconn` + `_wconn`, `_write_lock` guards writes |

**Latency benchmark** — 1200 queries, concurrency=42

| | Min | p50 | p95 | p99 | Max | Throughput |
|--|-----|-----|-----|-----|-----|------------|
| before\_sql (baseline) | 92ms | 1255ms | 1287ms | 1295ms | 1301ms | 33.7 ops/s |
| impl0 | 91ms | 1032ms | **1062ms** | 1202ms | 1217ms | 38.9 ops/s |
| impl1 | 106ms | 1056ms | 1246ms | 1304ms | 1445ms | 37.3 ops/s |
| impl2 ✓ | 110ms | 1046ms | **1086ms** | 1177ms | 1185ms | 38.4 ops/s |

**Stress benchmark** — 90s duration, concurrency=8

| | Total ops | Throughput | Total err% | Search err% | Notable errors |
|--|-----------|------------|------------|-------------|----------------|
| before\_sql | 100 | 1.0 ops/s | 16.0% | 25.4% | search timeouts (30s) |
| impl0 | 905 | 7.7 ops/s | 1.3% | 1.3% | "Cannot operate on a closed database" |
| impl1 | 1091 | 12.0 ops/s | **0.1%** | **0.0%** | 1× "unable to open database file" |
| impl2 ✓ | 1016 | 11.0 ops/s | **0.2%** | **0.0%** | 2× "unable to open database file" |

**Result files** (canonical runs after all bug-fixes):

```
# Baseline (before Phase 1)
benchmarks/results/latency-2026-03-02_032017_before_sql-4dc4b5b.txt
benchmarks/results/stress-2026-03-02_032017_before_sql-4dc4b5b.txt

# impl0 — branch sql-phase1-claude-impl0 @ d4b95d5
benchmarks/results/latency-2026-03-03_212122_after_sql-claude-impl0-d4b95d5.txt
benchmarks/results/stress-2026-03-03_212122_after_sql-claude-impl0-d4b95d5.txt

# impl1 — branch sql-phase1-claude-impl1 @ e531c77
benchmarks/results/latency-2026-03-03_212821_after_sql-claude-impl1-e531c77.txt
benchmarks/results/stress-2026-03-03_212821_after_sql-claude-impl1-e531c77.txt

# impl2 — branch sql-phase1-claude-impl2 @ e3aed3c  (merged to main)
benchmarks/results/latency-2026-03-03_213834_after_sql-claude-impl2-e3aed3c.txt
benchmarks/results/stress-2026-03-03_213834_after_sql-claude-impl2-e3aed3c.txt
```

**Winner: impl2** (read/write split) — merged to main (`9af1090`).

Rationale:
- **impl0** has the best p95 (1062ms, -17.5%) and p50 (1032ms) but its single
  shared connection produces real search errors under concurrent archive-restore
  load ("Cannot operate on a closed database"). `get_meta_batch` runs outside
  `collection_lock`, so write/restore operations can race against in-flight reads
  on the same connection object. Disqualified on correctness grounds.
- **impl1** (thread-local reads) eliminates search errors but the p95 improvement
  over baseline is only 3.2% — within run-to-run FAISS variance. The per-thread
  connection pool adds bookkeeping overhead that partially cancels the concurrency
  gain. p99 is actually worse than baseline (1304ms vs 1295ms).
- **impl2** (read/write split) delivers 15.7% p95 improvement (1287ms→1086ms),
  zero search errors, 11 ops/s stress throughput (vs 1.0 ops/s baseline), and
  its two "unable to open database file" errors are transient filesystem races
  during collection delete racing concurrent ingest — not connection-model bugs.

### Post-merge hardening (2026-03-04)

After `impl2` landed, a close/read race was hardened:

- `CollectionDB.close()` now waits for in-flight readers before closing.
- `TxtaiStore` now evicts cache entries before close during delete/rename.
- `has_doc`/search metadata reads now recover from transient closed/closing DB handles.

One sanitized benchmark pass was run with a fresh `data_dir` and fresh server process
(`v0.5.8a3`) using the same parameters (latency: 1200/42, stress: 90s/8):

| | Throughput | p50 | p95 | p99 | Total err% | Search err% |
|--|------------|-----|-----|-----|------------|-------------|
| impl2 canonical (`e3aed3c`) | 38.4 ops/s (lat), 11.0 ops/s (stress) | 1046ms | 1086ms | 1177ms | 0.2% | 0.0% |
| hardened sanitized pass (`91328b4`) | 40.0 ops/s (lat), 15.6 ops/s (stress) | 1012ms | 1083ms | 1190ms | 0.0% | 0.0% |

Notes:
- p99 movement of this magnitude is within expected single-run variance.
- The prior noisy run with "Cannot operate on a closed database" was not deterministic
  load variance; it was the close/read race above.

---

## Phase 2 — CatalogDB + Catalog Separation

**Target: v0.9**
**Roadmap item: `P1-33`**

### Problem

`LocalStore` listing and catalog methods walk the filesystem:
- `list_collections()` (`local.py:236`): scans `t_<tenant>/`
  for `c_*/meta.db` entries.
- `list_tenants()` (`local.py:252`): scans `data_dir` for
  `t_*` directories — including empty ones.
- `catalog_metrics()` (`local.py:268`): walks every tenant and
  collection directory, opening each `meta.db` for counts.

This works but does not scale, mixes control-plane catalog with
data-plane collection state, and cannot support per-collection
config (backend type, embedder model) without a central store.

### Contract

`CatalogDB` becomes the **only source of truth** for:
- Tenant and collection listing.
- Collection-level configuration (backend, embedder).

No permanent filesystem-walk fallback for listing.

### Semantic decisions

These are decided up front, not deferred to implementation:

1. **`_system/*` collections are not cataloged.** Startup and
   readiness probes create `_system/health` via
   `create_collection()` (`main.py:75`, `health.py:41`).
   `create_collection` skips the CatalogDB insert when
   `tenant == "_system"`. Listing and metrics never see
   `_system/*` collections.

2. **Tenant semantics change (breaking).** A tenant exists only
   if it has at least one cataloged collection. Empty `t_*`
   directories on disk are not tenants. This changes the current
   behavior of `list_tenants()` which counts any `t_*` dir.
   Tests that create empty tenant dirs and expect them in
   listings (`test_admin_tenants.py`,
   `test_store_catalog_metrics.py`) must be updated.

### Schema

One `catalog.db` at `{data_dir}/catalog.db`.

```sql
CREATE TABLE IF NOT EXISTS collections (
    tenant              TEXT NOT NULL,
    name                TEXT NOT NULL,
    display_name        TEXT,
    meta_json           TEXT,
    backend_type        TEXT,
    backend_config_json TEXT,
    embedder_type       TEXT,
    embed_model         TEXT,
    embed_config_json   TEXT,
    created_at          TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (tenant, name)
);
```

Column notes:
- `name` is the slug (URL-safe, used in paths/URLs/keys).
  `display_name` is the human label; renaming it does not
  affect paths.
- `backend_type`, `backend_config_json`: backend identity and
  constructor config. Populated with instance defaults on
  creation; per-collection override is P1-32.
- `embedder_type`, `embed_model`, `embed_config_json`: embedder
  identity and config. Same population strategy.
- `meta_json`: operator metadata (description, tags, owner).
- `created_at`: automatic, zero-maintenance timestamp.

**`name` (slug) assignment rules:**
- Explicit API-provided slug → used as-is.
- Display name provided → slug auto-derived: lowercase,
  spaces → underscores, strip non-alphanum/dash/underscore.
- Neither provided → UUID.

Doc and chunk counts are derived on demand from each
collection's Phase 1 `meta.db`
(`SELECT COUNT(DISTINCT docid), COUNT(*) FROM chunks`) — no
sync burden.

### `CatalogDB` class (`pave/metadb.py`)

```python
class CatalogDB:
    def open(self, path: Path) -> None
    def close(self) -> None
    def bootstrap(self, data_dir: Path) -> None
        # Reconcile catalog.db against on-disk collections.
        # Seed missing rows; remove orphaned rows.
        # Skip _system/*.
    def register_collection(
        self, tenant: str, name: str,
        display_name: str | None = None,
        meta_json: str | None = None,
        backend_type: str | None = None,
        backend_config_json: str | None = None,
        embedder_type: str | None = None,
        embed_model: str | None = None,
        embed_config_json: str | None = None,
    ) -> None
    def unregister_collection(
        self, tenant: str, name: str,
    ) -> None
    def rename_collection(
        self, tenant: str, old: str, new: str,
    ) -> None
    def list_tenants(self) -> list[str]
        # SELECT DISTINCT tenant FROM collections
    def list_collections(self, tenant: str) -> list[str]
        # Returns collection names for tenant.
    def get_collection_config(
        self, tenant: str, name: str,
    ) -> dict[str, Any] | None
        # Returns persisted backend/embedder config dict.
        # None if collection not found.
    def collection_count(self) -> int
    def tenant_count(self) -> int
```

One `threading.Lock` inside CatalogDB for catalog writes.
WAL mode for concurrent reads.

### Bootstrap and repair

On `LocalStore` startup (before serving requests):

1. Open `catalog.db` (create if missing).
2. Scan `data_dir` for `t_*/c_*/meta.db` paths.
3. For each discovered collection where `tenant != "_system"`:
   `INSERT OR IGNORE` into `collections` with defaults.
4. For each catalog row whose `t_<tenant>/c_<name>/meta.db`
   no longer exists on disk: `DELETE` from `collections`.
5. Log a summary: seeded N, removed M orphans.

This makes existing installs with collections but no
`catalog.db` upgrade seamlessly on first boot.

### LocalStore integration

All integration is against `LocalStore`
(`pave/stores/local.py`). TxtaiStore is gone (P1-31 done).

**Lifecycle methods:**
- `create_collection()`: after `_load_or_init()` + `_save()`,
  insert into CatalogDB. Skip insert if `tenant == "_system"`.
- `delete_collection()`: after filesystem removal, delete from
  CatalogDB.
- `rename_collection()`: after `os.rename()`, update CatalogDB.

**Query methods (replace filesystem walk):**
- `list_collections()`: `CatalogDB.list_collections(tenant)`.
- `list_tenants()`: `CatalogDB.list_tenants()`.
- `catalog_metrics()`: tenant/collection counts from CatalogDB;
  doc/chunk counts still from per-collection `meta.db`
  (opened read-only, same as today).

**Locking:** per-collection lock model unchanged. CatalogDB has
its own write lock — never held while holding a collection
lock (no nested lock risk).

### Failure model

Filesystem is the **durable truth** for collection payloads.
`catalog.db` is the control-plane index.

For create/delete/rename:
1. Perform the filesystem change (already atomic for rename).
2. Update CatalogDB.
3. On CatalogDB failure after successful filesystem op: fail
   loudly (log error, propagate exception). Do **not** attempt
   filesystem rollback — startup reconciliation will heal the
   catalog on next boot.

### P1-32 preparation

`get_collection_config()` exists and reads persisted
backend/embedder settings from `catalog.db`. `LocalStore`
still uses instance defaults initially (all collections share
the same `FaissBackend` + embedder), but the schema and read
path are in place so P1-32 (per-collection embeddings) does
not require another catalog migration.

### Test plan

| Category | What to test |
|---|---|
| Unit: CatalogDB | Migrations, CRUD, `bootstrap()` |
| Integration | create/delete/rename/listing via `LocalStore` |
| Upgrade | Existing on-disk collections with no `catalog.db` discovered on first boot |
| Metrics | Counts from catalog + `meta.db`, not dir walk |
| `_system` exclusion | Startup creates `_system/health`; listings and metrics never show it |
| Tenant semantics | Empty tenant dir → not listed |

---

## Phase 3 — Operational State (auth, tenant profiles, rate limits, metrics,
log retention)

**Target: v0.7 / v0.8**

### Auth progression

`tenants.yml` is the source of truth for API keys until Phase 3 ships:

```
tenants.yml (now)  →  SQL key store (Phase 3)  →  + OIDC/JWT opt-in (P3-17, v0.8)
```

API keys (`api_keys` table) are a permanent first-class auth method — simple
deployments never need anything else. OIDC/JWT is additive: if
`auth.oidc.issuer` is configured, PaveDB accepts either a valid API key or a
valid JWT on any request. JWT validation is stateless (signature check against
IdP public key); no new table needed for it. See ROADMAP P3-17.

**YAML seed:** on first boot after Phase 3 migration, read `cfg.get('tenants')`
and for each tenant entry INSERT OR IGNORE into `tenant_profiles`, populating
`max_concurrent` from `tenants.<name>.max_concurrent` and the global default
from `tenants.default_max_concurrent`. The `rate_limit_buckets` table (below)
is the target for moving-window rate limiting seeded from `tenants.max_rpm`.

**Seed logic:** on first boot after Phase 3 migration, for each tenant in
`tenants.yml`:
- If tenant not in `api_keys` → insert with key hashed (SHA256 or bcrypt)
- If already present → skip (SQL wins; YAML is not re-applied)

After seeding, `tenants.yml` is only read for limits/profile fallback until
those migrate to `tenant_profiles`. Per-tenant limits defined in `tenants.yml`
today (`max_req_per_min`, `max_collections`, etc.) seed `tenant_profiles` by
the same logic.

**`api_keys` table:**

```sql
CREATE TABLE IF NOT EXISTS api_keys (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant     TEXT NOT NULL,
    key_hash   TEXT NOT NULL UNIQUE,
    label      TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    revoked    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS api_keys_tenant ON api_keys (tenant);
```

One tenant → many keys. Rotation: issue new key, revoke old row. No restart
needed. The global bootstrap key (`PAVEDB_GLOBAL_KEY` env var) stays outside
SQL permanently — it is the credential used to access the system before any
tenant is provisioned.

**Key management API** (new endpoints, v0.8):
- `POST /admin/tenants` — provision tenant (slug, display_name, limits)
- `POST /admin/tenants/{tenant}/keys` — generate key, returns plaintext once
- `DELETE /admin/tenants/{tenant}/keys/{id}` — revoke key

### What gets persisted

**Tenant profiles** — resource limits and tier config. Seeds from `tenants.yml`
on first boot; SQL is source of truth thereafter. PaveDB enforces limits;
billing/onboarding are out of scope.

```sql
CREATE TABLE IF NOT EXISTS tenant_profiles (
    tenant          TEXT PRIMARY KEY,
    display_name    TEXT,
    max_collections INTEGER,
    max_storage_mb  INTEGER,
    max_concurrent  INTEGER,   -- per-tenant concurrent request cap (0 = unlimited)
    max_req_per_min INTEGER,
    tier            TEXT,
    meta_json       TEXT   -- operator metadata: description, tags, cost-center, etc.
);
```

**Rate limit state** — per-tenant, per-operation counters with TTL.

```sql
CREATE TABLE IF NOT EXISTS rate_limit_buckets (
    tenant      TEXT NOT NULL,
    operation   TEXT NOT NULL,   -- 'search', 'ingest', 'global'
    window_start TEXT NOT NULL,  -- ISO8601 truncated to window
    count        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant, operation, window_start)
);
```

**Aggregate metrics** — doc/chunk counts by collection for `/metrics` and admin
endpoints. (Prometheus counters for latency/search totals stay in-process.)

```sql
-- Doc/chunk counts derived from per-collection meta.db; no new table needed.
-- Structured search/ingest event log for per-tenant analytics:
CREATE TABLE IF NOT EXISTS operation_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant      TEXT NOT NULL,
    collection  TEXT,
    operation   TEXT NOT NULL,   -- 'search', 'ingest', 'delete'
    request_id  TEXT,
    latency_ms  REAL,
    status      TEXT,            -- 'ok', 'error'
    ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS op_log_tenant ON operation_log (tenant, ts);
```

Rolling retention enforced per tenant/collection (configurable window, default 30d).
Powers P2-13 collection log export.

**Per-collection backend + embedder config** — stores how each collection is
wired (backend and embedder) for P1-32 and later backend swaps.

This is collection-scoped logical wiring, not a requirement that each
collection owns a dedicated physical database or vector service. The
stored backend/embedder config may point either to dedicated local
artifacts or to shared infrastructure with collection-specific keys.

```sql
ALTER TABLE collections ADD COLUMN backend_type TEXT;
ALTER TABLE collections ADD COLUMN backend_config_json TEXT;
ALTER TABLE collections ADD COLUMN embedder_type TEXT;
ALTER TABLE collections ADD COLUMN embed_model TEXT;
ALTER TABLE collections ADD COLUMN embed_config_json TEXT;
```

---

## Phase 4 — Governance (syndicates, audit, versioning, usage, jobs)

**Target: v0.8 / v1.0**

### Collection versioning

Every collection records the PaveDB version and schema version it was written
with. Incompatible reads fail loudly with actionable guidance.

```sql
ALTER TABLE collections ADD COLUMN patchvec_version TEXT;
ALTER TABLE collections ADD COLUMN schema_version    INTEGER;
ALTER TABLE collections ADD COLUMN created_at        TEXT;
```

`created_at` not tracked today; added here when we start recording it.

### Document versioning

```sql
CREATE TABLE IF NOT EXISTS document_versions (
    tenant      TEXT NOT NULL,
    collection  TEXT NOT NULL,
    docid       TEXT NOT NULL,
    version     INTEGER NOT NULL,
    ingested_at TEXT NOT NULL,
    chunk_count INTEGER NOT NULL,
    PRIMARY KEY (tenant, collection, docid, version)
);
```

Powers P2-14 audit trails and future rollback tooling.

### Audit log

Admin-action audit trail (collection create/delete/rename, tenant changes).

```sql
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    actor      TEXT,            -- tenant or 'admin'
    action     TEXT NOT NULL,   -- 'create_collection', 'delete_doc', etc.
    tenant     TEXT,
    collection TEXT,
    detail_json TEXT
);
```

### Usage stats

Opt-in, anonymized telemetry for capacity planning (P2-22).

```sql
CREATE TABLE IF NOT EXISTS usage_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    tenant_count   INTEGER,
    collection_count INTEGER,
    doc_count      INTEGER,
    chunk_count    INTEGER,
    reported       INTEGER NOT NULL DEFAULT 0  -- 0=pending, 1=sent
);
```

### Syndicates (opt-in tenant groupings)

Lightweight overlay for org-level quotas and shared collections. No mandatory
hierarchy; a tenant exists without a syndicate.

```sql
CREATE TABLE IF NOT EXISTS syndicates (
    id    TEXT PRIMARY KEY,   -- syndicate slug
    name  TEXT,
    config_json TEXT
);

CREATE TABLE IF NOT EXISTS syndicate_members (
    syndicate_id TEXT NOT NULL REFERENCES syndicates(id),
    tenant       TEXT NOT NULL,
    role         TEXT,        -- 'admin', 'member'
    PRIMARY KEY (syndicate_id, tenant)
);
```

### Async ingest job status (P3-31)

```sql
CREATE TABLE IF NOT EXISTS ingest_jobs (
    job_id      TEXT PRIMARY KEY,
    tenant      TEXT NOT NULL,
    collection  TEXT NOT NULL,
    status      TEXT NOT NULL,   -- 'queued', 'running', 'done', 'failed'
    submitted_at TEXT NOT NULL,
    finished_at  TEXT,
    chunk_count  INTEGER,
    error        TEXT
);
```

### Collection migration records (P3-37)

```sql
CREATE TABLE IF NOT EXISTS collection_migrations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant          TEXT NOT NULL,
    collection      TEXT NOT NULL,
    from_version    INTEGER,
    to_version      INTEGER,
    migrated_at     TEXT NOT NULL,
    status          TEXT NOT NULL   -- 'ok', 'failed'
);
```

---

## Summary

| Phase | Layer | Replaces / Adds | Target |
|-------|-------|-----------------|--------|
| 1 | Per-collection `meta.db` | `catalog.json` + `meta.json` | v0.5.8 |
| 2 | Global `catalog.db` | Filesystem walk for listings | v0.6 |
| 3 | Operational state | Tenant profiles, rate limits, log retention | v0.7–v0.8 |
| 4 | Governance | Versioning, audit, syndicates, usage, jobs | v0.8–v1.0 |
