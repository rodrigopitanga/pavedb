<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# PLAN-OBSERVABILITY — Inspectability & Telemetry (v0.9)

PaveDB's v0.9 milestone is named **Inspectability** because the
v0.9 cycle completes the surface that lets a developer answer
three questions without attaching a debugger:

1. **What got ingested?** — which documents, how many chunks,
   which metadata, which offsets, when.
2. **What did search actually do?** — which query ran, with
   what flags, against what index, returning what ranked IDs,
   spending time in which phase.
3. **Can I reproduce a past search?** — given a query_id from
   a week ago, re-run it against today's index and compare.

Inspectability is the counter-positioning against competitors
whose pipelines are opaque black boxes. Every step between
user intent and ranked result is, or will be, a first-class
inspectable artifact.

This plan documents the observability surface — what has
already shipped up to this point, and which remaining v0.9
tasks complete the inspectability story. Later telemetry
work (retention, export, eval, regression) is out of scope
here and tracked in its own items.

---

## Principle

Every step between the user-facing call and the returned
results is traceable, queryable, and — where feasible —
replayable, without leaving the product. Each step in the
pipeline either emits an event, persists state, or is exposed
via an inspection endpoint.

### Observability split

Two complementary streams with different audiences:

- **`query_log`** — tenant-owned replay/debug history. Lives
  in each collection's `meta.db`. Primary consumer is the
  tenant developer tuning relevance and replaying searches.
  Surfaced through tenant/collection-scoped API routes.
- **`ops_log`** — instance-admin operational observability.
  JSONL stream, designed for log aggregators. Primary consumer
  is the operator running the server.

Admin capabilities for query logs are intentionally narrow:
targeted lookup/replay by `query_id` via the `query_home`
resolver (P1-51), with no global browsing API. Global
query-log browsing is not a first-class concern; operator
needs are served by ops_log.

Pipeline steps and their inspection surface:

| Step | Inspect via | Delivered |
|---|---|---|
| Request correlation | `request_id` echoed in every envelope and `ops_event` | ✓ P1-11 |
| Request-correlation input cleanup | `X-Request-ID` as the sole public input channel | v0.9 planned (P1-48) |
| Per-request envelope | `ok` / `code` / `error` / `request_id` / `latency_ms` | ✓ P1-14 |
| Operational event stream | JSONL `ops_event` lines | ✓ P2-28 |
| Latency histograms | `/health/metrics`, `/metrics` (p50/p95/p99) | ✓ P0-03 |
| Ingest record | `GET /v1/collections/{t}/{c}/documents/{docid}` | ✓ P1-17 |
| Document listing | `GET /v1/collections/{t}/{c}/documents` | v0.9 planned (P1-44) |
| Collection catalog | `GET /v1/collections/{t}` (enriched) + detail | v0.9 planned (P1-45) |
| Chunk offsets in source | `meta.offset` on every chunk | ✓ P2-41 |
| Service-layer errors | `log.warning` on every `ok: false` site | ✓ P2-40 |
| Search timing breakdown | `timing.{embed,search,filter,hydrate}_ms` | ✓ P1-40 |
| Versioned API path | `/v1/` prefix, additive-only contract | ✓ P1-43 |
| Chunk inspection | list chunks + get chunk metadata + get chunk content | v0.9 (P2-23) |
| Query history | per-collection `query_log` + read endpoints | v0.9 planned (P1-41) |
| Admin bare-id resolver | `query_home` table in `catalog.db` | v0.9 planned (P1-51) |
| Query replay | `POST /v1/collections/{t}/{c}/queries/{id}/replay` | v0.9 planned (P1-42) |
| Frozen public schema | contract tests + field descriptions | v0.9 (P1-23) |

The remaining v0.9 observability work centers on P1-41,
P1-42, P1-44, P1-45, P1-48, P1-51, P2-23, and P1-23. Later
additions (result diff, eval assertions, regression
detection, retention, export) build on top and are listed
under **Not in scope**.

