<!-- (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Roadmap

tl;dr: This roadmap tracks production readiness and integration milestones. To claim a
task, open an issue titled `claim: <task ID>` and link your branch or PR.
Detailed design plans live under `docs/design/` and are indexed after the task tables.

> PaveDB is an inspectable retrieval database: text searches keep their source
> provenance, query record, and replay trail, while one interface carries an
> embedded prototype into self-hosted or managed production. The roadmap is
> driven by production readiness for its first downstream consumer — an
> application that maps natural-language queries to structured codes via
> semantic search in a non-English language. The core product metric is
> **time saved to reach the correct results**. Every TODO is evaluated against
> that metric and general production readiness.

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
- **Collections are version-safe.** From v0.9.5 onward, supported SQLite schemas migrate
  forward without data loss. Newer schemas reject downgrades; incompatible reads fail
  loudly with actionable guidance rather than silently corrupting data.
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
| P1-07 | ~~Hybrid reranking~~ | 🧱 | Exact token boost inside the vector candidate window | v1.0 |
| P1-08 | ~~Per-tenant rate limiting~~ |  | Abuse protection | v0.5.8 |
| P1-09 | ~~Metadata store (SQLite)~~ | 🧱 | ACID + concurrency | v0.5.8 |
| P1-11 | ~~Global `request_id` echo~~ | 🧩 | Traceability | v0.9 |
| P1-12 | ~~Ingest timeout guidance~~ | 🧩 | Avoid client timeouts | v0.5.8 |
| P1-13 | ~~Ingest size limits~~ | 🧩 | Fail fast on huge uploads | v0.5.8 |
| P1-14 | ~~Response envelope standardization~~ | 🧱 | SDK-friendly API | v0.9 |
| P1-15 | ~~Embedded/library mode~~ | 🧱 | In-app use, adoption. Handle-based `Client`/`Collection` over `service.py`; `pave.connect()` dispatches local/ephemeral/HTTP by scheme; raises typed `PaveError`. Canonical interface every transport implements. See [`docs/design/PLAN-CLIENT.md`](docs/design/PLAN-CLIENT.md) | v1.0 |
| P1-16 | ~~JSON + batch document ingest~~ | 🧩 | Throughput, DX. `POST /documents` accepts multipart files or JSON text documents; `POST /documents:batch` ingests a list of JSON text documents. Python exposes `add_many`; CLI exposes `ingest-text` and `ingest-batch`. Multi-file batch waits for async jobs. | v1.1 |
| P1-17 | ~~Get document by ID~~ | 🧩 | Visibility, library mode | v0.9 |
| P1-18 | ~~Error code standardization~~ | 🧩 | Consistent API errors | v0.5.8 |
| P1-19 | ~~`build_app()` lazy init~~ | 🧩 | Testability, startup safety | v0.5.8 |
| P1-20 | ~~Search timeout + concurrency cap~~ | 🔧 | Graceful degradation | v0.5.8 |
| P1-21 | ~~Serve listings + store catalog counts in health/metrics~~ | 🧩 | Internal store query layer | v0.5.8 |
| P1-22 | ~~Per-collection hot caches~~ | 🧱 | Performance isolation — per-collection LRU-bounded backend/db/embedder caches with safe non-blocking eviction; pulled into v1.0 | v1.0 |
| P1-23 | ~~Freeze search response schema~~ | 🧩 | SDK contract; `query_id` added additively after freeze | v0.9 |
| P1-24 | ~~Python SDK package~~ | 🧱 | SDK foundation; v1.0 now includes `HttpClient` parity with `LocalClient`. Distribution target: `pavedb-sdk` package; `pavedb` provides optional local provider. | v1.0 |
| P1-25 | ~~Dev vs prod config defaults~~ | 🔧 | Safe defaults — code defaults are prod-safe (`auth.mode=none` refuses to boot outside dev); dev overlay lives in the `make serve` env | v1.0 |
| P1-26 | ~~Config reference + CI doc check~~ | 🧩 | Config clarity | v1.0 |
| P1-27 | ~~Admin key auto-generate + persist~~ | 🧩 | Secure bootstrap | v1.1 |
| P1-28 | ~~Moving-window rate limiting per tenant~~ | 🔧 | req/min, req/hour — persistent `rate_limit_buckets` state in the catalog | v1.0 |
| P1-29 | ~~VectorBackend protocol~~ | 🔧 | Initial backend seam for store split | v0.5.9 |
| P1-29b | ~~Clean protocol + Faiss path~~ | 🔧 | Finish FAISS cutover | v0.5.9 |
| P1-29c | ~~CollectionDB k/v pre-filter~~ | 🔧 | First pushdown stage | v0.5.9 |
| P1-30 | ~~Activate embedder factory cache (superseded by P1-29b)~~ |  | Superseded by Step 2 in PLAN-STORE | superseded |
| P1-31 | ~~Store orchestrator~~ | 🧱 | Orchestrate backend + meta + catalog | v0.5.9 |
| P1-32 | ~~Per-collection embeddings~~ | 🧱 | Model per collection | v1.0 |
| P1-33 | ~~CatalogDB + catalog separation~~ | 🧱 | Catalog + collection backend/embedder config source | v0.9 |
| P1-34 | ~~Server config bootstrap~~ | 🧩 | Explicit `pavecli init` + `--home` / explicit runtime paths for pip installs | v1.0 |
| P1-35 | ~~Metadata-filter SQL parity~~ | 🔧 | CI-only differential harness: SQL never drops canonical matches and equals canonical results for supported operators | v1.2 |
| P1-36 | ~~Reject empty/colliding sanitized metadata keys~~ | 🧩 | Avoid silent key drops/coalescing under current sanitization rules | v0.5.9 |
| P1-37 | ~~Pre-orchestrator cleanup~~ | 🔧 | Drop txtai dep, rename store/embedder, dead code removal, filter path simplification | v0.5.9 |
| P1-40 | ~~Search timing breakdown in response~~ | 🔧 | Latency debugging: embed/search/filter/hydrate split | v0.9 |
| P1-41 | ~~Persistent query log~~ | 🔧 | Queryable search history (query, filters, config, result IDs) in SQLite | v0.9 |
| P1-42 | ~~Query replay endpoint~~ | 🧩 | Re-execute stored query; depends on P1-41 | v0.9 |
| P1-43 | ~~`/v1/` route prefix~~ | 🔧 | Versioned API base path; frozen at v1.0, additive after | v0.9 |
| P1-46 | ~~Distribution documentation~~ | 🔧 | Ship public guides, generated references, OpenAPI, release/legal material, and a CI-verified documentation manifest in the PaveDB source distribution. | v1.0 |
| P1-47 | ~~Docs site renderer~~ | 🧱 | `pavedb-site` renders the released PaveDB and SDK documentation with Hugo; product sources and generators stay in their owning distributions. | v1.0 |
| P1-48 | ~~Remove `SearchBody.request_id`~~ | 🧩 | `X-Request-ID` becomes the only request-correlation input | v0.9 |
| P1-49 | ~~HTTP request metrics middleware~~ |  | v1.0 ships labeled `http_requests_total` middleware + handler-side `inc("requests_total")` cleanup; histogram + `# HELP`/`# TYPE` metadata ride P1-50 at v1.1 | v1.0 / v1.1 |
| P1-50 | ~~Product-signal metrics + metrics polish~~ | 🧩 | Zero-match searches, filter usage, query-log readiness, embedder counters, sidecar drift — competitor gap. Bundled with P1-49's histogram + HELP/TYPE refactor and docs-site coverage so the new gauges are discoverable | v1.0 |
| P1-51 | ~~Admin query-home resolver + shortcut routes + CLI rework~~ | 🧩 | Bare-`query_id` lookup/replay for admin via `query_home` table in `catalog.db` | v0.9 |
| P1-52 | ~~Concurrency chaos test (Level 1)~~ | 🔧 | N-thread random-interleave fuzz over create/delete/move/ingest/search/dump/restore with post-run catalog↔disk invariants. Regression net for the lock lattice hardened during the v0.9 rc cycle; bench-stress finds races, this turns them into non-flaky CI checks. Runs as a nightly job (not a v1.0 release gate). | v1.0 |
| P1-53a | ~~Reference-source plumbing: OpenAPI completeness pass~~ | 🔧 | Every route gets `summary`/`description`/`tags`/`responses`; every Pydantic `Field` gets `description` + at least one example. Unblocks an auto-rendered API reference. | v1.0 |
| P1-53b | ~~Reference-source plumbing: CLI completeness + dumper~~ | 🔧 | Every subcommand gets `help`/`epilog`-example; `scripts/cli_to_markdown.py` renders `docs/reference/cli.md` committed in-repo. | v1.0 |
| P1-53c | ~~Reference-source plumbing: config schema refactor~~ | 🔧 | Single (key, default, description, env_var) source for `_DEFAULTS`, `config.yml.example`, and `docs/reference/config.md`. Closes P1-26 (drift check) as a side effect. | v1.0 |
| P1-53d | ~~Reference-source plumbing: module/class docstrings on public seams~~ | 🔧 | `pave/stores`, `pave/backends`, `pave/embedders`, catalog, and `pave/service.py` public functions provide source documentation for supported seams. | v1.0 |
| P1-53e | ~~API contract snapshot + drift check~~ | 🔧 | Commit `openapi.json` as a checked-in snapshot; `docs-ref-openapi` dump + drift check (mirrors P1-53b's CLI gate) enforces the P1-43 "frozen at v1.0, additive-after" contract — breaking changes fail CI. Depends on P1-53a. | v1.0 |
| P1-54 | ~~Per-collection archive (dump + restore)~~ | 🧱 | Admin-only `GET`/`POST` archive endpoints for a single collection. Carries `manifest.json` (PaveDB version, embedder spec, schema version), `meta.db`, FAISS index, sidecars. Restore must verify embedder spec is resolvable locally before touching disk. Same-version restore only at v1.0; cross-version waits on P3-37. Depends on P2-25. | v1.0 |
| P1-55 | ~~Per-collection reindex~~ | 🧱 | Admin-only operation: rebuild vectors against a new embedder spec. Needs a job model (interruptible, resumable, status-trackable). Splits cleanly from PATCH (cheap label edit) and `move` (slug/path move + disk move) because it's the only one that rewrites stored vectors. See `docs/design/PLAN-REINDEX.md`. | v1.0 |
| P1-56 | ~~Chroma comparison benchmark~~ | 🔧 | Optional local compare bench for PaveDB vs Chroma using the same corpus, same sentence-transformers model, comparable create/upsert/delete/search operations, exact metadata filters only, and consolidated latency/stress output. Keep Chroma deps off the default install/test path. | v1.0 |
| P1-57 | ~~Typing policy + SDK-ready type vocabulary~~ | 🧩 | Boundary-focused typing rules, named internal JSON vocabulary, and lighter test annotations without duplicating API/client schemas | v1.0 |
| P1-58 | ~~Embedder instance registry core~~ | 🧩 | Registry-shaped config with keyed instances, `embedder.default` as instance/type/type:model/vector-key selector, useful CRITICAL failures for missing or ambiguous defaults, deterministic first compatible runtime instance, and catalog persistence limited to vector-space identity. Depends on P1-32 (done) | v1.0 |
| P1-59 | ~~Admin embedder inventory endpoint~~ | 🧩 | v1.0 slice: admin-only `GET /v1/admin/embedders` and tenant-scoped `GET /v1/embedders/{tenant}` surface configured instance keys, vector-space keys, types, models, and default resolution. No credentials, no readiness probe. Mirrored by `Client.embedders()` and `pavecli list-embedders`. v1.6 expansion: readiness probe + credential-ref identity per P2-49. Per-tenant embedder assignment stays a post-1.0 direction once tenant profiles (P2-26) land. Builds on P1-58. | v1.0 / v1.6 |
| P1-60 | ~~Embedder selection shorthand + routing policy~~ | 🧱 | Collection create accepts an `embedder` selector shorthand; compatible instance routing grows from deterministic first configured to explicit policy (`first`, `round_robin`, weighted/failover). Any pinning is operational config, not catalog state. | v1.0 |
| P1-61 | ~~General embedder worker pools~~ | 🧱 | Extend the SBERT worker-pool idea across all embedders with bounded concurrency as the default behavior where safe. Worker counts stay operational config and never enter vector-space identity. See [`docs/design/PLAN-EMBEDDER-WORKERS.md`](docs/design/PLAN-EMBEDDER-WORKERS.md). | v1.0 |
| P1-62 | ~~Python API reference~~ | 🔧 | `pavedb-sdk` generates a drift-checked public API reference shipped in its sdist. Runnable examples split to P1-66. See [`docs/design/PLAN-EXAMPLES.md`](docs/design/PLAN-EXAMPLES.md). | v1.0 |
| P1-63 | ~~VectorDBBench adapter~~ | 🔧 | Standard benchmark adapter over BYOV HTTP raw-vector ingest/search, with flat FAISS recall baseline and no PaveDB runtime dependency on VectorDBBench. External adapter (separate repo/fork), not in-repo. Depends on P1-64 (BYOV raw-vector round trip). | v1.0 |
| P1-64 | ~~Bring-your-own-vector (BYOV) raw-vector mode~~ | 🔧 | Additive raw-vector ingest/search mirroring Chroma. New `identity` embedder (`encode(x)→x`, dim from `embedder_config`); raw-vector schema fields `vector`/`v` paired with `text`/`q` (exactly-one). Embedding collections stay permissive (text or raw vector); identity collections are vector-only. No new endpoints; FAISS backend unchanged. Serves pre-computed-embedding users and unblocks P1-63. | v1.0 |
| P1-65 | ~~Instance archive/restore integrity~~ | 🔧 | Preflight catalog, collection sidecars, and configured embedders before rollback-safe whole-instance replacement; verify mixed-embedder round trips. | v1.0 |
| P1-66 | ~~Runnable book examples~~ | 🔧 | The Python SDK and Elixir client own tested, numbered top-level examples extracted from the book; CI verifies runnable programs and distribution boundaries. Landed as numbered `examples/` in `pavedb-sdk` and `pavedb-client-elixir`. See [`docs/design/PLAN-EXAMPLES.md`](docs/design/PLAN-EXAMPLES.md). | v1.1 |
| P1-67 | ~~Elixir public API parity~~ | 🔧 | Cover the current user-facing `/v1` surface plus a basic `/health` connection check; defer `/admin`, `/metrics`, and `/embedders`. | v1.0 |
| P1-68 | ~~Task-oriented engine guides~~ | 🔧 | Hand-written, low-boilerplate quickstart and usage guides shipped in the PaveDB source distribution, complementing the generated reference (P1-46) with narrative task-oriented docs rather than auto-generated dumps. | v1.1 |
| P1-70 | ~~SQLite chunk-text index and content filters~~ | 🧱 | Make chunk text queryable through native FTS5 for contains/phrase/prefix filters and lexical candidates; prerequisite for P1-72 | v1.0 |
| P1-71 | ~~Backend-aware filtered vector search~~ | 🧱 | Let SQLite-selected record IDs constrain vector search instead of relying on repeated vector-first overfetch; depends on P1-35 | v1.0 |
| P1-72 | ~~Hybrid lexical/vector retrieval~~ | 🧱 | Fuse independent FAISS and SQLite FTS5 candidate lists; depends on P1-70 | v1.0 |
| P1-73 | ~~Preview SQLite compatibility baseline~~ | 🔧 | Transactional forward migrations from v0.9.5; reject downgrades and pre-baseline schemas | v0.9.5 |
| P1-74 | ~~Sbert embedder options: `trust_remote_code`, task prefixes (space keys), `runtime=remote` (TEI)~~ | 🔧 | Unlocks custom-architecture local models and GPU offload with an unchanged `vector_space_key`; additive config, frozen protocol untouched. | v1.1 |
| P1-75 | ~~Exclusive local data-directory ownership~~ | 🧩 | Fail closed before a second server, local client, or offline CLI opens the same canonical data directory | v0.9.6 |

### P2 — Enables enterprise use cases and competitive moat

| ID | Task | Effort | Why it matters | Source |
|---|---|---|---|---|
| P2-11 | `meta.priority` boosts |  | Surface priority items | v1.2 |
| P2-12 | ~~List tenants/collections API~~ | 🧩 | Ops visibility | v1.0 |
| P2-13 | Collection log export | 🧱 | Search analytics | v1.4 |
| P2-14 | Document versioning | 🧱 | Audit trails | v1.7 |
| P2-19 | Tenant admin infra | 🧱 | Admin ops | v1.5 |
| P2-20 | ~~Collection limit / tenant~~ | 🧩 | Billing-hard growth cap | v0.9.5 |
| P2-21 | Storage limit / tenant | 🧩 | Cap storage | v1.5 |
| P2-22 | Usage stats to mothership |  | Capacity planning | v1.4 |
| P2-23 | ~~Chunk inspector + collection browser~~ | 🔧 | List chunks, get chunk by ID, browse doc→chunk tree | v0.9 |
| P2-24 | Delete by ID list / by query | 🧩 | Bulk ops, DX | v1.6 |
| P2-25 | ~~Collection version tagging~~ | 🧩 | Delivered in three parts: live collections gained schema-version tagging with loud incompatible-read failures in the v0.9.5 migration baseline (P1-73); the catalog stamps `pavedb_version`/`collection_schema_version` on every new or restored collection and the collection detail response reports them; archives carry enforced `pavedb_version`/`schema_version` tags in the P1-54 manifest. Cross-version restore remains P3-37. | v1.0 |
| P2-26 | Tenant profiles + templates | 🧱 | Quota governance, tiers | v1.6 |
| P2-28 | ~~Structured log emission~~ | 🧩 | JSON lines per operation with request_id, tenant, latency | v0.5.8 |
| P2-29 | ~~Public cross-language retrieval fixtures~~ | 🧩 | Recall validation | v0.5.9 |
| P2-30 | ~~Benchmark CI gate + p99 SLO~~ | 🧩 | Latency contract | v0.5.9 |
| P2-31 | Formalize collection independence | 🔧 | Portability contract | v1.5 |
| P2-32 | Remote CLI usability evaluation | 🔧 | Multisite Cloud management by agents and orchestrators | v1.1 |
| P2-33 | JS/TS client | 🧱 | Web + Node adoption | v1.3 |
| P2-34 | LangChain adapter | 🧱 | Framework coverage | v1.3 |
| P2-35 | MCP server | 🧱 | AI agent integration | v1.3 |
| P2-36 | LlamaIndex adapter | 🧱 | Framework coverage | v1.3 |
| P2-37 | Audit logs for admin actions | 🧩 | Governance trail | v1.6 |
| P2-38 | Tenant key management API | 🔧 | Generate/revoke keys, YAML seed → SQL | v1.5 |
| P2-39 | Structured log retention | 🔧 | Rolling window + purge via `operation_log` (SQLite Phase 3); powers P2-13 | v1.2 |
| P2-40 | ~~Error logging at service layer~~ | 🧩 | `log.warning` on every `ok: false` return site; audit codes and choose level per error class | v0.5.9 |
| P2-41 | ~~TXT preprocessor: character offset in chunk metadata~~ | 🧩 | Provenance contract (replace `chunk` key with `offset`) | v0.5.9 |
| P2-42 | Result diff API | 🔧 | Compare two search runs: added/removed/reordered | v1.2 |
| P2-43 | Eval assertion API | 🧱 | Define expected results, batch run, track pass/fail | v1.2 |
| P2-44 | Regression detection | 🔧 | Compare eval runs across versions, flag drift; depends on P2-43 | v1.2 |
| P2-45 | Per-query execution-context stamp | 🔧 | Stamp corpus/space/config drift axes per query; powers exact-vs-drifted replay & diff; prerequisite for P2-42/P2-44 | v1.2 |
| P2-46 | ~~query_log + ops_log enrichment~~ | 🧩 | Historical `tenant` / `collection` / `actor` columns; `actor` field in ops_log | v0.9 |
| P2-47 | Per-collection storage accounting | 🔧 | Separate document/chunk/index/meta bytes; expose `document_bytes` for quotas, keep `chunk_bytes` admin-only | v1.5 |
| P2-48 | ~~`bench-stress-full` profile~~ | 🔧 | Off-by-default stress run covering every public endpoint; landed early as `STR_SUITE=critical/full` with `@_covers` decorator + OpenAPI startup gap warning | v0.9 |
| P2-49 | Embedder credential refs | 🔧 | Multi-key embedder runtimes without storing or echoing raw secrets in collection config; credentials resolve from server config/secret store and affect runtime cache identity, not vector-space compatibility | v1.6 |
| P2-50 | ~~Ollama embedder~~ | 🧩 | Thin OpenAI-compatible wrapper for the local Ollama daemon — default base_url `http://localhost:11434/v1`, daemon-up probe → `embedder_unavailable`, sane embedding-model defaults. Lowers adoption bar for devs already running Ollama for LLM workflows | v1.0 |
| P2-51 | Flat cosine/IP/L2 metrics | 🔧 | Exact metrics; VectorDBBench gate. | v1.1 |
| P2-52 | Optional FAISS HNSW index | 🧱 | HNSW; recall/QPS gates. | v1.2 |
| P2-53 | Optional FAISS IVF index | 🧱 | IVF plus explicit optimize. | v1.8 |
| P2-54 | ~~Chunk limit / collection~~ | 🧩 | Billing-hard row cap for shared plans; with P2-20 bounds tenant growth | v0.9.5 |
| P2-55 | Cross-tenant read grants per collection | 🔧 | An owner tenant grants search on one of its collections to named tenants, turning tenant↔collection into n:n for reads. Generalizes the single shared-corpus pair (`common_tenant`/`common_collection`) into a grant table, reusing the existing merge path and its embedder-compatibility check. Read-only by design: the owner keeps every write, because cross-tenant writes would move quota accounting, catalog ownership and query-log attribution. Replay resolves grants from live state, never from the logged query | v1.2 |
| P2-56 | Bound archive-dump disk materialization (collection + instance) | 🔧 | P1-54 follow-up covering both dump paths: the per-collection dump and the whole-instance `GET /v1/admin/archive` materialize the full zip on disk and serve it via `FileResponse`, so concurrent dumps are capped only by free disk in a multi-tenant deployment. Remedy is an open design choice: stream the archive or bound concurrent materializations. Both endpoints are admin-key gated, which is why this defers to v1.5 while the attacker-supplied restore-side disk risk (B89) gates 1.0 | v1.5 |

### P3 — Scale and long-term

| ID | Task | Effort | Source |
|---|---|---|---|
| P3-15 | Async ingest + parallel purge |  | v1.8 |
| P3-16 | Horizontal scalability + routing | 🧱 | v1.8 |
| P3-17 | OIDC/JWT auth (additive; API keys remain) | 🧱 | v1.6 |
| P3-18 | Final API freeze + SDK clients | 🧱 | v2.0 |
| P3-23 | ~~Docs website (superseded by P1-46/P1-47)~~ | 🧩 | v1.1–v1.2 |
| P3-24 | Revamp UI | 🧱 | v1.4 |
| P3-25 | Multilingual UI/errors/docs | 🧱 | v1.4 |
| P3-26 | ~~Embedder/store contract~~ | 🧱 | v1.0 |
| P3-28 | Extensible ingest plugin architecture | 🧱 | v1.6 |
| P3-30 | Retain original uploaded files (opt-in) + content inspector | 🧱 | v1.8 |
| P3-31 | Async ingest jobs + job status API | 🧱 | v1.8 |
| P3-32 | Per-tenant parallel ingest limits | 🧱 | v1.8 |
| P3-34 | ~~Relicensing (AGPLv3 candidate)~~ | 🧱 | v0.5.9 |
| P3-35 | ~~Rebranding phase 1 (PaveDB candidate); phase 2~~ | 🧱 | v0.5.9–v0.9 |
| P3-36 | Multimodal collections (cross-modal search) | 🧱 | post-1.0 |
| P3-37 | Collection migration tooling (version compat) | 🧱 | v1.7 |
| P3-40 | ~~Publish pip freeze snapshot + regenerate THIRD_PARTY_LICENSES~~ | 🧩 | v1.0 |
| P3-41 | Swagger UI tenant/collection defaults | 🧩 | v1.4 |
| P3-42 | Alive test in CI | 🧩 | v1.4 |
| P3-43 | Go client | 🧱 | post-1.8 |
| P3-44 | Persistent metrics in UI | 🧩 | v1.7 |
| P3-45 | Independence principle audit | 🔧 | v1.6 |
| P3-46 | Matrix CI builds | 🧱 | v1.8 |
| P3-47 | Additional media types | 🧱 | post-1.0 |
| P3-48 | Explicit embedder warmup policy (v1.0: ~~`warmup_default` bool~~ done; v1.7: enum + per-embedder switch + warmup-by-key list, depends on P1-58 registry) | 🧩 | v1.0 / v1.7 |
| P3-49 | ~~Convention-based embedder dispatch~~ | 🔧 | v1.0 |
| P3-50 | ~~Split main.py routes into APIRouter modules (health, admin, collections, documents, search)~~ | 🧩 | v0.5.9 |
| P3-51 | ~~`make docker-check`: alive test against prebuilt Docker image~~ | 🧩 | v0.5.9 |
| P3-52 | ~~`make build-check`: install from local wheel in temp venv, alive test~~ | 🧩 | v0.5.9 |
| P3-53 | Micro-batch concurrent query embeddings | 🧱 | v1.8 |
| P3-54 | Deployment, operations, and replay documentation update | 🧩 | v1.0 |

P1-53e includes a checked-in `openapi.json` snapshot plus a
`docs-ref-openapi` dump and drift check, mirroring P1-53b's CLI gate.
Any unintended public API change should fail CI after the freeze; the
current spec is complete as of P1-53a.

---

## Plan Docs

Substantial features are specified under `docs/design/` before implementation.

[docs-refresh]: docs/design/PLAN-DOCS.md#v10-adoption-documentation-p3-54

| File | Plan ID | Feature |
|---|---|---|
| [Docs refresh][docs-refresh] | P3-54 | Deployment, operations, replay |
| [`docs/design/PLAN-OPS-LOG.md`](docs/design/PLAN-OPS-LOG.md) | P2-28 | Structured log emission — ops JSON stream |
| [`docs/design/PLAN-CLIENT.md`](docs/design/PLAN-CLIENT.md) | P1-15 / P1-24 / P1-75 / P2-32 | Local and HTTP clients, local ownership, and remote CLI evaluation |
| [`docs/design/PLAN-SQLITE.md`](docs/design/PLAN-SQLITE.md) | P1-09 / P1-33 / P1-54 / P1-73 / P2-25 | SQLite metadata, catalog, migrations, and collection portability |
| [`docs/design/PLAN-METRICS-PERSISTENCE.md`](docs/design/PLAN-METRICS-PERSISTENCE.md) | P1-50 | Worker-safe metrics |
| [`docs/design/PLAN-STORE.md`](docs/design/PLAN-STORE.md) | P1-29/P1-29b/P1-29c/P1-31/P1-32 | Store split; P1-30 superseded by P1-29b |
| [`docs/design/PLAN-OBSERVABILITY.md`](docs/design/PLAN-OBSERVABILITY.md) | P0-03, P1-11/14/17/23/40/41/42/43/44/45/48, P2-23/28/40/41/45 | Inspectability surface — request correlation, timing, query log, replay, chunk inspector, execution-context stamp |
| [`docs/design/PLAN-DOCS.md`](docs/design/PLAN-DOCS.md) | P1-46 / P1-47 / P1-53 / P1-62 / P1-66 | Distribution documentation and site-input contract |
| [`docs/design/PLAN-EXAMPLES.md`](docs/design/PLAN-EXAMPLES.md) | P1-62 / P1-66 | SDK Python API reference and runnable book examples |
| [`docs/design/PLAN-EMBEDDER-WORKERS.md`](docs/design/PLAN-EMBEDDER-WORKERS.md) | P1-61 | General embedder worker pools |
| [`docs/design/PLAN-REINDEX.md`](docs/design/PLAN-REINDEX.md) | P1-55 | Durable per-collection vector rebuild jobs |
| [`docs/design/PLAN-HYBRID-SEARCH.md`](docs/design/PLAN-HYBRID-SEARCH.md) | P1-35 / P1-70 / P1-71 / P1-72 | Metadata parity, SQLite text search, hybrid retrieval, and true vector prefiltering |
| [PLAN-ANN](docs/design/PLAN-ANN.md) | P2-51 / P2-52 / P2-53 | FAISS indexes |
| [`docs/design/PLAN-EXTRA-BENCHMARKS.md`](docs/design/PLAN-EXTRA-BENCHMARKS.md) | P1-63 | VectorDBBench adapter shipped outside core |

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
  from the internal store; runtime op counter deltas are periodically merged into
  the catalog database.~~
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
- ~~Freeze search response schema (`matches`, `latency_ms`,
  `match_reason`, `request_id`); `query_id` added additively
  to enable replay from clients without scanning the log
  (P1-23).~~
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
- ~~Chunk inspector + collection browser: list chunks, get chunk
  by ID (text + metadata + provenance), doc→chunk tree (P2-23).~~
- ~~Mount all routes under `/v1/` prefix; drop unversioned routes
  (no compat shim pre-GA). Contract: frozen at v1.0, additive
  only after (new endpoints, optional fields). `/v2/` introduced
  only if a `/v1/` shape must break (P1-43).~~
- ~~Rebranding phase 2: public-facing rename, env fallback removal,
  and `patchvec` → `pavedb` redirect/shim path (P3-35).~~
- ~~Remove `SearchBody.request_id`; `X-Request-ID` becomes the
  single documented input channel for request correlation
  (P1-48).~~
- ~~`bench-stress-full` profile: STR_SUITE=critical/full tiers
  with `@_covers` decorator + OpenAPI coverage warning;
  auto-discovery picks up any new `op_*` without touching
  `bench-stress.py`. Race-outcome reclassification keeps real
  5xx visible (P2-48).~~

### PaveDB v0.9.3 — Final contract preview

- Freeze features after the current hardening batch. Run one final full review
  against the merged candidate and accept only release-critical corrections:
  data loss/corruption, security/auth, crash/recovery, installation/packaging,
  licensing, or documented destructive behavior.
- Keep the v1.1/v1.2 feature branches parked. P1-46, P1-47, and P1-62 start
  after the v0.9.3 tag and do not expand this release.
- Ship the current runtime as a final pre-1.0 preview. The supported contract
  is declared only when the distribution docs, Python API reference, examples,
  and rendered site land together at v1.0.

### PaveDB v0.9.6 — 1.0 preview hardening

- ~~Give each canonical local data directory one supported local owner; a
  second server, local client, or offline CLI fails before opening the store
  (P1-75).~~

### PaveDB v1.0 — Supported contract

- ~~Define embedder/store separation contract (P3-26).~~
- ~~Activate embedder factory cache (P1-30; superseded by P1-29b).~~
- ~~Per-collection embeddings + `display_name` + admin move/update
  APIs; `vector_space_key` surfaced on detail/listing (P1-32).~~
- ~~Embedded/library mode: `import pave; db = pave.connect("./data")`
  — handle-based `Client`/`Collection` over the service layer, no
  server required. Lowers the install friction bar (a few lines to
  first search) without sacrificing the inspectability surface.
  The library API is the canonical interface the v1.0 Python HTTP
  client mirrors 1:1. Design in
  [`docs/design/PLAN-CLIENT.md`](docs/design/PLAN-CLIENT.md)
  (P1-15).~~
- ~~Python SDK package: `pavedb-sdk` distribution. It provides
  the shared API and `HttpClient`; the `pavedb` package depends on
  it and registers the optional `LocalClient` provider so the same
  interface works locally when `pavedb` is installed (P1-24).~~
- Reference-source plumbing pass:
  - ~~OpenAPI completeness: every route gets summary/description/
    tags/responses; every Pydantic Field gets description + at
    least one example. Unblocks an auto-rendered API reference
    (P1-53a).~~
  - ~~CLI completeness + dumper: every subcommand gets help and
    an epilog example; `scripts/cli_to_markdown.py` renders
    `docs/reference/cli.md` committed in-repo (P1-53b).~~
  - ~~Config schema refactor: single (key, default, description,
    env_var) source feeds `_DEFAULTS`, `config.yml.example`, and
    `docs/reference/config.md`. Closes the P1-26 drift check as a
    side effect (P1-53c).~~
  - ~~Module/class docstrings on public seams: `pave/stores`,
    `pave/backends`, `pave/embedders`, catalog, and
    `pave/service.py` public functions document supported seams
    at their source (P1-53d).~~
  - ~~API contract snapshot: commit `openapi.json` and add a dump +
    drift check that enforces the P1-43 frozen-at-1.0,
    additive-after contract — breaking changes fail CI (P1-53e).~~
- ~~Distribution documentation: package public guides, generated
  CLI/config/protocol references, OpenAPI, release/legal material,
  and a checked manifest in the PaveDB sdist. Wheels keep only
  runtime resources (P1-46).~~
- ~~Python API reference: `pavedb-sdk` generates a committed,
  drift-checked public-symbol reference and ships it in its sdist,
  not its wheel (P1-62).~~
- ~~Docs site: `pavedb-site` consumes the selected PaveDB and SDK
  source distributions and renders them with Hugo. It owns layout,
  navigation, links, and deployment—not product facts (P1-47).~~
- ~~Admin/tenant embedder inventory (P1-59 v1.0 slice): admin-only
  `GET /v1/admin/embedders` and tenant-scoped
  `GET /v1/embedders/{tenant}` list configured instance
  keys, vector-space keys, types, models, and default resolution
  (no credentials, no readiness probe). Mirrored by
  `Client.embedders()` so the library mode and the v1.0 HTTP
  client answer the same question through one handle method,
  plus `pavecli list-embedders`.~~
- ~~Instance archive restore integrity: validate catalog/sidecar parity
  and local embedder resolution before rollback-safe replacement;
  cover mixed-embedder round trips (P1-65).~~
- ~~Collection version tagging for safe archive restore — covered by
  the v0.9.5 schema-version baseline (P1-73), the catalog's
  `pavedb_version`/`collection_schema_version` stamp on new and
  restored collections, and the archive manifest's enforced
  `pavedb_version`/`schema_version` tags (P1-54) (P2-25).~~
- ~~Per-collection archive (dump + same-version restore): admin
  endpoints carrying `manifest.json` + `meta.db` + FAISS index +
  sidecars. Embedder spec validated locally before restore touches
  disk. Cross-version restore waits on P3-37 migration tooling
  (P1-54).~~
- Deployment and production guidance, collection portability, and query-log
  comparison after replay; link the full Python SDK example (P3-54).
- ~~Dev vs prod config defaults: code defaults are prod-safe, the dev
  convenience overlay lives in the Makefile/env, enforced by
  `enforce_policy` fail-fast (P1-25).~~
- ~~Explicit config bootstrap for pip installs (`pavecli init`,
  `--home`, explicit runtime paths) (P1-34).~~
- ~~Config reference doc + CI drift check — lands as a side effect
  of P1-53c (P1-26).~~
- ~~Publish `pip freeze` snapshot as release artifact; regenerate
  `THIRD_PARTY_LICENSES.md` from the runtime-only closure (drops stale
  dev-venv cruft) and keep `*.dist-info` in the Docker image (P3-40).~~
- ~~List tenants and collections via API (CLI parity) (P2-12).~~
- ~~HTTP request metrics middleware (v1.0 slice): single middleware
  records per-endpoint count labeled by method, path template, and
  status class. Removes the 20+ per-handler `inc("requests_total")`
  call sites. Histogram + `# HELP`/`# TYPE` metadata refactor
  bundles with P1-50 at v1.1 (P1-49).~~
- ~~Chroma comparison benchmark: optional local bench, off the
  default dependency path, comparing PaveDB and Chroma on the same
  corpus/model and comparable create/upsert/delete/search workload.
  Use exact metadata filters only; wildcard metadata filters remain
  PaveDB-specific unless a true Chroma-equivalent operator exists
  (P1-56).~~
- ~~Typing policy + SDK-ready type vocabulary: document gradual typing
  rules, name internal JSON-like store/service shapes, keep embedder
  config ownership in `pave.embedders`, and leave future Python client
  types to OpenAPI/Pydantic/SDK-specific request-response contracts
  (P1-57).~~
- ~~Convention-based embedder dispatch: factory becomes a thin
  `importlib` dispatcher; each embedder module exports
  `build_embedder(cfg, **overrides)` and its own `CONFIG_KEYS` /
  `SPACE_KEYS` tuples. New embedders drop in with zero factory
  edits. Lands before P2-50 so OllamaEmbedder ships as the first
  validation of the abstraction (P3-49).~~
- ~~Ollama embedder: thin OpenAI-compatible wrapper for the local
  Ollama daemon. Defaults `base_url=http://localhost:11434/v1`,
  `api_key="ollama"`, sensible default embedding model;
  daemon-up probe at construction raises `embedder_unavailable`
  with a clear "daemon not reachable" message. Lowers adoption
  friction for the local-LLM dev loop (P2-50).~~
- ~~Default-embedder warmup knob: top-level boolean
  `embedder.warmup_default` (default `null`: production warms, dev
  stays lazy) drives default-embedder warmup at server lifespan
  startup. SBERT process mode prewarms every configured worker so
  startup cost does not land on first traffic. Restores v0.9.0
  behavior after the lazy refactor and fail-fast checks API
  key/network for openai-class embedders.
  Per-embedder warmup switches arrive at v1.7 alongside the
  multi-embedder registry (P1-58) (P3-48 v1.0 slice).~~

**Deferred from v1.0** (to lean the path; pulled in by next
release, see v1.1 and v1.2 sections): P1-27 admin key auto-gen,
P1-35 filter pushdown parity harness, P1-49 histogram polish.
None blocks the documented-contract promise that defines v1.0; each
lands where it has more impact during post-contract hardening.

### PaveDB v1.1 — Adoption

- Exact flat cosine, inner-product, and L2 metrics selected per collection;
  the VectorDBBench adapter expands beyond its v1.0 cosine cases (P2-51).
- ~~Runnable book examples owned by the Python SDK and Elixir client
  distributions (P1-66).~~
- ~~Task-oriented quickstart and usage guides in the PaveDB source
  distribution (P1-68).~~
- ~~Batch ingest endpoint (list of documents in one call) (P1-16).~~
- ~~Admin key auto-generate + persist on first boot; CLI rotate /
  show; mode-`0600` enforced (P1-27).~~
- ~~HTTP metrics polish + product-signal metrics: histogram refactor
  with `# HELP`/`# TYPE` metadata (deferred P1-49 slice) plus
  zero-match search rate, filter usage, common-merge count, search
  timeout count, query-log rows + oldest-entry age, embedder
  counters, sidecar drift gauges. Update the existing docs when
  these signals land (P1-49 / P1-50).~~
- ~~Concurrency chaos test (Level 1) lands as a nightly GitLab job
  (not a release gate). N-thread random-interleave fuzz over
  create/delete/move/ingest/search/dump/restore with post-run
  catalog↔disk invariants. Regression net for the lock lattice
  hardened during the v0.9 rc cycle (P1-52).~~
- ~~Sbert embedder options: `trust_remote_code` for custom-architecture
  models, e5-style task prefixes that join the vector-space key, and
  `runtime=remote` delegation to a TEI-compatible endpoint serving the
  identical model (P1-74).~~
- Evaluate remote CLI usability for Cloud multisite management by agents and
  orchestrators. One invocation targets one HTTP instance through the SDK;
  fleet policy remains outside PaveDB (P2-32).

### PaveDB v1.2 — Control

- Optional FAISS HNSW index using the same metric-aware collection config;
  recall and throughput are gated by VectorDBBench (P2-52).
- ~~General embedder worker pools with bounded concurrency across providers
  (P1-61).~~
- Cross-tenant read grants: an owner tenant shares search on one of its
  collections with named tenants, generalizing the single shared-corpus
  pair into a grant table. Reads only; writes stay with the owner (P2-55).
- ~~Metadata-filter SQL parity: a CI-only differential corpus proves
  canonical matches are a subset of SQL survivors, with equality
  where SQL claims full support. No request-time double execution
  (P1-35).~~
- ~~SQLite chunk-text index and additive content filters: migrate
  sidecar text into the collection database, index it with native
  FTS5, and expose contains/phrase/prefix matching (P1-70).~~
- Honor `meta.priority` boosts during scoring (P2-11).
- ~~Add exact token boost inside the vector candidate window (P1-07).~~
- ~~Add hybrid retrieval by fusing independent FAISS and FTS5
  candidate lists; depends on P1-70 (P1-72).~~
- Result diff API: compare two search runs — added/removed/reordered
  hits (P2-42).
- Eval assertion API: define expected results per query, batch run,
  track pass/fail over time (P2-43).
- Regression detection: compare eval runs across versions, flag
  drift (P2-44).
- Per-query execution-context stamp: corpus/space/config drift axes
  on each query; extends P1-42 replay with an exact/drifted/unknown
  verdict (P2-45).
- Structured log retention: rolling window + purge via
  `operation_log` table (SQLite Phase 3); powers P2-13 (P2-39).
- ~~Moving-window per-tenant rate limiting (req/min, req/hour) via
  persistent catalog `rate_limit_buckets` state (P1-28).~~

### PaveDB v1.3 — Reach

- ~~Backend-aware filtered vector search using SQLite-selected record IDs;
  depends on P1-35 (P1-71).~~
- JavaScript/TypeScript client: typed, bootstrapped from OpenAPI
  spec, published to npm. Covers web frontends and Node.js backends
  (P2-33).
- LangChain `VectorStore` + `Retriever` adapter (covers LangGraph
  + CrewAI) (P2-34).
- MCP server (expose search/ingest/list as MCP tools) (P2-35).
- LlamaIndex `VectorStore` adapter (P2-36).

### PaveDB v1.4 — Polish

- Default tenant/collection selectors in Swagger UI (P3-41).
- Collection-level structured log export for analytics (P2-13).
- Alive test in CI pipeline (post-deploy health check) (P3-42).
- Revamp UI (P3-24).
- Multilingual UI/errors/docs (P3-25).
- Usage stats to mothership (opt-in/anon) (P2-22).

### PaveDB v1.5 — Lifecycle

- Tenant admin infrastructure (P2-19).
- Per-tenant storage limit (P2-21).
- Per-collection storage accounting: `document_bytes` exposed for
  quota surfaces; `chunk_bytes` admin-only (P2-47).
- Formalize collection independence from tenant (P2-31).
- Tenant key management API: `POST /admin/tenants/{tenant}/keys`,
  `DELETE /admin/tenants/{tenant}/keys/{id}`. Seed `tenants.yml`
  into SQL on first boot; SQL becomes source of truth for keys
  and limits thereafter. Lands with tenant admin infra (P2-19)
  as the foundation the v1.6 governance items build on (P2-38).
- Bound archive-dump disk materialization on both admin dump paths
  (collection + instance): stream the archive or cap concurrent
  materializations (P2-56).

### PaveDB v1.6 — Governance

- OIDC/JWT as opt-in alternative auth (`auth.oidc.issuer` config);
  API keys remain permanently supported. PaveDB accepts either on
  any request (P3-17).
- Tenant profiles: manifest-driven resource limits (memory,
  storage, concurrency, models available), profile templates
  (e.g. free/paid tiers); PaveDB enforces limits, does not
  handle billing or onboarding (P2-26).
- Embedder credential refs: allow multiple runtime instances for
  the same provider/model without storing or returning raw keys in
  collection config. Credential identity participates in the
  embedder cache key, not the vector-space key (P2-49).
- Extensible ingest plugin architecture (stable plugin interface
  for custom preprocessors and future media types) (P3-28).
- ~~Per-collection reindex: admin job that rebuilds vectors against
  a new embedder spec. Job model is interruptible and
  status-trackable — splits cleanly from PATCH (label edit) and
  `move` (slug/path move) since it's the only operation that rewrites
  stored vectors (P1-55).~~
- Delete by ID list and delete by metadata query
  (single-collection scope first) (P2-24).
- Commit to independence principle: auth, tenant profiles,
  collections, and server config are orthogonal — no coupling
  between layers (P3-45).
- Audit logs for admin actions: governance trail pairing with
  key management and OIDC (P2-37).

### PaveDB v1.7 — Maturity

- ~~Embedder selector shorthand and routing policies (P1-60).~~
- Document versioning, rebuild tooling (P2-14).
- Collection migration tooling: detect version mismatches,
  provide upgrade path across PaveDB/FAISS version changes
  (P3-37).
- Persistent metrics in the UI (P3-44).
- Explicit embedder warmup policy — enum expansion: `embedder.warmup`
  accepts `default` / `none` / `all` / list-of-keys. Adds per-embedder
  `warmup` switch under each spec in the v1.6 multi-embedder registry
  (P1-58). Per-instance `warmup` overrides the global/default policy
  when present. v1.0's `warmup_default` boolean remains as the canonical
  default-only knob (P3-48).

### PaveDB v1.8 — Scale

- Optional FAISS IVF index with explicit train/build state and a public
  collection-optimize operation used by operators and VectorDBBench (P2-53).
- Async ingest, parallel purge (P3-15).
- Horizontal scalability, tenant groups, sub-index routing
  (P3-16).
- Retain original uploaded files, opt-in (originals + versioning
  hooks) (P3-30).
- Async ingest jobs with status tracking API (P3-31).
- Per-tenant parallel ingest limits (P3-32).
- Micro-batch concurrent query embeddings: coalesce in-flight search
  encodes that share a `vector_space_key` into batched `encode()` calls,
  lifting the embed-CPU throughput ceiling that caps concurrent search
  latency under load (flat p50≈p99 saturation observed at concurrency
  ~40). A request-coalescing layer in front of the embedder seam; lands
  in the Scale release since it is a throughput optimization, not a GA
  blocker, and groups batches by embedder identity so it depends on the
  multi-embedder registry (P1-58, v1.6) (P3-53).
- Matrix CI builds (Python 3.10/3.11/3.12 × core ML versions)
  as pre-2.0 compatibility gate (P3-46).

### PaveDB v2.0 — Ground Truth

- Lock routes, publish final OpenAPI spec, ship SDK clients
  (P3-18).

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
- **Pluggable architecture** (BaseStore ABC, Embedder/VectorBackend protocols,
factory pattern) makes it straightforward to swap backends without touching
consumers.
- **Canonical SQLite chunk text** keeps text retrievable when the vector index
  loses content and makes the text projection independently rebuildable.
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

**Current state:** `CollectionDB.filter_by_meta()` selects exact-match
candidates before vector search when the backend implements the optional
filtered-search capability. Wildcards, comparisons, and negation retain
canonical Python evaluation (`matches_filters()`). The legacy
`_split_filters()` was removed in P1-37.

**Why it matters:** SQL-selected IDs now constrain FAISS work for selective
filters instead of repeatedly expanding a vector-first candidate window.
Backends without the capability keep that correct fallback.

**Action:** P1-35 proves capability-based SQL filtering against canonical
semantics in CI. P1-71 lets SQLite-selected IDs constrain vector search
itself. See PLAN-HYBRID-SEARCH.

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

## Pluggability: one retrieval core, multiple interfaces

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

PaveDB is not Qdrant or Pinecone. It does not compete on billion-vector scale
or sub-millisecond latency. Its niche is retrieval that stays inspectable and
under the operator's control:

**"Retrieval you can inspect. Infrastructure you can own."** Start embedded,
self-host the same engine, or use it managed without changing the API or
archive format.

| | PaveDB | ChromaDB | Qdrant | Pinecone |
|---|--------|----------|--------|----------|
| Deployment | Embedded, self-hosted, managed | Single-process | Docker/K8s | Managed |
| Multi-tenancy | Built-in | No | Namespaces | Namespaces |
| File preprocessing | CSV/PDF/TXT | No | No | No |
| Embedding choice | Pluggable | BYO | BYO | BYO |
| Filtering | Expressive | Basic | Rich | Basic |
| Inspectability | Query log, replay, chunk inspector | No | No | No |
| License | AGPL-3.0 | Apache-2.0 | Apache-2.0 | Proprietary |

The **provenance and replay trail**, **owned deployment path**, **built-in
preprocessing**, and **multi-tenancy** are the differentiators. ChromaDB, the
closest lightweight competitor, does not combine them.

### Architectural decisions that affect pluggability NOW

These are decisions in v0.5.7–v1.0 that determine whether the integration layer (v1.1+)
is cheap or expensive:

1. **Stabilize the search response schema** — If `do_search()` returns
`{matches: [{id, score, text, meta}]}` today and changes later, every adapter breaks.
Freeze the response shape in v0.5.7. Add new fields (`latency_ms`, `match_reason`,
`request_id`) now, so the schema is stable by v1.0.

2. **Document delete by ID** (P1-6) — LangChain's `delete()` method
requires this. Without it, the LangChain adapter ships incomplete.

3. ~~**Per-collection embedding config** (v1.0)~~ — LangChain's `from_texts()`
passes an `embedding` parameter. PaveDB must be able to accept external
embeddings OR let the caller specify which model to use per collection.
Embedder factory (v0.5.9) provides the foundation; per-collection config
wiring lands in v1.0 (P1-32). Status: done for model/config selection.

4. ~~**List collections** (P2-12)~~ — Both LangChain and MCP need enumeration.
Status: done.

5. ~~**`BaseStore.search()` return type** — Currently returns
`List[Dict[str, Any]]`. For SDK/adapter consumption, a typed dataclass (e.g.,
`SearchResult(id, score, text, meta)`) would be cleaner. This is a v0.9 candidate.~~

### Integration roadmap

| Version | Deliverable | Depends on |
|---------|------------|------------|
| v1.0 | Python SDK (`pavedb-sdk`): HTTP + optional local provider | Stable REST API (v1.0) |
| v1.1 | Embedded mode, batch ingest | Python client |
| v1.1 | Evaluate remote CLI for multisite automation | Python client + Cloud management use cases |
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

## Cloud Offering (hosted PaveDB) — infra decisions (2026-08-15)

PaveDB stays "no cluster, no cloud" as *software*. The hosted offering wraps
that single-process model per tenant rather than replacing it with a shared
cluster — same deployment shape, someone else runs it.

Multisite agents and orchestrators motivate an early remote CLI usability
evaluation (P2-32). Each invocation would target one explicit HTTP instance;
fleet scheduling, credential distribution, and failover policy remain outside
PaveDB.

### Control/data plane: Fly.io

One Fly Machine per tenant/collection, booted from the existing
`registry.gitlab.com/flowlexi/pavedb` image (CPU and GPU variants already
built). No Kubernetes to operate, per-machine volumes for the store, deploys
straight off the current container pipeline. No AWS/GCP footprint exists
anywhere in Flowlexi's infra today, so there's no ops muscle to leverage
there, and standing up IAM/VPC/ASG plumbing for a solo-maintained product
would be pure overhead against a "no cluster" pitch. Hetzner bare-metal is
cheaper per-vCPU but reintroduces the cluster-ops burden the product exists
to avoid.

### Embedder hosting, per backend

| Embedder | Where it runs | Cost model |
|---|---|---|
| `sbert` | Co-located sidecar process on the tenant's own Fly Machine, **CPU-only** | Included in base price |
| `identity` | N/A — customer supplies precomputed vectors | Free |
| `openai` | OpenAI's infra; PaveDB proxies the call | BYO key or metered pass-through |
| `ollama` | **Customer's own server** — self-hosted or a GPU host of their choice, PaveDB just points at their endpoint | Zero hosting cost to us; customer picks their own GPU pricing |

`ollama` is not a candidate for Flowlexi-run compute — its whole value here
is that the customer already has (or wants) a remote Ollama endpoint on
hardware they control. Net effect: Flowlexi never operates GPU infrastructure
for embedding. GPU compute is either OpenAI's problem or the customer's.

Matches the existing sidecar/thread-pool split in
[`docs/design/PLAN-EMBEDDER-WORKERS.md`](docs/design/PLAN-EMBEDDER-WORKERS.md)
(P1-61): `sbert` as a co-located
process pool, `openai`/`ollama` as remote clients over HTTP — the cloud
offering doesn't need a new execution model, just a place to run the
sidecar.
- LlamaIndex adapter.
