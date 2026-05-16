<!-- (C) 2025 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

## 0.9.0 — TBD

### Core
- Complete the public rebrand to PaveDB

### Breaking changes
- `PATCHVEC_*` env vars are no longer recognized; use `PAVEDB_*`
- Default instance home is `~/pavedb/` instead of `~/patchvec/`
- Docker image renamed to `pavedb`
- Published registry path is now
  `registry.gitlab.com/flowlexi/pavedb/pavedb:*`

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
- Map supported legacy PATCHVEC_* env vars to PAVEDB_* (P3-35)
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