---

## What's already delivered

### Request correlation — P1-11 (v0.9)

Every request already carries a `request_id` through the
response envelope and the ops log. The current transport may
accept that correlation identifier through more than one input
channel; P1-48 tightens the public contract so `X-Request-ID`
is the only documented input, with the server minting one when
absent. The resulting ID is:

- Attached to `request.state.request_id` by middleware.
- Echoed in the response envelope (`request_id` field).
- Set on the response header (`X-Request-ID`).
- Emitted in the corresponding `ops_event` JSONL line.

Rationale: end-to-end correlation across logs and client
telemetry is the prerequisite for every other inspection
surface. Without it, a user-reported "bad result" cannot be
tied back to a specific search in the log.

### Response envelope — P1-14 (v0.9)

All JSON responses conform to `OkResponse` / `ErrorResponse`,
both extending `TraceResponse` with `request_id` and
`latency_ms`. Every call carries uniform envelope fields
regardless of the domain model it wraps. P1-11 populates the
trace fields; P1-40 extends latency with per-phase timing on
search.

### Operational event stream — P2-28 (v0.5.8)

A JSON-lines ops log, separate from the dev stderr log, emits
one event per operation with stable fields. Configurable via
`log.ops_log: null | stdout | <path>`. Full schema and
rationale: `docs/PLAN-OPS-LOG.md`.

This is the **transient** observability stream — designed for
log aggregators (Datadog, Loki, ELK). It is not queryable via
the API. The query log (P1-41) adds the **persistent,
queryable** counterpart for search-specific history.

### Latency histograms — P0-03 (v0.5.7)

`/health/metrics` and `/metrics` expose p50 / p95 / p99
histograms per operation (search, ingest). Histograms are the
hot-path SLO signal; the ops log is the cold-path audit
signal.

### Per-phase search timing — P1-40 (v0.9)

`SearchResponse.timing` carries `embed_ms`, `search_ms`,
`filter_ms`, `hydrate_ms` — the four observable phases
between query receipt and result return. Lets a developer
attribute slow searches to the actual bottleneck (embedding?
FAISS? post-filter? metadata hydration?) without enabling
debug logs.

### Read endpoints over persisted state — current base + v0.9 extensions

- Already shipped:
  `GET /v1/collections/{t}/{c}/documents/{docid}` — single
  doc with version, ingest timestamp, merged metadata.
- Planned for the rest of v0.9:
  `GET /v1/collections/{t}/{c}/documents` — full listing
  with per-doc chunk counts (single-query aggregate,
  no N+1) (P1-44).
- Planned for the rest of v0.9:
  `GET /v1/collections/{t}` — tenant's collections with
  doc / chunk counts and embedder config, plus
  `GET /v1/collections/{t}/{c}` detail (P1-45).

All read endpoints in this family should tolerate transient
`meta.db` read errors under concurrent writes (fallback to a
read-only open).

### Service-layer error logging — P2-40 (v0.5.9)

Every service function that can return `{"ok": False, ...}`
emits a `log.warning` with code, message, and context at the
return site. Before P2-40, only route-layer HTTP errors were
logged; store/service failures returned silently up the call
chain.

### Chunk offsets — P2-41 (v0.5.9)

TXT preprocessor emits `meta.offset` (character offset into
the source document) on every chunk. Document → chunk
provenance becomes exact-byte rather than approximate.

### Versioned API path — P1-43 (v0.9)

All business endpoints mount under `/v1/`. The shape is
frozen at 1.0 and additive-only after. Health and UI stay
unversioned (they are operational, not contract surface).

---

## What v0.9 adds

The v0.9 cycle closes the minimum viable inspectability
surface: P1-41 (persistent query log), P1-42 (replay),
P1-44/P1-45 (read-side browsing), P1-48 (request-correlation
input cleanup), P1-51 (admin query-home resolver), P2-46
(query_log + ops_log enrichment — shipped alongside P1-51),
P2-23 (chunk inspector), and P1-23 (pre-freeze).

