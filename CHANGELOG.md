<!-- (C) 2025 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

## 0.9.4 — 2026-08-13

### Store
- Bound hot collection caches (P1-22)
- Preflight instance archive restores (P1-65)
- Reject incomplete archive collections (P1-65)
- Validate archive metadata databases (P1-65)
- Reject empty archive catalogs (P1-65)
- Reject uninitialized archive catalogs (P1-65)
- Keep archive export independent of current routing (P1-65)
- Validate archive metadata schema (P1-65)
- Validate archive metadata columns (P1-65)
- Preserve list metadata filter parity (P1-35)

### Documentation
- Repair P3-18 roadmap row (B21)
- Reconcile P1-52 roadmap status (P1-52)
- Split hybrid search roadmap work
- Close Elixir public API parity (P1-67)
- Document archive runtime requirements (P1-65)
- Record archive restore integrity (P1-65)
- Add Elixir client quick start
- Clarify SDK and client repository roles
- Add v0.9.4 release announcement

### Bug Fixes
- Preserve list-valued metadata filtering (B22)
- Reject SQLite archive sidecars (P1-65)
- Reject malformed archive restores (P1-65)
- Reject inconsistent archive indexes (P1-65)
- Reject wrong-metric archive indexes (P1-65)
- Reject archives with incomplete catalog schema (P1-65)
- Reject wrong-dimension archive indexes (P1-65)
- Reject wrong-dimension archive indexes for openai/ollama (P1-65)

### Log
- Add product-signal metrics (P1-50)
- Cache product gauge refresh (P1-50)
- Bound search metric cardinality (P1-50)
- Restore bounded search metric labels (P1-50)
- Cache query-log gauge scans (P1-50)
- Persist worker metrics snapshots (P1-50)
- Normalize worker metrics persistence (P1-50)

### Infrastructure
- Backfill and harden issue-key sync
- Add chaos failure issue triage (P1-52)
- [fix] Fail on malformed roadmap rows (B21)
- Pick the interpreter from the declared Python range
- Accept reconcile CLI mode (B23)

### Performance
- Add chaos correctness harness (P1-52)
- Finish chaos nightly invariants (P1-52)
- Accept controlled ingest shedding in chaos
- Exercise both collection move routes
- Show concurrency in stress results

### Testing
- Guard metrics endpoints from gauge refreshes (P1-50)
- Cover remaining product metric signals (P1-50)
- [infra] Chaos recipe var regression + xeon runner tag (P1-52)
- Cover incomplete archive rollback (P1-65)

### API
- Remove unused slugify helper (P1-32)
- Forward shared raw-vector queries (B24)

### Build
- Add HTTPX2 test client dependency

### CLI
- Describe shared metrics reset (P1-50)

### Plan
- Split ANN roadmap into flat/HNSW/IVF metrics (P3-54/55/56)

---
## 0.9.3.1 — 2026-08-02

### Packaging
- Ship P1-46 distribution documentation

---

## 0.9.3 — 2026-07-30

### Bug Fixes
- Return 413 for oversized ingest (R2-07)
- [core] Fix CSV column loss on blank/dup headers; collapse meta keys
- Reject invalid collection names on ingest (B2)
- Reject overlong ingest docids (B6)
- Reject non-object ingest metadata (B7)
- Fail fast on macOS direct SBERT with FAISS (B3)
- Keep catalog metrics off request path (B4)
- Accept case-insensitive Bearer scheme in static auth (B10)
- Fix dev loopback coercion in enforce_policy (B9)
- Harden auth/config startup validation (R4-70, R4-71)
- Break circular import via lazy LocalStore export (B11)
- Map encode outages to 503 (B12)
- Let filters import standalone (B11)
- Defer negated metadata filters (B14)
- Import embedder config from its owner (B11)
- Preserve BYOV vectors across restart (B13)
- Hold search slots through timeouts (B16)
- Bootstrap admin key in workers (B17)
- Preserve collection config on catalog recovery (B15)
- Guard query cache identity (R3-77)
- Keep cancelled search slots occupied (B16)
- Backfill legacy collection config sidecars (B15)

