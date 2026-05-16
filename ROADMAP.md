<!-- (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Roadmap

tl;dr: This roadmap tracks production readiness and integration milestones. To claim a
task, open an issue titled `claim: <task ID>` and link your branch or PR.
Detailed design plans live under `docs/` and are indexed after the task tables.

> PaveDB is a general-purpose vector search engine. The roadmap that follows is
> driven by production readiness for its first downstream consumer — an application
> that maps natural-language queries to structured codes via semantic search in a
> non-English language. The core product metric is **time saved to reach the correct
> results**. Every TODO is evaluated against that metric and general production
> readiness.

## Design Principles

These are non-negotiable constraints that apply across all versions.

- **Zero boilerplate by default.** A working setup requires no org, workspace, profile,
  or grouping ceremony. Create a collection and go. Groupings, limits, and profiles are
  opt-in — never required for a basic setup.
- **Collection independence.** Collections are not owned by tenants. The tenant is a
  runtime namespace, not a structural owner. Collections must be fully portable: export
  from one instance/tenant, import into another without data loss or format surprise.
  This is a logical portability/lifecycle guarantee, not a promise that each
  collection maps to its own physical DB file, DB instance, or vector service.
- **Transparency by default.** Developers must be able to see what was indexed, what
  chunks were produced, what metadata is stored. Opacity is a DX failure.
- **Layered independence.** Auth, tenant profiles, collections, and server configuration
  are orthogonal concerns. No layer forces coupling to another. A tenant can exist
  without a profile; a collection can exist without a custom embedding config.
- **Optional tenant groupings ("syndicates").** When tenant grouping is needed (e.g.
  for org-level quotas or shared collections), it is expressed as a lightweight
  syndicate
  — an opt-in overlay, not a mandatory hierarchy. No boilerplate orgs/workspaces.
- **Server and library are the same thing.** PaveDB must run equally well as an HTTP
  microservice and as an in-process Python library (embedded, single-tenant, no
  uvicorn).
  The service layer is the API; HTTP is just one transport.
- **Media types are progressive, not baked in.** Text is the baseline. Every additional
  media type (images, audio, video, and beyond) is added through a stable ingest plugin
  interface without touching the core. The plugin contract must be stable before any
  specific media type ships.
- **Collections are version-safe.** Every collection records the PaveDB version and schema
  version it was written with. Incompatible reads must fail loudly with actionable
  guidance, not silently corrupt. Migration tooling ships alongside breaking changes.
- **PaveDB enforces limits; it does not govern business logic.** Resource limits,
  quotas, and tier profiles are read from a manifest and applied at runtime. Billing,
  onboarding, and payment are outside PaveDB's scope — it just reads a file.

---

## Priority Evaluation (external driver)

### P0 — Blocks first consumer GA launch

Effort legend: 🧩 bite-sized, 🔧 medium, 🧱 foundational

| ID | Task | Effort | Why it blocks | Source |
|---|---|---|---|---|
| P0-01 | ~~Multilingual embedding model~~ |  | Non-EN recall | v0.5.7 |
| P0-02 | ~~`match_reason` on every hit~~ |  | Trust + explanation gap | v0.5.7 |
| P0-03 | ~~Latency histograms~~ |  | No latency visibility | v0.5.7 |
| P0-04 | ~~Negation pre-filter~~ |  | Tail latency | v0.5.7 |
| P0-05 | ~~`trace_id` propagation~~ |  | No request correlation | v0.5.8 |

### P1 — Critical for first B2B pilots

| ID | Task | Effort | Why it matters | Source |
|---|---|---|---|---|
| P1-06 | ~~Delete doc by ID~~ | 🧩 | No partial data fixes | v0.5.7 |
| P1-07 | Hybrid reranking | 🧱 | Exact token boost | v1.2 |
| P1-08 | ~~Per-tenant rate limiting~~ |  | Abuse protection | v0.5.8 |
| P1-09 | ~~Metadata store (SQLite)~~ | 🧱 | ACID + concurrency | v0.5.8 |
| P1-11 | ~~Global `request_id` echo~~ | 🧩 | Traceability | v0.9 |
| P1-12 | ~~Ingest timeout guidance~~ | 🧩 | Avoid client timeouts | v0.5.8 |
| P1-13 | ~~Ingest size limits~~ | 🧩 | Fail fast on huge uploads | v0.5.8 |
| P1-14 | ~~Response envelope standardization~~ | 🧱 | SDK-friendly API | v0.9 |
| P1-15 | Embedded/library mode | 🧱 | In-app use, adoption | v1.1 |
| P1-16 | Batch ingest endpoint | 🧩 | Throughput, DX | v1.1 |
| P1-17 | ~~Get document by ID~~ | 🧩 | Visibility, library mode | v0.9 |
| P1-18 | ~~Error code standardization~~ | 🧩 | Consistent API errors | v0.5.8 |
| P1-19 | ~~`build_app()` lazy init~~ | 🧩 | Testability, startup safety | v0.5.8 |
| P1-20 | ~~Search timeout + concurrency cap~~ | 🔧 | Graceful degradation | v0.5.8 |
| P1-21 | ~~Serve listings + store catalog counts in health/metrics~~ | 🧩 | Internal store query layer | v0.5.8 |
| P1-22 | Per-collection hot caches | 🧱 | Performance isolation | v1.0 |
| P1-23 | Freeze search response schema | 🧩 | SDK contract | v0.9 |
| P1-24 | Python client package | 🧱 | SDK foundation | v1.1 |
| P1-25 | Dev vs prod config defaults | 🔧 | Safe defaults | v1.0 |
| P1-34 | ~~Server config bootstrap~~ | 🧩 | Explicit `pavecli init` + `--home` / explicit runtime paths for pip installs | v1.0 |
| P1-35 | Filter pushdown parity harness | 🔧 | Speedups without semantic drift | v1.0 |
| P1-26 | Config reference + CI doc check | 🧩 | Config clarity | v1.0 |
| P1-27 | Admin key auto-generate + persist | 🧩 | Secure bootstrap | v1.0 |
| P1-28 | Moving-window rate limiting per tenant | 🔧 | req/min, req/hour — needs `rate_limit_buckets` table (Phase 3) | post-SQLite |
| P1-29 | ~~VectorBackend protocol~~ | 🔧 | Initial backend seam for store split | v0.5.9 |
| P1-29b | ~~Clean protocol + Faiss path~~ | 🔧 | Finish FAISS cutover | v0.5.9 |
| P1-29c | ~~CollectionDB k/v pre-filter~~ | 🔧 | First pushdown stage | v0.5.9 |
| P1-36 | ~~Reject empty/colliding sanitized metadata keys~~ | 🧩 | Avoid silent key drops/coalescing under current sanitization rules | v0.5.9 |
| P1-37 | ~~Pre-orchestrator cleanup~~ | 🔧 | Drop txtai dep, rename store/embedder, dead code removal, filter path simplification | v0.5.9 |
| P1-30 | ~~Activate embedder factory cache (superseded by P1-29b)~~ |  | Superseded by Step 2 in PLAN-STORE | superseded |
| P1-31 | ~~Store orchestrator~~ | 🧱 | Orchestrate backend + meta + catalog | v0.5.9 |
| P1-32 | Per-collection embeddings | 🧱 | Model per collection | v1.0 |
| P1-33 | ~~CatalogDB + catalog separation~~ | 🧱 | Catalog + collection backend/embedder config source | v0.9 |
| P1-40 | ~~Search timing breakdown in response~~ | 🔧 | Latency debugging: embed/search/filter/hydrate split | v0.9 |
| P1-41 | ~~Persistent query log~~ | 🔧 | Queryable search history (query, filters, config, result IDs) in SQLite | v0.9 |
| P1-42 | ~~Query replay endpoint~~ | 🧩 | Re-execute stored query; depends on P1-41 | v0.9 |
| P1-43 | ~~`/v1/` route prefix~~ | 🔧 | Versioned API base path; frozen at v1.0, additive after | v0.9 |
| P1-51 | ~~Admin query-home resolver + shortcut routes + CLI rework~~ | 🧩 | Bare-`query_id` lookup/replay for admin via `query_home` table in `catalog.db` | v0.9 |
| P1-46 | Docs site — preview | 🔧 | MkDocs static site; user + developer sections; published via GitLab Pages | v0.9 |
| P1-47 | Docs site — 1.0 | 🧱 | Full coverage: guides, core concepts, inspect/debug, operations, architecture, plugins | v1.0 |
| P1-48 | ~~Remove `SearchBody.request_id`~~ | 🧩 | `X-Request-ID` becomes the only request-correlation input | v0.9 |
| P1-49 | HTTP request metrics middleware | 🔧 | Per-endpoint request count + latency histogram, status class split, Prometheus `# HELP`/`# TYPE` metadata | v1.0 |
| P1-50 | Product-signal metrics | 🧩 | Zero-match searches, filter usage, query-log readiness, embedder counters, sidecar drift — competitor gap | v1.0 |

### P2 — Enables enterprise use cases and competitive moat

| ID | Task | Effort | Why it matters | Source |
|---|---|---|---|---|
| P2-11 | `meta.priority` boosts |  | Surface priority items | v1.2 |
| P2-12 | ~~List tenants/collections API~~ | 🧩 | Ops visibility | v1.0 |
| P2-13 | Collection log export | 🧱 | Search analytics | v1.4 |
| P2-14 | Document versioning | 🧱 | Audit trails | v1.7 |
| P2-19 | Tenant admin infra | 🧱 | Admin ops | v1.5 |
| P2-38 | Tenant key management API | 🔧 | Generate/revoke keys, YAML seed → SQL | v1.6 |
| P2-20 | Collection limit / tenant | 🧩 | Cap growth | v1.5 |
| P2-21 | Storage limit / tenant | 🧩 | Cap storage | v1.5 |
| P2-47 | Per-collection storage accounting | 🔧 | Separate document/chunk/index/meta bytes; expose `document_bytes` for quotas, keep `chunk_bytes` admin-only | v1.5 |
| P2-22 | Usage stats to mothership |  | Capacity planning | v1.4 |
| P2-23 | Chunk inspector + collection browser | 🔧 | List chunks, get chunk by ID, browse doc→chunk tree | v0.9 |
| P2-24 | Delete by ID list / by query | 🧩 | Bulk ops, DX | v1.6 |
| P2-25 | Collection version tagging | 🧩 | Portability, migration | v1.5 |
| P2-26 | Tenant profiles + templates | 🧱 | Quota governance, tiers | v1.6 |
| P2-42 | Result diff API | 🔧 | Compare two search runs: added/removed/reordered | v1.2 |
| P2-43 | Eval assertion API | 🧱 | Define expected results, batch run, track pass/fail | v1.2 |
| P2-44 | Regression detection | 🔧 | Compare eval runs across versions, flag drift; depends on P2-43 | v1.2 |
| P2-45 | Config snapshot per collection | 🧩 | Record embedder model + version + search params at ingest | v1.2 |
| P2-46 | ~~query_log + ops_log enrichment~~ | 🧩 | Historical `tenant` / `collection` / `actor` columns; `actor` field in ops_log | v0.9 |
| P2-28 | ~~Structured log emission~~ | 🧩 | JSON lines per operation with request_id, tenant, latency | v0.5.8 |
| P2-40 | ~~Error logging at service layer~~ | 🧩 | `log.warning` on every `ok: false` return site; audit codes and choose level per error class | v0.5.9 |
| P2-39 | Structured log retention | 🔧 | Rolling window + purge via `operation_log` (SQLite Phase 3); powers P2-13 | v1.2 |
| P2-29 | ~~Public cross-language retrieval fixtures~~ | 🧩 | Recall validation | v0.5.9 |
| P2-30 | ~~Benchmark CI gate + p99 SLO~~ | 🧩 | Latency contract | v0.5.9 |
| P2-48 | `bench-stress-full` profile | 🔧 | Off-by-default stress run covering every public endpoint; full-surface regression profile | v1.0 |
| P2-31 | Formalize collection independence | 🔧 | Portability contract | v1.5 |
| P2-32 | `pavecli --host` remote mode | 🧩 | CLI/SDK parity | v1.3 |
| P2-33 | JS/TS client | 🧱 | Web + Node adoption | v1.3 |
| P2-34 | LangChain adapter | 🧱 | Framework coverage | v1.3 |
| P2-35 | MCP server | 🧱 | AI agent integration | v1.3 |
| P2-36 | LlamaIndex adapter | 🧱 | Framework coverage | v1.3 |
| P2-37 | Audit logs for admin actions | 🧩 | Governance trail | v2.0 |
| P2-41 | ~~TXT preprocessor: character offset in chunk metadata~~ | 🧩 | Provenance contract (replace `chunk` key with `offset`) | v0.5.9 |

### P3 — Scale and long-term

| ID | Task | Effort | Source |
|---|---|---|---|
| P3-15 | Async ingest + parallel purge |  | v1.8 |
| P3-16 | Horizontal scalability + routing | 🧱 | v1.8 |
| P3-17 | OIDC/JWT auth (additive; API keys remain) | 🧱 | v1.6 |
| P3-18 | API freeze + SDK client | 🧱 | v2.0 |
| P3-23 | Docs website | 🧩 | v1.4 |
| P3-24 | Revamp UI | 🧱 | v1.4 |
| P3-25 | Multilingual UI/errors/docs | 🧱 | v1.4 |
| P3-26 | Embedder/store contract | 🧱 | v1.0 |
| P3-28 | Extensible ingest plugin architecture | 🧱 | v1.6 |
| P3-30 | Retain original uploaded files (opt-in) + content inspector | 🧱 | v1.8 |
| P3-31 | Async ingest jobs + job status API | 🧱 | v1.8 |
| P3-32 | Per-tenant parallel ingest limits | 🧱 | v1.8 |
| P3-34 | ~~Relicensing (AGPLv3 candidate)~~ | 🧱 | v0.5.9 |
| P3-35 | ~~Rebranding phase 1 (PaveDB candidate); phase 2~~ | 🧱 | v0.5.9–v0.9 |
| P3-36 | Multimodal collections (cross-modal search) | 🧱 | post-1.0 |
| P3-37 | Collection migration tooling (version compat) | 🧱 | v1.7 |
| P3-40 | Publish pip freeze snapshot | 🧩 | v1.0 |
| P3-41 | Swagger UI tenant/collection defaults | 🧩 | v1.4 |
| P3-42 | Alive test in CI | 🧩 | v1.4 |
| P3-43 | Go client | 🧱 | post-1.8 |
| P3-44 | Persistent metrics in UI | 🧩 | v1.7 |
| P3-45 | Independence principle audit | 🔧 | v1.6 |
| P3-46 | Matrix CI builds | 🧱 | v1.8 |
| P3-47 | Additional media types | 🧱 | post-1.0 |
| P3-50 | ~~Split main.py routes into APIRouter modules (health, admin, collections, documents, search)~~ | 🧩 | v0.5.9 |
| P3-51 | ~~`make docker-check`: alive test against prebuilt Docker image~~ | 🧩 | v0.5.9 |
| P3-52 | ~~`make build-check`: install from local wheel in temp venv, alive test~~ | 🧩 | v0.5.9 |

---

## Plan Docs

Substantial features are specified under `docs/` before implementation.

| File | Plan ID | Feature |
|---|---|---|
| [`docs/PLAN-OPS-LOG.md`](docs/PLAN-OPS-LOG.md) | P2-28 | Structured log emission — ops JSON stream |
| [`docs/PLAN-SQLITE.md`](docs/PLAN-SQLITE.md) | P1-09 / P1-33 | Internal SQLite metadata and global catalog store |
| [`docs/PLAN-STORE.md`](docs/PLAN-STORE.md) | P1-29/P1-29b/P1-29c/P1-31/P1-32 | Store split; P1-30 superseded by P1-29b |
| [`docs/PLAN-OBSERVABILITY.md`](docs/PLAN-OBSERVABILITY.md) | P0-03, P1-11/14/17/23/40/41/42/43/44/45/48, P2-23/28/40/41 | Inspectability surface — request correlation, timing, query log, replay, chunk inspector |
| [`docs/PLAN-DOCS.md`](docs/PLAN-DOCS.md) | P1-46 / P1-47 | Docs site plan (preview for v0.9, full for v1.0) |

---

## Release Schedule (internal driver)

### PatchVec v0.1 — Prototype
- First search + ingest pipeline; single-tenant, FAISS-backed, sbert embeddings.
- CLI-driven TXT ingestion and REST search endpoint; minimal auth stub.

### PatchVec v0.2 — Isolation
- Multi-tenant routing (`/{tenant}/{collection}`) with per-tenant API key auth.
- Collection creation, deletion, and document management endpoints.

### PatchVec v0.3 — Extension
- QdrantStore skeleton, OpenAI embedder proof of concept, unified `CFG` config.
- Full CLI mode added.

### PatchVec v0.4 — Modularity
- Codebase split into `stores/`, `embedders/`, `auth.py`, `service.py`, `cli.py`,
  `preprocess.py`, `metrics.py`.

### PatchVec v0.5 — Pluggability
- `BaseStore` ABC, `StoreFactory` + `EmbedderFactory`; runtime backend selection.
- `/health` endpoint; `DummyStore` for isolated testing.

### PatchVec v0.5.1 — Hardening
- Auth refactored into dependency-injected `auth_ctx()`; unified GET/POST search.
- Comprehensive pytest suite; factories migrated to `match` syntax.

### PatchVec v0.5.2 — Ingestion
- CSV and PDF ingest alongside TXT; `TxtaiEmbedder`, `OpenAIEmbedder`, `SbertEmbedder`.
- Fixed JSON body search route; docker-compose stub added.

### PatchVec v0.5.3 — Foundation
- Makefile release flow, GitLab/GitHub CI/CD, `.env.example`, `tenants.yml`.
- README split into user + contributor docs; REST curl examples; PyPI install path.

### PatchVec v0.5.4 — Launch
- Initial public release; CSV ingestion knobs, deterministic doc-ID re-ingest.
- Request metrics standardized; Docker GPU/CPU + PyPI publish pipeline bootstrapped.

### PatchVec v0.5.5 — Refinement
- FAISS index initialization on collection creation; correct text retrieval from store.
- Auth edge cases fixed; entry point hardened for production binding.

### PatchVec v0.5.6 — Deployment
- Docker GPU/CPU split pipeline; Swagger/OpenAPI UI with branding and auth helpers.
- Ingestion timestamps; improved FAISS concurrency and chunk text persistence fallback.

### PatchVec v0.5.7 — Readiness
- ~~Switch default embedding model to multilingual (e.g., `paraphrase-multilingual-
MiniLM-L12-v2`).~~
- ~~Return a `match_reason` field alongside every search hit.~~
- ~~Return `latency_ms` in every search response (market practice §1).~~
- ~~Push `!`-prefixed negation filters into SQL pre-filter (`<>`) for performance
(market practice §4).~~
- ~~Accept and propagate `request_id` / `trace_id` through search requests, responses,
and logs (market practice §7).~~
- ~~Expose latency histograms (p50/p95/p99) via `/metrics` for search and ingest.~~
- ~~Provide REST/CLI endpoints to delete a document by id.~~
- ~~Document the live-data-update path (purge + ingest).~~
- ~~Replace `eval()` in filter matching with `operator` module.~~
- ~~Replace `assert` in `index_records` with a proper runtime check.~~
- ~~Fix `_LOCKS` dict race condition with a global guard lock (market practice §8).~~
- ~~Ship initial `benchmarks/` directory with search latency load test (market practice
§6).~~
- ~~Push legacy typing synthax to Python 3.10~~
- ~~Update copyright notices, polish logging infrastructure~~

### PatchVec v0.5.8 — Resilience

- ~~Error code standardization (consistent codes/messages).~~
- ~~Add ingest size limits with clear errors.~~
- ~~Document ingest timeout guidance (client/proxy/uvicorn).~~
- ~~Make `build_app()` lazy; avoid eager app creation at import time.~~
- ~~Configurable search timeout + `max_concurrent_searches` with 503 fast-fail (market
practice §5).~~
- ~~Per-tenant and per-operation API rate limits (market practice §8 — quota governance).~~
- ~~Ship internal metadata/content store (SQLite) with migrations.~~
- ~~Serve `/collections` and store-backed catalog counts in `/health/metrics` + `/metrics`
  from the internal store (runtime op counters still come from `metrics.json` until
  Phase 3).~~
- ~~Emit structured logs (JSON lines) with `request_id`, tenant, collection, and
latency on every search/ingest/delete.~~
- ~~Support renaming collections through the API and CLI.~~

### PatchVec v0.5.9 — Relevance (last PatchVec release)

- ~~Extract VectorBackend protocol seam (P1-29).~~
- ~~Set backend seam to `search(vector, k)` and split `pave/backends/`
  (P1-29b slice A).~~
- ~~Finish `P1-29b` with Faiss backend cutover and SQL-path removal.~~
- ~~Add first `CollectionDB` k/v pre-filter stage (P1-29c).~~
- ~~Reject empty/colliding sanitized metadata keys instead of silently
  dropping/coalescing them (P1-36).~~
- ~~Pre-orchestrator cleanup: drop txtai, rename store/embedder/metadb,
  remove dead code, simplify filter path (P1-37).~~
- ~~Build store orchestrator: CollectionDB + FaissBackend
  + embedder (P1-31).~~
- ~~Build public cross-language retrieval fixtures (P2-29).~~
- ~~Add benchmark CI gate + p99 latency SLO (P2-30).~~
- ~~`make build-check`: install from local wheel in temp venv, alive test (P3-52).~~
- ~~`make docker-check`: alive test against prebuilt Docker image (P3-51).~~
- ~~Rebranding phase 1 (runtime/operator surface changes) (P3-35).~~
- ~~Split `main.py` into APIRouter modules per domain (P3-50).~~
- ~~Add service-layer error logging for `ok: false` sites (P2-40).~~
- ~~TXT preprocessor: emit char `offset` in chunk metadata (P2-41).~~
- ~~Relicensing review (AGPLv3 candidate) (P3-34).~~

### PaveDB v0.9 — Inspectability

- ~~Get document by ID endpoint (P1-17).~~
- ~~CatalogDB + catalog separation (PLAN-SQLITE Phase 2), including
  collection backend/embedder config wiring (P1-33).~~
- ~~Response envelope standardization (P1-14).~~
- ~~Global `request_id` echo across endpoints and responses (P1-11).~~
- Freeze search response schema (`matches`, `latency_ms`,
  `match_reason`, `request_id`) (P1-23).
- ~~Search timing breakdown in response: embed/search/filter/hydrate
  split alongside existing `latency_ms` (P1-40).~~
- ~~Persistent query log: store query text, filters, config snapshot,
  and result IDs in each collection's `meta.db` (P1-41).~~
- ~~Query replay endpoint: re-execute stored query at
  `POST /v1/collections/{t}/{c}/queries/{id}/replay` (P1-42).~~
- ~~Admin query-home resolver + shortcut routes + CLI rework:
  small `query_home` table in `catalog.db` mapping
  `query_id → tenant, collection`. Adds admin-only `GET` /
  `POST .../replay` at `/v1/admin/queries/{id}` delegating
  to P1-42 handlers after resolving the owning collection,
  plus admin-only CLI rework (`get-query` / `replay-query`
  accept a bare `<query_id>`; `list-queries` takes optional
  `--tenant` / `--collection` filters). No global HTTP
  listing (P1-51).~~
- ~~query_log + ops_log enrichment: `query_log` gains
  historical `tenant` / `collection` / `actor` columns
  (audit + portability); `ops_log` events gain `actor`
  (P2-46).~~
- Chunk inspector + collection browser: list chunks, get chunk
  by ID (text + metadata + provenance), doc→chunk tree (P2-23).
- ~~Mount all routes under `/v1/` prefix; drop unversioned routes
  (no compat shim pre-GA). Contract: frozen at v1.0, additive
  only after (new endpoints, optional fields). `/v2/` introduced
  only if a `/v1/` shape must break (P1-43).~~
- ~~Rebranding phase 2: public-facing rename, env fallback removal,
  and `patchvec` → `pavedb` redirect/shim path (P3-35).~~
- ~~Remove `SearchBody.request_id`; `X-Request-ID` becomes the
  single documented input channel for request correlation
  (P1-48).~~
- Docs site — preview: MkDocs Material static site with
  getting-started guide, core concepts (tenants, collections,
  chunks, metadata, filters), inspect/debug walkthrough using
  the v0.9 endpoints (query log, replay, chunk inspector,
  request_id correlation), and a developer section seeded from
  `docs/PLAN-*.md`. Published via GitLab Pages at a stable URL.
  Text-first, no marketing chrome (P1-46).

### PaveDB v1.0 — Stability

- Define embedder/store separation contract (P3-26).
- ~~Activate embedder factory cache (P1-30; superseded by P1-29b).~~
- Per-collection embeddings (P1-32).
- Per-collection hot caches with isolation (P1-22).
- Dev vs prod config defaults (P1-25).
- ~~Explicit config bootstrap for pip installs (`pavecli init`,
  `--home`, explicit runtime paths) (P1-34).~~
- Add capability-based filter pushdown with parity checks against
  canonical post-filter semantics (P1-35).
- Admin key auto-generate + persist (P1-27).
- Config reference doc + CI drift check (P1-26).
- Publish `pip freeze` snapshot as release artifact (P3-40).
- ~~List tenants and collections via API (CLI parity) (P2-12).~~
- Docs site — 1.0: expand the v0.9 preview to full release
  coverage. User-facing: install, quickstart, core concepts,
  auth, ingest, search, filters, inspect/debug, operations
  (config, limits, metrics, health). Developer-facing:
  architecture, service/store/embedder seams, plugin contract,
  internals. Auto-generated API reference from OpenAPI. Search,
  versioned URLs, dark/light theme. Seeds future language
  translations (P1-47).
- HTTP request metrics middleware: single middleware records
  per-endpoint count + latency histogram labeled by method,
  path template, and status class. Removes per-handler `inc`
  sprawl. Adds `# HELP`/`# TYPE` metadata and standard
  histogram buckets to `/metrics`. Prometheus parity baseline
  (P1-49).
- Product-signal metrics: zero-match search rate, filter-usage
  split, common-merge count, search timeout count, query-log
  rows + oldest-entry age, embedder counters, sidecar drift
  gauges. Signals competitors don't expose; directly ties
  metrics to the inspectability thesis (P1-50).

### PaveDB v1.1 — Adoption

- Python client package (`pave`): HTTP mode for remote instances;
  library mode for in-process use (no HTTP). Same package, two
  transports.
- Embedded/library mode: run PaveDB in-process without HTTP server
  (expose service + store layer as a Python API; single-tenant
  default).
- Batch ingest endpoint (list of documents in one call).

### PaveDB v1.2 — Control

- Honor `meta.priority` boosts during scoring (P2-11).
- Add hybrid reranking (vector similarity + BM25/token
  matching) (P1-07).
- Result diff API: compare two search runs — added/removed/reordered
  hits (P2-42).
- Eval assertion API: define expected results per query, batch run,
  track pass/fail over time (P2-43).
- Regression detection: compare eval runs across versions, flag
  drift (P2-44).
- Config snapshot per collection: record embedder model + version +
  search params at ingest time (P2-45).
- Structured log retention: rolling window + purge via
  `operation_log` table (SQLite Phase 3); powers P2-13 (P2-39).

### PaveDB v1.3 — Reach

- `pavecli --host`: route CLI commands through the HTTP client
  instead of the service layer directly; depends on Python client.
  CLI becomes a thin wrapper.
- JavaScript/TypeScript client: typed, bootstrapped from OpenAPI
  spec, published to npm. Covers web frontends and Node.js backends.
- LangChain `VectorStore` + `Retriever` adapter (covers LangGraph
  + CrewAI).
- MCP server (expose search/ingest/list as MCP tools).
- LlamaIndex `VectorStore` adapter.

### PaveDB v1.4 — Polish

- Default tenant/collection selectors in Swagger UI.
- Collection-level structured log export for analytics.
- Alive test in CI pipeline (post-deploy health check).
- Docs website (public docs, API reference).
- Full-surface stress profile for every public endpoint,
  off by default (P2-48).
- Revamp UI.
- Multilingual UI/errors/docs.
- Usage stats to mothership (opt-in/anon).

### PaveDB v1.5 — Lifecycle

- Tenant admin infrastructure (P2-19).
- Per-tenant collection count limit (P2-20).
- Per-tenant storage limit (P2-21).
- Per-collection storage accounting: `document_bytes` exposed for
  quota surfaces; `chunk_bytes` admin-only (P2-47).
- Collection version tagging (P2-25).
- Formalize collection independence from tenant (P2-31).

### PaveDB v1.6 — Governance

- Tenant key management API: `POST /admin/tenants/{tenant}/keys`,
  `DELETE /admin/tenants/{tenant}/keys/{id}`. Seed `tenants.yml`
  into SQL on first boot; SQL becomes source of truth for keys
  and limits thereafter.
- OIDC/JWT as opt-in alternative auth (`auth.oidc.issuer` config);
  API keys remain permanently supported. PaveDB accepts either on
  any request.
- Tenant profiles: manifest-driven resource limits (memory,
  storage, concurrency, models available), profile templates
  (e.g. free/paid tiers); PaveDB enforces limits, does not
  handle billing or onboarding.
- Extensible ingest plugin architecture (stable plugin interface
  for custom preprocessors and future media types).
- Delete by ID list and delete by metadata query
  (single-collection scope first).
- Commit to independence principle: auth, tenant profiles,
  collections, and server config are orthogonal — no coupling
  between layers.

### PaveDB v1.7 — Maturity

- Document versioning, rebuild tooling.
- Collection migration tooling: detect version mismatches,
  provide upgrade path across PaveDB/FAISS version changes.
- Persistent metrics in the UI.

### PaveDB v1.8 — Scale

- Async ingest, parallel purge.
- Horizontal scalability, tenant groups, sub-index routing.
- Retain original uploaded files, opt-in (originals + versioning
  hooks).
- Async ingest jobs with status tracking API.
- Per-tenant parallel ingest limits.
- Matrix CI builds (Python 3.10/3.11/3.12 × core ML versions)
  as pre-2.0 compatibility gate.

### PaveDB v2.0 — Ground Truth

- Lock routes, publish final OpenAPI spec, ship SDK clients.
- Audit logs for admin actions.

### PaveDB post-2.0 backlog (no IDs yet)
- Additional media types (image, audio/video, graphic/geom,
  georeferenced) via ingest plugin architecture.
- Go client.
- Tenant job notifications (webhook/email).
- Tenant syndicates (opt-in grouping, no mandatory hierarchy).
- Multimodal collections: images, audio, and text in a shared vector space
  (cross-modal search; requires model architecture commitment).
- Vector dimension/schema guardrails.
- Soft-delete + TTL policies.
- Snapshot/backup automation.
- Index rebuild / compaction tooling.
- Filter indexes / prefilter cache.
- Drift/quality monitoring.
- Resource limits (RAM/index caps).
- Cold-start mitigation (warming hooks).
- Approx-search tuning config.

---

## Source Code Observations

### What is solid

- **Multi-tenant isolation** (`t_{tenant}/c_{collection}` layout) is clean and
well-tested. Production consumers already use it correctly.
- **Filter system** is expressive (wildcards, comparisons, datetime, negation,
OR/AND). Real consumer code exercises most filter features.
- **SQL injection prevention** (`_sanit_sql`, `_sanit_field`, `_sanit_meta_dict`)
is thorough and has dedicated tests.
- **Pluggable architecture** (BaseStore/BaseEmbedder ABCs, factory pattern) makes
it straightforward to swap backends without touching consumers.
- **Chunk text sidecar storage** guarantees text is always retrievable even when
the vector index loses content — a practical reliability win.
- **Auth policy enforcement** (`enforce_policy`) correctly prevents auth=none in
production. The loopback-only dev mode is a good guardrail.
- **Data archive/restore** with lock acquisition is operationally useful for
backup and migration.

### What needed attention (code-level) — resolved

1. ~~**`eval()` in filter matching** — replaced with `operator` module
comparisons. (done v0.5.7)~~
2. ~~**Global singleton at import time** — `build_app()` made lazy.
(done v0.5.8)~~
3. ~~**`assert` in production code** — replaced with runtime check.
(done v0.5.7)~~
4. ~~**Lock dict not thread-safe** — guarded with module-level lock.
(done v0.5.7)~~
5. ~~**Embedder factory unused** — resolved by store split
(P1-29/P1-31). Embedder factory is now the active path.
(done v0.5.9)~~
6. ~~**QdrantStore dead stub** — deleted along with `qdrant-client`
dependency. (done v0.5.9, P1-37)~~
7. ~~**Preprocess reads config at import** — chunking parameters
now passed through constructor/function args. (done v0.5.9,
P1-31)~~

---

## Market Practices (extracted from real-time decisioning benchmarks)

> PaveDB serves downstream consumers the same way a geo-bidding engine
> serves ad campaigns: both are real-time decisioning systems that must
> return the right answer fast under concurrent load. The patterns below
> are table-stakes in that domain.

### 1. Return `latency_ms` in every search response

Real-time decisioning APIs mandate `latency_ms` in every response body. PaveDB now
returns `latency_ms` (done v0.5.7).

**Why it matters:** The core metric for consumers is time saved. If PaveDB returns
`latency_ms`, consumers can log it alongside every request, giving operators concrete
data to prove and monitor value.

**Gap:** None. (done v0.5.7)

**Effort:** Low. Wrap `do_search` in `time.perf_counter()`, add field to response dict.

### 2. Define an explicit latency SLO and enforce it in CI

~~Production decisioning APIs require p99 latency SLOs.~~

**Status:** Done (v0.5.9). Benchmark suite with `--slo-p99-ms` and
`--max-error-pct` exit gates, wired into Makefile and GitLab CI
(P2-30).

### 3. Hot-reload data and configuration without restart

Production APIs require hot-reloading configuration without downtime. PaveDB's
equivalent: updating indexed data or swapping embedding models without restarting the
server.

**Current state (v0.5.9):** Document purge + re-index works live. Embedder
is constructed per-collection via factory. Chunking parameters are passed
through constructor args (no import-time config reads). Config changes
still require process restart for the embedder model.

**Action for v0.5.7:** ~~Document the live-data-update path (purge +
ingest)~~ as an explicit operational procedure. For v1.0 (per-collection
embeddings), design model hot-swap via a `/admin/reload` endpoint.

### 4. Pre-computation beats post-filtering

The equivalent of spatial indexing for geo queries: pushing filters into
the SQLite pre-filter instead of post-filtering in Python.

**Current state (v0.5.9):** `CollectionDB.filter_by_meta()` handles
exact-match and negation pushdown. Wildcards and comparisons remain in
the canonical Python post-filter (`matches_filters()`). The legacy
`_split_filters()` was removed in P1-37.

**Why it matters:** If consumers fire multiple parallel searches and
most use post-filtering, tail latency multiplies. At scale, this
becomes the bottleneck.

**Action:** Negation (`!value`) already goes to SQL (`<>`) — done
v0.5.7. Next step (P1-35) is capability-based pushdown with parity
checks against canonical post-filter semantics.

### 5. Graceful degradation under overload

Production APIs must degrade gracefully (e.g., shed low-priority work, return partial
results) rather than failing entirely under load.

~~**Status:** Done (v0.5.8). Configurable search timeout +
`max_concurrent_searches` with 503 fast-fail (P1-20). Per-tenant
rate limiting (P1-08).~~

### 6. Benchmark suite as a first-class artifact

Performance benchmarks are not optional documentation — they are proof of performance
claims and regression gates.

**Why it matters:** When optimizing PaveDB, you need a regression baseline. When
choosing between embedding models, you need comparable latency/recall numbers. When
consumers evaluate PaveDB against alternatives, benchmarks are the first thing they
look for.

~~**Status:** Done (v0.5.9). `benchmarks/search_latency.py` and
`benchmarks/stress.py` with SLO gates (P2-30). Public relevance
regression suite (P2-29).~~

### 7. Request/response traceability as contract

Distributed systems require `request_id` in both request and response. This is the
minimum contract for any service that participates in a call chain.

**Current state:** `request_id` is accepted and echoed in
responses. The v0.9 cleanup narrows the public input
contract to `X-Request-ID`.

**Action:** Remove `SearchBody.request_id`, use
`X-Request-ID` as the request-correlation input, and keep
echoing the resulting `request_id` in responses and
structured log entries. This closes the observability gap
between consumers and PaveDB.

### 8. Concurrency safety as explicit contract (not assumed)

Production APIs must handle concurrent requests correctly as a must-have, not a nice-to-
have.

~~**Status:** Done (v0.5.7). Lock registry guarded with module-level
lock. Concurrency safety hardened further in store split (P1-31,
v0.5.9).~~

### Summary: What the market expects from a real-time decisioning API

| Practice | Geo-bidding benchmark | PaveDB | Status |
|----------|----------------------|----------|--------|
| Latency in response body | `latency_ms` | Returned by search | **DONE** |
| Latency SLO + benchmarks | p99 <50ms | CI gate + SLO exit (P2-30) | **DONE** |
| Hot-reload without downtime | Hot reload | Model swap needs restart | **Partial** |
| Pre-computation / indexing | Precompute | SQLite pushdown + negation | **DONE** |
| Graceful degradation | Shed load | Search timeout + concurrency cap | **DONE** |
| Request ID propagation | `request_id` | Echoed in responses | **DONE** |
| Concurrency safety | Thread-safe | Store-level locking (P1-31) | **DONE** |
| Budget / quota governance | Quotas | Per-tenant rate limiting (P1-08) | **DONE** |

One of eight practices is still partial (hot-reload / model swap).

---

## Pluggability: PaveDB as a General-Purpose Vector Search Microservice

> Secondary priority — after the first consumer reaches GA. But
> architectural decisions made in v0.5.7–v1.0 determine whether this
> path is cheap or a rewrite.

### The landscape (as of early 2026)

There is **no standard vector store API**. Qdrant, Pinecone, Weaviate, ChromaDB, Milvus
— each has a proprietary REST API. The de facto unifying layers are:

1. **LangChain `VectorStore`** — the dominant abstraction. Implementing it
covers LangChain, LangGraph, AND CrewAI (which delegates to LangChain's VectorStore
internally). Two abstract methods: `add_texts()`, `from_texts()`. Plus
`similarity_search()`, `similarity_search_with_score()`, `delete()` for full
functionality.

2. **LlamaIndex `VectorStore`** — second framework. Different interface but
similar surface: `add()`, `delete()`, `query()`. Supports dense search and metadata
filtering.

3. **MCP (Model Context Protocol)** — NOT dead. Adopted by OpenAI (March
2025), Google DeepMind, and hundreds of tool providers. 2026 is the enterprise adoption
year. Qdrant, Pinecone, and MindsDB already ship MCP servers for vector search. MCP lets
any compatible AI agent (Claude, ChatGPT, custom) search the vector store directly — no
SDK needed on the agent side.

4. **OpenAI Vector Store API** — proprietary to OpenAI's platform
(Assistants/Retrieval). NOT a standard others implement. Implementing compatibility
would be cargo-culting with no adoption benefit.

### What PaveDB has today (v0.5.9)

| Surface | Status | Notes |
|---------|--------|-------|
| REST API (FastAPI) | **Solid** | OpenAPI spec; APIRouter modules per domain. |
| OpenAPI schema | **Solid** | Swagger UI with filtered views (search/ingest). |
| Multi-tenancy | **Solid** | `tenant/collection` namespacing — a real differentiator. |
| File preprocessing | **Unique** | CSV/PDF/TXT built-in. |
| Benchmarks + SLO | **Solid** | Latency + stress suites with CI gate (P2-30). |
| Structured logging | **Solid** | JSON ops log with request_id, tenant, latency (P2-28). |
| Python SDK (client) | **Planned** | v1.1 — `pave` package for HTTP + library mode. |
| LangChain adapter | **Planned** | v1.3 — `VectorStore` subclass. |
| LlamaIndex adapter | **Planned** | v1.3 — `VectorStore` implementation. |
| MCP server | **Planned** | v1.3 — tool exposure for AI agents. |

### What PaveDB does NOT need

- **OpenAI-compatible API** — There is no "OpenAI vector store standard"
that third parties implement. OpenAI's Vector Store API is platform-locked. Skip.

- **gRPC (short term)** — REST is sufficient for the current latency targets.
gRPC matters at >10k req/s with sub-5ms budgets. Not the current reality.

- **GraphQL** — No vector store uses it. No framework expects it. Skip.

### What PaveDB needs (in priority order)

#### 1. Python SDK — `pave` client package (~150 lines)

A thin HTTP wrapper that maps PaveDB's REST API to Python method calls.
This is the foundation everything else wraps.

```python
from pave import PaveClient

client = PaveClient("http://localhost:8086", api_key="...")
client.create_collection("tenant", "my_collection")
client.ingest("tenant", "my_collection", file_path="data.csv")
results = client.search("tenant", "my_collection", "example query", k=5)
```

**Why:** Every vector DB ships a client SDK. Without one, PaveDB integration requires
raw `httpx`/`requests` calls, which nobody does in 2026. This is table-stakes.

**Effort:** Low. ~150 lines wrapping the existing REST endpoints.

**When:** v1.1 (after API stabilizes in v1.0).

#### 2. LangChain `VectorStore` adapter (~200 lines)

Implement `langchain_core.vectorstores.VectorStore`:
- `add_texts(texts, metadatas)` → calls `POST /collections/{t}/{c}/documents`
- `similarity_search(query, k, filter)` → calls `POST /collections/{t}/{c}/search`
- `similarity_search_with_score(query, k)` → same, returns scores
- `delete(ids)` → calls document delete endpoint (needs P1-6 first)
- `from_texts(texts, embedding)` → creates collection + ingests

This single adapter covers:
- **LangChain** chains and agents
- **LangGraph** stateful agent graphs
- **CrewAI** agents and tools (delegates to LangChain VectorStore)

```python
from pave.integrations.langchain import PaveVectorStore

store = PaveVectorStore(
    client=client, tenant="my_tenant", collection="my_collection"
)
retriever = store.as_retriever(search_kwargs={"k": 5})
```

**Why:** LangChain is the dominant orchestrator. A single adapter covers three major
frameworks.

**Effort:** ~200 lines. Depends on Python SDK.

**When:** v1.3 (immediately after SDK).

#### 3. MCP server (~300 lines)

Expose PaveDB operations as MCP tools:
- `search_collection(tenant, collection, query, k, filters)` → search
- `ingest_document(tenant, collection, file_path)` → upload
- `list_collections(tenant)` → list (~~needs P2-12 first~~ done)

```json
{
  "name": "search_collection",
  "description": "Search a PaveDB collection using semantic similarity",
  "parameters": {
    "tenant": "string",
    "collection": "string",
    "query": "string",
    "k": "integer"
  }
}
```

**Why:** MCP is the standard protocol for AI agent ↔ tool communication. Qdrant,
Pinecone, MindsDB already ship MCP servers. Without one, PaveDB is invisible to the
fastest-growing integration channel. An MCP server backed by PaveDB lets any MCP-
compatible AI assistant search indexed data directly.

**Effort:** ~300 lines. MCP Python SDK is well-documented.

**When:** v1.3 (after API freeze candidates are stable).

#### 4. LlamaIndex adapter (~200 lines)

Similar to LangChain but implements LlamaIndex's `VectorStore` protocol:
- `add(nodes)` → ingest
- `delete(ref_doc_id)` → delete
- `query(query_bundle)` → search with metadata filters

**Why:** Second-largest orchestrator framework. Smaller ROI than LangChain but completes
the coverage.

**When:** v1.3.

### PaveDB's positioning in the vector DB landscape

PaveDB is not Qdrant or Pinecone. It does not compete on billion-vector scale or sub-
millisecond latency. Its niche is:

**"The SQLite of vector search"** — embed it, no cluster, no cloud, good enough for most
workloads under 10M vectors.

| | PaveDB | ChromaDB | Qdrant | Pinecone |
|---|--------|----------|--------|----------|
| Deployment | Single-process | Single-process | Docker/K8s | Managed |
| Multi-tenancy | Built-in | No | Namespaces | Namespaces |
| File preprocessing | CSV/PDF/TXT | No | No | No |
| Embedding choice | Pluggable | BYO | BYO | BYO |
| Filtering | Expressive | Basic | Rich | Basic |
| Inspectability | Query log, replay, chunk inspector | No | No | No |
| License | AGPL-3.0 | Apache-2.0 | Apache-2.0 | Proprietary |

The **built-in preprocessing**, **multi-tenancy**, and **inspectability**
are real differentiators. ChromaDB, the closest lightweight competitor,
has none of them.

### Architectural decisions that affect pluggability NOW

These are decisions in v0.5.7–v1.0 that determine whether the integration layer (v1.1+)
is cheap or expensive:

1. **Stabilize the search response schema** — If `do_search()` returns
`{matches: [{id, score, text, meta}]}` today and changes later, every adapter breaks.
Freeze the response shape in v0.5.7. Add new fields (`latency_ms`, `match_reason`,
`request_id`) now, so the schema is stable by v1.0.

2. **Document delete by ID** (P1-6) — LangChain's `delete()` method
requires this. Without it, the LangChain adapter ships incomplete.

3. **Per-collection embedding config** (v1.0) — LangChain's `from_texts()`
passes an `embedding` parameter. PaveDB must be able to accept external
embeddings OR let the caller specify which model to use per collection.
Embedder factory (v0.5.9) provides the foundation; per-collection config
wiring lands in v1.0 (P1-32).

4. ~~**List collections** (P2-12)~~ — Both LangChain and MCP need enumeration.
Status: done.

5. ~~**`BaseStore.search()` return type** — Currently returns
`List[Dict[str, Any]]`. For SDK/adapter consumption, a typed dataclass (e.g.,
`SearchResult(id, score, text, meta)`) would be cleaner. This is a v0.9 candidate.~~

### Integration roadmap

| Version | Deliverable | Depends on |
|---------|------------|------------|
| v1.1 | Python client (`pave`): HTTP + library mode | Stable REST API (v1.0) |
| v1.1 | Embedded mode, batch ingest | Python client |
| v1.3 | `pavecli --host` (remote CLI via HTTP client) | Python client |
| v1.3 | JS/TS client (npm) | OpenAPI spec (auto-generated, then typed) |
| v1.3 | LangChain `VectorStore` adapter | Python client |
| v1.3 | MCP server | Python client |
| v1.3 | LlamaIndex adapter | Python client |
| post-1.8 | Go client (Go module, generated) | Stable OpenAPI spec |
| ~~v0.9~~ | ~~Typed response models~~ | ~~API freeze~~ |
| 2.0 | Published integrations on PyPI/npm (`pave-langchain`, `pave-mcp`) | API freeze |
| post-2.0 | Rust client (WASM target) | API freeze |

### What this means for the revised version milestones

**v0.9** gains:
- Freeze search response schema (add `latency_ms`, `match_reason`, `request_id`).
- Typed return models as internal preparation.

**v1.1** gains:
- Python SDK client package.

**v1.3** gains:
- LangChain VectorStore adapter.
- MCP server.
- LlamaIndex adapter.