### P1-41 — Persistent query log (2 commits)

A `query_log` table in **each collection's `meta.db`** records
every search against that collection: `query_id`, query text,
`k`, filters, `include_common` / `common_tenant` /
`common_collection`, result IDs in rank order, result count,
latency, phase timings, `request_id`, `replay_of`,
`executed_at`. `tenant` and `collection` remain part of the
API model, but are derived from the owning collection scope on
read rather than duplicated as columns inside the per-collection
table.

**Ownership model.** Query logs are primarily **tenant-owned
replay/debug data**, not instance-admin observability. The
tenant developer is the main consumer — they need to inspect
the queries they just issued and replay them to debug relevance.
Instance-admin operational concerns are already better served
by the ops log (P2-28). The two streams are intentionally kept
separate (see "Observability split" below).

**Storage direction.** The log lives with the collection, not
in the global `catalog.db`. This follows the collection
self-containment principle established in PLAN-STORE: copying,
renaming, or deleting a collection naturally carries its query
history. Path-based auth becomes trivial because ownership is
encoded in the URL. `catalog.db` is not the source of truth for
query-log rows and should not be treated as one.

Read endpoints (tenant/collection-scoped):
- `GET /v1/collections/{t}/{c}/queries` — paginated list
  (summaries only). Filters via query params: `limit`, `offset`
  today; future `request_id`, `replay_of`, `q`, `since`, `until`
  (keep filters in query params, not in extra path segments).
- `GET /v1/collections/{t}/{c}/queries/{query_id}` — full entry
  including `result_ids` and `timing`.

`service.search()` gains an internal `_log: bool = True`
kwarg so replay (P1-42) can suppress the auto-log and
attribute its run to its own `replay_qid`.

**Why this is different from the ops log:** the JSONL ops
log is write-once and rotates / ages out; the query log is
SQLite, queryable, joinable with the rest of the collection,
and retained until explicitly purged (retention is P2-39).
Both streams continue to run — they serve different
audiences (log aggregators vs. in-product inspection UIs
and debugging clients).

### P1-42 — Query replay endpoint (1 commit)

`POST /v1/collections/{t}/{c}/queries/{query_id}/replay` —
re-executes a logged query against the *current* index,
returning fresh results alongside the original result summary
(`original_query_id`, `replay_query_id`,
`original_result_count`, `original_latency_ms`, `matches`,
`timing`, `latency_ms`). The new run is itself logged with
`replay_of = original_query_id`.

Replay is faithful: it passes `include_common` /
`common_tenant` / `common_collection` from the stored
entry (critical for queries that ran with common-merge —
otherwise replay silently diverges).

Auth and rate-limit do **not** bypass the primary search
path: the handler resolves the entry, requires the caller to
be admin or the owning tenant (else 403), and counts the
replay against the tenant's concurrent-search limit. Execution
is gated through the same `_do_search` wrapper as the primary
search path — no separate concurrency budget.

Admin-shortcut routes at `/v1/admin/queries/{id}[/replay]`
ship later under P1-51; P1-42 itself contributes the
tenant/collection-scoped replay path and its replay semantics.

#### CLI

P1-42 ships no new CLI subcommand. The admin-only CLI rework
(bare-`query_id` `get-query` / `replay-query`, optional
`--tenant` / `--collection` filters on `list-queries`)
lands with P1-51, since every bare-id command depends on the
`query_home` resolver.

### P1-51 — Admin query-home resolver + shortcut routes + admin CLI rework (1 commit)

P1-42 ships with tenant/collection-scoped replay only.
Admin support and cross-tenant debugging need a bare-`query_id`
shortcut — a lookup by `query_id` alone without knowing the
owning `(tenant, collection)` upfront. P1-51 adds the
catalog-side resolver, the two admin HTTP routes that consume
it, and the admin-only CLI rework (bare-id `get-query` /
`replay-query`, optional-filter `list-queries`).