### Documentation
- Document GitLab MR workflow
- Fix README auth key (R1-01)
- Refresh compose and env examples (R1-02, R2-08, R2-09, R2-10)
- Fix embedder_label example to colon form (R4-38)
- List all reference pages in README, prettify links
- Document embedder.<type>.* global fallback keys (R4-44)
- Mark release reproducibility complete (P3-40)
- Clarify legacy env mapping was temporary (R4-37)
- Regen OpenAPI snapshot: batch 413 response (B8)
- Fix embedder and CSV docstring drift (R4-76)
- Align embedder label examples (R4-38)
- Mark rebrand plan historical
- Remove stale bench-stress findings note
- Clarify benchmark artifact storage (R6-59)
- Explain Docker volume persistence (B20)
- Correct CLI selector order (R2-79)
- Forward Docker auth key (B20)

### Core
- Register local client provider
- Share local client surface with client package
- Point local client surface at Python SDK
- Honor include_common search opt-in
- Commit schema migration explicitly (R3-22)
- Replace config in place so child views stay live (R3-15)
- Order multi-collection write locks by key, not id (R3-17)
- Close orphaned embedder worker on cache eviction (R3-16)
- Re-encode search query if embedder swapped mid-flight (R3-23)
- Validate native embedder device; fail fast on unavailable/invalid (B1)
- Fix instance path defaults (B5)
- Return not-found for vanished collection updates (R3-36)
- Support string not-equal filters (R2-74)
- Add identity (raw-vector/BYOV) embedder

### Testing
- Align CLI query listing tests (R2-06)
- Drop unused fake embeddings helper (R4-48)
- Prune redundant API coverage
- Remove leftover test noise (R4-50)
- Release timed-out search worker instead of sleeping 5s
- Remove misnamed dead tests/__init.py__ (R4-51)
- Cover close() reader-drain path (R4-52)
- Reproduce close() racing an in-flight read (R4-46)
- Drop duplicate CSV ingest API tests (R4-47)
- Trim duplicate auth status tests (R4-45)
- Cover batch metadata validation (B7)
- Cover single-doc ingest encode lock scope (R3-18)
- Assert canonical negation filtering (B14)
- Close pytest resources deterministically (R3-80)

### API
- Share current_store dependency (R3-63)
- Map update_collection not_found structurally, not by message (R3-36)
- Validate name and report not_found on delete_collection (R3-28)
- Reject metadata-only multipart ingest (R1-67)
- Reject invalid POST search k values (R2-68)
- Gate health metrics behind admin auth (R4-69)
- Enforce batch ingest size limit (B8)
- Add tenant-scoped collection move route (R3-30)
- Classify invalid document ingest errors (R1-73)
- Add raw-vector fields to ingest and search schemas (BYOV chunk 2)
- Gate BYOV: identity vectors-only, embedding dim validation (BYOV chunk 3)
- Validate BYOV dimensions (P1-64)
- Validate raw-vector document metadata (P1-64)

### Infrastructure
- Prepare release mirror state
- Add GitLab issue sync
- Remove config import fallbacks (R1-61)
- Source issue keys from commits only, not the MR description
- Drop phantom PAVEDB_CONFIG from Docker image (R4-39)
- Bench: cache HF model + 120s startup timeout on cold runners
- Sync B/F/G issue keys, not just R/P
- Auto-retry jobs on runner/system failures (max 2)
- Retry source checkout fetches
- Allow reserved review issue suffixes
- Warn on reserved review issue suffix gaps (R4-66)

### Build
- Regenerate third-party licenses from runtime closure (P3-40)
- Publish runtime pip-freeze snapshot on release tags (P3-40)
- Build only the CPU image in CI; rename torch-wheel selector (P3-40)
- Tighten requirement pins after dependency due-diligence
- [cli] Drop source-tree shell wrappers (R4-65)
- Copy pyproject metadata in Dockerfile (R4-42)
- Remove invalid Torch floor (R6-58)
- Copy project license into Docker image (R3-78)

### Store
- Drop dead bare-list handling from search output (R4-55)
- Remove unreachable async _flush_caches close path (R3-12)
- Snapshot embedder caches before orphan scan (R3-16)
- Use concrete FAISS backend in LocalStore (R4-53)
- Drop custom SearchOutput equality (R4-55)
- Release catalog guard before bootstrap (R3-13)
- Drain active readers before close (R4-52)
- Keep filtered metadata on one snapshot (R6-56)