**Schema.** `query_home(query_id PK, tenant, collection,
created_at)` in `catalog.db`, with an index on
`(tenant, collection)` so collection deletion can purge
rows cheaply and filtered CLI listings stay fast.

**CatalogDB methods.** `put_query_home(query_id, tenant,
collection)`, `resolve_query_home(query_id) → (tenant,
collection) | None`, `purge_query_homes_for_collection(
tenant, collection)`, `list_query_homes(tenant=None,
collection=None, limit, offset) → list[dict]` (catalog-side
browsing used by the admin CLI; filters optional; pointer
rows ordered by `created_at DESC`).

**Orchestrator wiring.** `LocalStore.log_query` writes the
per-collection row first, then best-effort-upserts the
`query_home` row. Failure of the home upsert is logged at
`warning` level but does not abort the search log — the
entry remains reachable via the collection-scoped route.
`delete_collection` calls `purge_query_homes_for_collection`
after per-collection teardown so stale rows don't linger.
`rename_collection` updates the matching `query_home` rows
to the new collection name so bare-id resolution stays valid
after a rename.

**Service.** `resolve_query_home(store, query_id)` — thin
passthrough to `CatalogDB`. `list_query_homes(store, tenant=
None, collection=None, limit=50, offset=0)` — envelope
wrapper for CLI browsing. `get_query_log_entry(store,
query_id)` and `replay_query(store, query_id)` refactor to
bare-id: resolve via `query_home`, then hit the owning
collection's `query_log` (or delegate to P1-42's
`execute_replay`). The tenant/collection-scoped helpers stay
in place for the public collection routes, and they
cross-check `query_home` before the scoped fetch/replay:
if the `query_id` resolves to a different `(tenant,
collection)` than the scoped path, they return the normal
`query_not_found` envelope.

**Admin routes (`pave/routes/admin.py`).**

- `GET /v1/admin/queries/{query_id}` — admin-only
  (`Depends(auth_ctx)` + `ctx.is_admin` gate; 403
  otherwise). Calls `resolve_query_home`; 404 if unknown;
  otherwise delegates to the P1-42 tenant-scoped
  `get_query_log_entry` and returns `GetQueryLogResponse`.
- `POST /v1/admin/queries/{query_id}/replay` — same
  resolver pattern, then delegates to the same P1-42
  replay pipeline (`do_search(execute_replay, …)`).
  Replay still counts against the owning tenant's
  concurrent-search budget (the resolver just tells us
  who that tenant is).
- **Not** adding `GET /v1/admin/queries` — HTTP global
  listing is out of scope. Operator browsing happens via
  the admin CLI (below); cross-collection debugging happens
  via ops_log; fan-out across every collection's `meta.db`
  isn't worth the cost.

**Admin CLI rework (`pave/cli.py`).** PatchVec's CLI is an
admin-local tool — no per-tenant CLI persona — so every
query-log subcommand operates at admin scope.
Tenant/collection are filters at most, never required
positional context. This commit amends the P1-41-era
signatures:

- `pavecli list-queries [--tenant T] [--collection C]
  [--limit N] [--offset N]` — catalog-side browsing via
  `list_query_homes`. Returns pointer rows
  `(query_id, tenant, collection, created_at)`; pivot to
  `get-query <id>` for rich detail.
- `pavecli get-query <query_id>` — bare id; resolves via
  `query_home`, then fetches the full entry from the owning
  collection.
- `pavecli replay-query <query_id>` — bare id; resolves via
  `query_home`, then delegates to `execute_replay`.

Same argparse conventions as the rest of `cli.py`
(kebab-case subcommand, runtime parent, `--compact` flag,
JSON out via `_dump`).

**Scope guardrails.** `query_home` is a pure pointer index —
no duplicated query data, no summaries, no hot-path reads.
Anything heavier belongs in the per-collection `query_log`.

### P2-23 — Chunk inspector (1 commit)

Three endpoints, metadata split from content so chunks stay
content-type-agnostic (forward-compat with non-text chunks:
transcripts, OCR, page renders):

- `GET /v1/collections/{t}/{c}/documents/{docid}/chunks` —
  summaries only (RID, chunk_path, meta, ingested_at). No
  text preview: loading previews would be O(N) sidecar-file
  reads per request, the same N+1 class of bug avoided in
  P1-44/P1-45.
- `GET /v1/collections/{t}/{c}/chunks/{rid}` — single chunk
  metadata (rid, docid, chunk_path, meta, ingested_at). No
  `text` field.
- `GET /v1/collections/{t}/{c}/chunks/{rid}/content` — raw
  bytes. v0.9 hardcodes `Content-Type: text/plain; charset=utf-8`;
  returns `Response`, not `OkResponse` (the only v0.9 endpoint
  that opts out of the envelope — P1-14 doesn't fit binary
  payloads).

Same transient-read-error fallback shape as `get_document`.

Document content retrieval (`GET /documents/{docid}/content`)
and source-file retention at ingest are deferred to P3-30
(retain originals opt-in + content inspector), grouped with
multimodal retrieval work.

### P1-23 — Pre-freeze: contract tests + field descriptions (1 commit)

Runs LAST in v0.9. Adds `Field(description=...)` to every
public model, plus two contract test files:

- `test_schema_freeze.py` — per-model field-name
  superset assertions (additive fields OK, removals /
  renames fail CI).
- `test_openapi_descriptions.py` — parametrized test
  that every field on every public model has a non-empty
  `description`.

This is the **pre-freeze**, not the 1.0 freeze. Additive
changes remain legal until 1.0; from 1.0 on, fields and
endpoints cannot be removed without a `/v2/` branch.

---

## Architecture summary

```
                         ┌─────────────────────┐
                         │   HTTP request      │
                         │  (optional Req-ID)  │
                         └──────────┬──────────┘
                                    │
         X-Request-ID middleware  ──┼──►  request.state.request_id
                                    │                          (P1-11)
                                    ▼
                         ┌─────────────────────┐
                         │   route handler     │
                         │   @ops_event(...)   │──► ops_log JSONL
                         └──────────┬──────────┘    (P2-28, P1-11)
                                    │
                                    ▼
                         ┌─────────────────────┐
                         │   service.search    │──► dev log (P2-40)
                         │   (returns timing)  │    histograms (P0-03)
                         └──────────┬──────────┘
                                    │
                                    ├──► store.log_query(...)
                                    │    ┌────────────────────┐
                                    │    │ CollectionDB       │
                                    │    │  (per-coll meta.db)│
                                    │    │  query_log table   │◄── P1-42
                                    │    │  (P1-41)           │    replay
                                    │    └────────────────────┘
                                    ▼
                         ┌─────────────────────┐
                         │ OkResponse envelope │
                         │  request_id +       │
                         │  latency_ms +       │
                         │  timing +           │──► client
                         │  matches            │
                         └─────────────────────┘

Read-side inspection:
  GET /v1/collections/{t}                       → list collections + counts
  GET /v1/collections/{t}/{c}                   → collection detail
  GET /v1/collections/{t}/{c}/documents         → list docs + chunk counts
  GET /v1/collections/{t}/{c}/documents/{d}     → get document
  GET .../documents/{d}/chunks                  → list chunks (P2-23)
  GET .../chunks/{rid}                          → get chunk (P2-23)
  GET /v1/collections/{t}/{c}/queries           → list queries (P1-41)
  GET /v1/collections/{t}/{c}/queries/{id}      → get query entry (P1-41)
  POST /v1/collections/{t}/{c}/queries/{id}/replay  → replay (P1-42)

Admin-only convenience shortcuts (not the primary API):
  GET  /v1/admin/queries/{id}            → query_home resolve + fetch
  POST /v1/admin/queries/{id}/replay     → query_home resolve + replay
  (resolver = CatalogDB.query_home, P1-51)
```