### Config
- Parse list-valued env overrides (R2-11)
- Drop .gitignore un-ignore for nonexistent example (R4-40)
- Package favicon.ico in pave.assets (R6-57)
- Drop dead scripts/*.sh include from MANIFEST.in (R4-41)
- Align pave.toml Python version with setup.py (R4-43)
- Ignore local tenants file (R4-40)
- Reject invalid text chunk overlap (R3-75)

### Packaging
- Remove archived patchvec shim (R3-64)
- Remove qdrant stub (R1-60, R4-54)
- Replace BaseStore ABC with protocol (R2-62)
- Sanitize default docids (R2-05, R4-27)
- Move ingest size limit into service (R2-07)
- Drop unused imports stale from relocated archive logic (R4-34)
- Rename pave.toml to pyproject.toml so its tables are discovered (R4-42)

### Log
- Harden metrics persistence (R1-03, R2-04)
- Assign ops-log handle before dest in configure (R3-19)
- Drain executors before flushing metrics on shutdown (R3-24)
- Drop redundant lock around stdout dest assignment
- Split query-log writer from document writes (R3-20)

### SDK
- Scope SDK get_query/replay to tenant+collection (R3-33)
- Return the move envelope from SDK _rename (R3-32)
- Map no_text_extracted to InvalidRequest in the SDK (R3-31)
- Align local rename return contract (R3-32)
- Reach BYOV search/add through the public client (B18)

### CLI
- Reject non-object --filters early (R3-26)
- Include vector_space_key in CLI collection listing (R3-29)
- Validate collection name on create, drop auto-slug (R3-35)
- Return JSON errors for malformed options (B19)

### Performance
- Stabilize local vector stress tails
- Walk catalog metrics outside the metrics lock (R3-21)
- Remove historical benchmark dumps (R6-59)

### Plan
- Record v0.9.3 final review

### Refactor
- Unify tenant cap logic via tenant_limit_gate (R4-72)

### UI
- Refresh app icon artwork and regenerate favicon

---
## 0.9.2 — 2026-06-20

### Core
- Add batch document ingest (P1-16)
- Add JSON document ingest (P1-16)
- Single-source config schema in pave/config package (P1-53c)
- Centralize branding in version.py; promote CLI runtime flags to global
- [conf] Add embedder instance registry (P1-58)
- [fix] Stop spurious force-exit, especially under --reload
- Add embedder inventory endpoints (P1-59)

### Log
- Create log parent dirs and disable cleanly on open failure
- Show instance name in startup banner and reflow config fields
- Log store SEARCH-IN at debug level
- [fix] Route uvicorn access log to file via log_config
- Add HTTP request metrics middleware (P1-49)
- Polish startup banner and warmup logging
- Preserve public v1 paths in HTTP metrics

### Documentation
- Add generated CLI reference docs (P1-53b)
- [core] Document store protocol contract (P3-26)
- [core] Add module and class docstrings on public seams (P1-53d)(P3-26)
- [build] Add OpenAPI contract snapshot (P1-53e)

### Config
- Tweak production-ready defaults (static auth, file logs)
- Rely on schema defaults for config reads
- Standardize config access calls

### API
- [doc] Add OpenAPI reference metadata (P1-53a)
- Add embedded/library mode client (P1-15)

### Build
- Wire dev serve flags for workers, reload, and instance name
- Recreate stale PaveDB venvs

### Performance
- Refactor benchmark server config, restore bench model
- Cover batch document ingest endpoint (P2-48)

### CLI
- Add parser metadata for CLI reference (P1-53b)

### UI
- Migrate served template to centralized branding tokens

---
## 0.9.1 — 2026-06-15

### Core
- Move embedder specs onto embedder modules (P1-32)
- Convention-based embedder dispatch (P3-49)
- Add OllamaEmbedder as a thin OpenAI-compatible wrapper for a local Ollama dae...
- Replace startup warmup with store.warm() behind a config knob (P3-48)
- Align typing policy with SDK boundaries (P1-57)

### Store
- [fix] Treat concurrent-delete race in search as 404
- [fix] Stop persisting embedder defaults that bust the load cache
- Remove _system tenant carve-outs
- [fix] Keep health probes off the per-collection lock walk
- Hoist query embedding out of the collection read lock

### API
- Add collection model specs and move/update APIs (P1-32)
- Return 503 for unavailable embedders (P1-32)

### Performance
- Cover collection update and move APIs (P1-32)

---
## 0.9.0 — 2026-06-11

### API
- Get document by ID endpoint (P1-17)
- Per-collection embedder params in create-collection (P1-32 prep)
- Add ok field to search response (P1-14)
- Add response models for CRUD endpoints (P1-14)
- Mount API routes under /v1/ prefix (P1-43)
- Expose timing breakdown in search response (P1-40)
- Add X-Request-ID middleware (P1-11)
- Echo request_id and latency_ms in all JSON response bodies (P1-11)
- List documents endpoint (P1-44)
- Enrich list collections response (P1-45)
- Get collection detail endpoint (P1-45)
- Log search queries + collection-scoped query log endpoints (P1-41)
- Reject search-body request_id; use X-Request-ID (P1-48)
- Query replay endpoint (P1-42)
- Admin query-home resolver + shortcut routes + CLI rework (P1-51)
- [fix] make global searches go through the ops_event annotation and generate o...
- Add actor field to ops_log events (P2-46)
- [cli] Add chunk inspector endpoints and commands (P2-23)
- [bench] Surface search query IDs for stress replay (P2-48)

### Store
- Add CatalogDB catalog store (P1-33)
- Wire CatalogDB into LocalStore (P1-33)
- Return SearchOutput with per-phase timing (P1-40)
- Persist query logs in per-collection meta.db (P1-41)
- Enrich query_log with historical scope and actor (P2-46)
- Consolidate pre-1.0 SQLite schemas
- [fix] Avoid collection reopen races after cache flush
- [fix] Avoid CatalogDB reopen races after cache flush
- [fix] Wait for SQLite writers before closing DB handles
- [fix] Serialize archive restore with store ops (P1-52)
- [fix] Keep search hydration under collection lock (P1-52)
- [fix] Serialize SQLite read connections (P1-52)
- [fix] Replace documents atomically during ingest (P1-52)
- Allow concurrent same-collection reads (P1-52)
- [fix] Make catalog updates atomic (P1-52)
- [fix] Return 409 for rename target collisions (P1-52)
- [fix] Pin cached collection reads during races (P1-52)
- [fix] Reopen vector backend after collection rename (P1-52)

### Performance
- [fix] Disambiguate bench-stress coverage-pass misses
- [fix] Preserve ephemeral server logs on benchmark failure
- [fix] Fail when managed benchmark server exits (P2-48)
- Add critical/full stress suites (P2-48)
- Auto-discover stress ops for full suite (P2-48)
- Warn on stress coverage gaps and race outcomes (P2-48)
- Add stress ops for remaining OpenAPI endpoints (P2-48)
- Add bench-stress-full target (P2-48)

### Build
- Defer release commit until local validation passes
- Keep version in one file
- [fix] Avoid eager embedder loads in build-check (P3-52)
- [fix] Force BuildKit on docker build
- [fix] Use validated bash for make recipes
- Make release builds opt-in and name dev artifacts

### UI
- Align docs UI with PaveDB theme
- Regroup API browser tabs and tag groups
- Add API browser tab deep links
- Add contextual hints to API browser tabs
- Simplify API browser wording and chrome

### CLI
- Add query log inspection commands (P1-41)
- Add collection and document inspection commands (P1-44 P1-45)
- Add collection config args to create-collection (P1-45)
- Add explicit common-merge search flag

### Core
- [fix] Normalize _do_search error results
- [fix] Cross-check scoped query lookup against query_home
- [fix] Isolate SBERT from server process on macOS (P3-26)
- [fix] Prefer current SBERT dimension API (P3-26)

### Documentation
- Update roadmap and plan docs
- Document store concurrency model (P1-52)
- [fix] Default to non-cpu quick startup

### Testing
- CatalogDB integration + upgrade tests (P1-33)
- Make LocalStore tests use temp_data_dir explicitly
- Consolidate collection API tests into one file

### Log
- Improve server failure diagnostics (P1-52)
- Clarify concurrency cap diagnostics (P1-20)

### Infrastructure
- Rename GitLab CI config file

### Packaging
- Finalize PaveDB rebrand (P3-35)

---
## 0.9 series — Rebrand to PaveDB

### Breaking changes
- `PATCHVEC_*` env vars are no longer recognized; use `PAVEDB_*`
- Default instance home is `~/pavedb/` instead of `~/patchvec/`
- Docker image renamed to `pavedb`
- Published registry path is now
  `registry.gitlab.com/flowlexi/pavedb/pavedb:*`

---
## 0.5.9 — 2026-04-03

### Store
- CollectionDB read-only open for fallback reads
- Close abandoned CollectionDBs on cache flush
- Extract vector backend seam (P1-29a)
- Clean VectorBackend protocol, add FaissBackend, activate embedder path (P1-29b)
- Adapt SbertEmbedder to Embedder protocol (P1-29b)
- Add QdrantVectorBackend stub (P1-29b)
- [fix] validate FAISS index dimension on load
- [fix] harden chunk/archive I/O against TOCTOU races
- Add chunk_meta CollectionDB storage (P1-29c)
- Add exact-negation CollectionDB pushdown (P1-29c)
- Sanitize metadata keys and values before inserting to chunk metadata table
- Split document/chunk metadata storage and pushdown (P1-29c)
- Remove dead txtai backend and legacy methods (P1-37)
- Simplify filter path — pass normed_filters directly to filter_by_meta (P1-37)
- Drop txtai dependency, promote SbertEmbedder to default (P1-37)
- Rename TxtaiStore → FaissStore, simplify file names, reshape provider-scoped...
- Fix filter_by_meta mixed-OR key narrowing incorrect results
- Reject invalid sanitized metadata keys (P1-36)
- Extract filters and sanitization to pave/filters.py (P1-31)
- LocalStore orchestrator replaces FaissStore (P1-31)

### Build
- Fix make install-dev output under .ONESHELL
- Update make check to use faiss config
- Add make build-check target (P3-52)
- Include openai extra in Docker image
- Add make docker-check target (P3-51)
- Gate release on smoke checks
- Rename build/CI identifiers and benchmark labels to pavedb (P3-35)

### Documentation
- Add third-party license audit and dependency policy
- Add Step 2 benchmark results to `docs/PLAN-STORE.md` (before/after FAISS cuto...
- Refresh docs for faiss/sbert naming (P1-37)
- Clarify collection logical-vs-physical mapping
- Remove unsupported qdrant runtime mode references
- Add inspectability/control layer to roadmap

### Performance
- Implement variant filtering latency modes, with fresh model per variant run a...
- Latency results before and after metadata k,v table implementation, several f...
- Benchmarks after txtai removal
- Add --slo-p99-ms exit gate to search_latency (P2-30)
- Add --max-error-pct exit gate to stress (P2-30)
- Wire SLO flags into Makefile targets (P2-30)

### Config
- Fix tenants sidecar precedence and dev config loading
- Temporarily map supported legacy `PATCHVEC_*` env vars to `PAVEDB_*`
  during the phase-1 migration; removed in the 0.9 rebrand series (P3-35)
- [fix] isolate default config state across instances

### Service
- Add error logging at all ok:false service layer return sites (P2-40)
- [fix] wrap create_collection and health probe in collection_lock
- [fix] reorder archive restore and lock exclusion, rewrite data_export tests (...

### Testing
- Drop unsafe skipped concurrent upsert race case
- Rename test_faiss_* → test_store_* (P1-31)
- Add opt-in public relevance regression checks (P2-29)

### Tests
- FaissBackend and TxtaiEmbedder tests.
- Cover chunk_meta CollectionDB storage (P1-29c)
- Cover pushdown edge cases (P1-29c)

### API
- Split main.py routes into APIRouter modules (P3-50)

### Bug Fixes
- Harden store lifecycle races found by bench-stress

### CLI
- [conf] add explicit instance bootstrap and runtime paths (P1-34)

### Core
- Rename operator-visible identifiers to pavedb (P3-35)

### Deps
- Package sbert runtime and relevance deps

### Infrastructure
- Add benchmark CI job with SLO gate (P2-30)

---
## 0.5.8.1 — 2026-03-07

### API
- [cli] add list-tenants and list-collections endpoints and commands
- Ensure latency_ms in all /search returns (incl. common disabled)
- Normalize error envelope and make document delete idempotent
- Reject uploads exceeding configurable size limit (default 500 MB)
- Search timeout + concurrency cap (P1-20)
- Ingest concurrency cap (P1-20 follow-up)
- Per-tenant concurrent request cap (P1-08)
- Expose server HW info in /health/metrics

### Documentation
- Add coding style, commit message standards to developer docs
- Revamp ROADMAP structure, priorities, and release ordering
- Config.yml.example full docs pass
- [bench] baseline benchmark results before SQLite metadata store
- [perf] Phase 1 benchmark results + impl comparison (winner:impl2)
- Plan large scale store refactor

### Performance
- Rebalance stress weights; error tracking in both scripts
- Improve benchmark scripts, make targets, and docs
- Improve benchmark resilience: retry on failed seeds and add --debug mode
- Run header, unified table format, sample results; tune stress duration/concur...
- Stress: coverage pass for ops not picked during timed phase
- Fix op_delete_collection: remove from world only after success

### Store
- Migrate legacy txtai indexes missing documents/objects/sections tables
- Phase 1 SQLite per-collection meta.db — impl2 (read/write split conn)
- [tests] fix has_doc cache race and add regression test
- [search] scope metadata fetch to top-k when no post-filter
- Store-backed catalog metrics (P1-21) + validation hardening
- Accept optional doc_meta and pass it explicitly to SQLite upsert

### Core
- Add collection rename functionality across all layers
- Standardize naming across API, CLI, and service layers
- Add SearchResult dataclass for type-safe search results
- Lazy build_app(): app only initialised on first access
- Replace  key with character  in TXT preprocessor (P2-41)

### Log
- Dev stream cleanup (P2-28)
- Ops stream: pave/log.py, ops_event decorator, 8 routes (P2-28)
- Merge diverging log level configs, set defaults, surface log.level as source...
- Migrate dev stream from config.py to log.py; ~ expansion for log paths
- Add top result excerpt to search log line

### Build
- Make release re-run safety, docker-build/push USE_CPU parity
- Fix docker-build/push error propagation on sub-make failure
- [docs] overhaul Makefile release flow and project docs

### Config
- Ingest timeout guidance: expose timeout_keep_alive, nginx proxy hints
- Complete _DEFAULTS, ~ path expansion, fix Makefile env vars
- Default path ~/patchvec/config.yml; expand PATCHVEC_CONFIG too

### Testing
- Speed up suite 5x: inject FakeEmbeddings for non-slow tests
- [store] fix Python 3.12 sqlite3 DeprecationWarning; silence SwigPy noise

### Infrastructure
- Remove deploy jobs, add docker RC build, fix tag patterns

### Packaging
- Changelog generation goes back to last tag in changelog itself not in git his...

### Service
- Fix _flush_store_caches: drop refs, do not close() connections

---
## 0.5.7 — 2026-02-21

### Store
- Ensure CRLF-rich documents round-trip intact
- Sanitize txtai metadata persistence (closes #3)
- Prevent infinite recursion with deeply nested collections
- Push !-prefixed filters into SQL pre-filter instead of post-filter, reducing...
- Change default embedding model to multilingual and add multilingual cross lan...
- Build a match_reason as part of the search return contract
- Fixed race condition in get_lock() with double-checked locking pattern using...
- Replace eval() with operator module in filter matching
- Replace assert with runtime check in index_records
- Fix collection_lock usage across all TxtaiStore methods
- [fix] Disable meta-device loading so that Pooling.to() works on PyTorch>=2.6...

### Documentation
- Refresh workflows and roadmap
- Revise roadmap based on technical evaluation
- [test] Document and test the live-data-update path (purge + ingest)
- Added benchmark suite documentation
- Update short description (ABOUT.md) to reflect recent workflow changes

### Build
- Push torch cpu requirement to 2.8+
- Add cpu/gpu extras with proper torch wheel selection and python version. Clos...
- Fix assets package and license definitions in setup.py
- Consolidate deps into setup.py, single source of truth

### Core
- Add feature to dump entire datastore as .zip file (api and cli)
- Add feature to push/restore data archive from zip backup (api and cli)
- [metrics] Persist metric across app resets and add metrics reset cli utility...
- Pretty terminal logging with colors and level-based filtering

### API
- Add latency histograms (p50/p95/p99)** on  for search and ingest
- Structured request logging with request_id, added latency and request_id to s...
- [cli] Add delete document by ID endpoint and command

### Performance
- Add concurrent stress test exercising all supported API operations
- Eagerly load embedding model at server startup

### Bug Fixes
- Atomic writes, coalesced metrics flush, Makefile benchmark targets

### Config
- Implement initial/decent multilevel logging support

### Infrastructure
- Add benchmarks/ directory with search latency load test

### Packaging
- Revamp the automated changelog generation script

### Chores
- Update ROADMAP.md
- Update to Python 3.10+ type hint syntax
- Update copyright notices to 2026, add missing copyright headers
- Use Python 3.10+ typing syntax; warn on corrupt index
- Small fix in README.md to make docs consistent with docker image naming scheme
- Update project urls

---
## 0.5.6 — 2025-10-29

### Core
- Added ingestion timestamps to document metadata and improved CSV ingestion controls
  (headers, meta columns and include lists).
- Hardened API boot by forcing string-based Uvicorn startup and gating document purges
  behind `has_doc` checks.
- Normalized service entrypoint configuration, including stricter binding and
  authentication safeguards.
- Standardized request metrics emission and activated service-level telemetry across the
  API.

### Store
- Prevented FAISS index overwrites on multi-document ingests and ensured index
  directories are created eagerly.
- Improved FAISS store concurrency through SQL filtering hooks, stronger locking, and
  thread-safe helpers.
- Guaranteed text chunks are persisted and hydrated when vector content retrieval falls
  back to storage.
- Ensured `txtai_store` consistently returns search text results.

### Build & Packaging
- Added dedicated Makefile targets for local deployment, e2e checks (still needs work)
  and dependency cleanup.
- Enabled the Docker build pipeline with split GPU/CPU flows, refined image tagging, and
  updated startup scripts.
- Extended release automation with PyPI publishing support, GitLab pipeline steps, and
  tuned Makefile/setup.py metadata.
- Updated dependency sets, including explicit `faiss-cpu` support and auxiliary tooling
  definitions in `pave.toml`.

### Config
- Introduced multilevel logging defaults and refreshed the example embedding model to a
  multilingual preset.
- Expanded configuration backend coverage with additional tests.

### UI
- Added a lightweight Swagger/OpenAPI UI with branding, authorization helpers, and
  contextual headers/footers.

### Testing
- Simplified store mocks by pinning default embedding models and cleaning up legacy
  FAISS test shims.

### Misc
- Updated project metadata, copyright headers, and ignore lists.
- Advanced version markers for intermediate dev builds and release tags (0.5.5 →
  0.5.6devN).

---
## 0.5.5 — 2025-09-02

### Core
- Added CSV ingestion configuration knobs (headers, meta columns, include filters).
- Implemented default document ID handling to overwrite vectors deterministically on re-
  ingest.
- Fixed authentication edge cases and expanded accompanying tests.
- Ensured request metrics are emitted consistently across the API surface.

### Build & Packaging
- Added an end-to-end Makefile target and improved startup scripts with dependency
  cleanup steps.
- Extended release automation with Docker targets and a PyPI publishing flow.
- Refined dependency management by bundling `faiss-cpu` for CPU builds and `sqlite4` for
  testing.

### Store
- Made FAISS-backed collections initialize their index structure on creation.
- Corrected the txtai store path so search responses always include original text
  payloads.

### Commits
- [buid] Add e2e check target and cleanup Makefile
- [build] Enhance startup scripts, add dependency clean Makefile target
- [core] Add CSV ingestion options: headers (yes|no), meta_cols and include_cols
- [core] Add default docid behavior so that vectors are seamlessly overriden when same
  file is ingested even if no docid is provided
- [core] Fixed txtai_store to handle indexes correctly and always return search text.
- [core] Fixing auth and adding tests
- [core] Normalize entry point config and add binding and auth safeguards for prod envs
- [core] Standardize request metrics in API and enable service metrics in service
  pipeline
- [pkg] Add docker targets and make further adjustments do Makefile
- [pkg] Add pypi publish makefile target
- [pkg] Fix dependencies: add sqlite4 to testing and explicitly add faiss-cpu to cpu-
  only target
- [store] Make sure FAISS indexes and dir structure are initialized upon collection
  creation.
- Fix .gitlab-ci.yml file

---
## 0.5.4 — 2025-08-12

### Feat
- initial public release of PatchVec — multi-tenant, pluggable vector search
  microservice

---
## 0.5.3 — 2025-08-12

### Config
- Introduced minimal `.env.example` (required vars only; `PATCHVEC_` scheme)
- Clarified tenants secrets via untracked `tenants.yml`

### Packaging
- Added Makefile-based release flow (tests gate release; Docker/compose versions bumped
  automatically)
- Added Gitlab & Github CI/CD workflows (not tested)

### Docs
- Split README into **README.md** (end-user) and **CONTRIBUTING.md** (dev)
- Added REST examples with `curl`
- Documented uvicorn server overrides via `HOST`, `PORT`, `RELOAD`, `WORKERS`,
  `LOG_LEVEL`
- Trimmed Quickstart and added PyPI install path

---
## 0.5.2 — 2025-08-12

### Testing
- Added CSV and PDF ingestion/search
- Adjusted test cases for FastAPI’s stricter body/query validation.

### Arch
- Added `TxtaiEmbedder` as default; added `OpenAIEmbedder` and `SbertEmbedder`.

### API
- Fixed `POST /search` route to accept `SearchBody` via JSON body.

### Other
- Added `docker-composer` stub

---
## 0.5.1 — 2025-08-11

### Testing
- Added comprehensive pytest test suite covering:
- Collection creation/deletion
- Document ingestion & search (TXT)
- Re-ingestion with purge
- Expanded pytest coverage for TXT ingestion, re-ingestion, and search.
- Fixed relative import issues in tests.

### Arch
- Refactored store and embedder factories to use Python 3.10+ `match` syntax.
- Standardized naming (`*_store`, `*_emb`).

### Auth
- Refactored authentication/authorization into `auth_ctx()` and `authorize_tenant()`
  using FastAPI dependency injection.
- Authorization now automatically derives tenant from bearer token when applicable.

### API
- Unified GET and POST search behavior.

---
## 0.5 — 2025-08-11

### Testing
- Added `DummyStore` for testing.

### Arch
- Isolated vector store interfaces via ABC (`BaseStore`) for plug-in stores (Qdrant,
  FAISS, etc.).
- Added `StoreFactory` and `EmbedderFactory` with runtime store/embedder selection
  (pluggable backends).

### API
- Added `/health` endpoint with general metrics and alive status

---
## 0.4 — 2025-08-10
- Modularized codebase:
- `stores/` for vector store backends.
- `embedders/` for embedding backends.
- `auth.py` for authentication/authorization implementation.
- `service.py` for main logic implementation (abstracted from api and cli interfaces).
- `cli.py` for cli implementation.
- `main.py` for endpoint routing and default initialization.
- `preprocess.py` for file ingestion helpers.
- `metrics.py` for metrics implementation.

---
## 0.3 — 2025-08-09
- Added QdrantStore skeleton (methods unimplemented).
- Added `OpenAIEmbedder` proof of concept (not tested).
- Introduced `CFG` for unified cfg management.
- Added complete cli mode

---
## 0.2 — 2025-08-08
- Implemented multi-tenant routing (`/{tenant}/{collection}`).
- Added basic static authentication via global or per-tenant API keys.
- Added document ingestion and collection management endpoints.

---
## 0.1 — 2025-08-07
- First working prototype with:
- FastAPI service with search endpoint.
- FAISS store and Sbel embeddings.
- Command-line TXT ingestion and REST `search` endpoint.
- Single-tenant mode.
- Minimal auth stub.