---

## File ownership

| Layer | File | Observability role |
|---|---|---|
| Middleware | `pave/main.py` (request-id mw) | Mint/accept `X-Request-ID`, populate `request.state` |
| Logging | `pave/log.py` | `ops_event` decorator, JSONL emit, dev log |
| Metadata | `pave/metadb.py` | `CollectionDB.query_log` (P1-41, per-collection) + `CatalogDB.query_home` resolver for admin shortcuts (P1-51) |
| Service | `pave/service.py` | Timed search, `_log` gate, replay orchestration |
| Schemas | `pave/schemas.py` | Envelopes + `SearchTiming` + query-log models |
| Routes | `pave/routes/search.py` | Queries list/get + replay (tenant/collection-scoped) |
| Routes | `pave/routes/admin.py` | Admin convenience `queries/{id}` + replay shortcuts |
| Routes | `pave/routes/documents.py` | Doc + chunk inspectors |
| Routes | `pave/routes/collections.py` | Catalog browser |
| Tests | `tests/test_schema_freeze.py` | Envelope freeze guard (P1-23) |

---

## Not in scope (follow-ups, not in this plan)

These items build on the v0.9 surface but belong to later
releases. Each has its own ROADMAP entry.

| ID | Item | Target | Depends on |
|---|---|---|---|
| P2-39 | Log retention (rolling window, purge) | v1.2 | SQLite Phase 3 |
| P2-13 | Collection log export | v1.4 | P2-39 |
| P2-42 | Result diff API (compare two search runs) | v1.2 | P1-41 |
| P2-43 | Eval assertion API (define expected results) | v1.2 | P1-41 |
| P2-44 | Regression detection across versions | v1.2 | P2-43 |
| P2-45 | Config snapshot per collection at ingest | v1.2 | P1-33 |
| P2-37 | Audit logs for admin actions | v2.0 | P2-28 |

Post-1.0 observability items (multimodal chunk inspection,
streaming replay, alerting integrations) are not on the
current roadmap.

---

## Verification checklist (v0.9 closing)

Before cutting v0.9:

1. `make test` — all contract tests pass; schema freeze
   asserts every envelope; description test covers every
   public model.
2. POST search, then GET the collection's queries list → the
   search appears within one page.
3. GET `/v1/collections/{t}/{c}/queries/{id}` → full entry
   with `timing` and common-merge flags. The same `query_id`
   under the wrong collection path returns 404 (scoped path
   cross-checks `query_home`).
4. POST `/v1/collections/{t}/{c}/queries/{id}/replay` → fresh
   results + `replay_query_id` distinct from `original_query_id`;
   replay is itself logged, with `replay_of = original_query_id`.
5. Cross-tenant replay with a tenant key → 403; admin key or
   owning-tenant key → 200. Replay counts against the owning
   tenant's concurrent-search limit (429 when saturated).
6. Admin shortcuts: `GET /v1/admin/queries/{id}` resolves via
   `CatalogDB.query_home` (P1-51) and returns the same entry
   as the collection-scoped endpoint; `POST` replay by bare id
   returns the same replay envelope as the collection-scoped
   route. Delete the owning collection → subsequent resolve
   returns 404 (no stale `query_home` rows). No global
   listing route exists.
7. Ingest a doc, GET its chunks listing → no text_preview
   field, chunk_path populated.
8. GET a chunk by RID with `::` in the path → 200 with full
   text.
9. `GET /openapi.json` — every public model field has a
   description; frozen models appear with their documented
   fields.
10. Set `X-Request-ID: abc123` on any call → echoed in the
    response envelope, ops log, and (for search) the query
    log row.
