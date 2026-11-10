<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# PaveDB Config Reference

Generated from `pave.config.CONFIG_FIELDS`. Regenerate with `make docs-refresh`.

Environment variables use the `PAVEDB_` prefix. Nested config keys
replace dots with double underscores, so `auth.mode` becomes
`PAVEDB_AUTH__MODE`.

## General

### `data_dir`

- Default: `"~/pavedb/data"`
- Env var: `PAVEDB_DATA_DIR`
- Description: Root directory for local catalog, metadata, document chunks, and indexes.
  When omitted at runtime, defaults to the instance home data directory.

### `common_enabled`

- Default: `false`
- Env var: `PAVEDB_COMMON_ENABLED`
- Description: Allow searches to include the configured common collection when
  requested.

### `common_tenant`

- Default: `"global"`
- Env var: `PAVEDB_COMMON_TENANT`
- Description: Tenant that owns the common collection.

### `common_collection`

- Default: `"common"`
- Env var: `PAVEDB_COMMON_COLLECTION`
- Description: Collection name used for common shared results.

### `dev`

- Default: `false`
- Env var: `PAVEDB_DEV`
- Description: Enable development startup behavior such as allowing auth.mode=none on
  loopback and verbose PaveDB logging.

## Query Log

### `query_log.enabled`

- Default: `true`
- Env var: `PAVEDB_QUERY_LOG__ENABLED`
- Description: Enable persisted query history and query-home lookup pointers.

## Other

### `cache.collection_max`

- Default: `64`
- Env var: `PAVEDB_CACHE__COLLECTION_MAX`
- Description: Max open collection backend and metadata handles; values below 1 become
  1.

### `cache.embedder_max`

- Default: `32`
- Env var: `PAVEDB_CACHE__EMBEDDER_MAX`
- Description: Max runtime embedder instances in the hot cache; active collections stay
  pinned.

### `archive.max_concurrent`

- Default: `2`
- Env var: `PAVEDB_ARCHIVE__MAX_CONCURRENT`
- Description: Collection archive dumps/restores running at once on this instance; -1
  disables the bound. Archives read or rebuild a whole collection, so the default stays
  small.

### `archive.max_concurrent_per_tenant`

- Default: `1`
- Env var: `PAVEDB_ARCHIVE__MAX_CONCURRENT_PER_TENANT`
- Description: Collection archive operations one tenant may run at once; -1 disables the
  bound.

### `reindex.max_concurrent`

- Default: `1`
- Env var: `PAVEDB_REINDEX__MAX_CONCURRENT`
- Description: Reindex jobs running at once on this instance; -1 disables the bound. A
  reindex re-embeds a whole collection.

### `reindex.max_concurrent_per_tenant`

- Default: `1`
- Env var: `PAVEDB_REINDEX__MAX_CONCURRENT_PER_TENANT`
- Description: Reindex jobs one tenant may run at once; -1 disables the bound.

## Authentication

### `auth.mode`

- Default: `"static"`
- Env var: `PAVEDB_AUTH__MODE`
- Description: Authentication mode. Use static outside dev; none is accepted only under
  the startup policy's dev safeguards.

### `auth.default_access_tenant`

- Default: `"public"`
- Env var: `PAVEDB_AUTH__DEFAULT_ACCESS_TENANT`
- Description: Tenant label assigned to unauthenticated requests when auth.mode is none.
  Tenant names must match ^[a-z0-9][a-z0-9-]{0,62}$ (lowercase ASCII, digits and hyphen)
  -- the same rule as collection names. Lowercase-only keeps a name from colliding with
  another under case folding or Unicode normalization on case-insensitive filesystems
  (APFS, NTFS, SMB), where two such tenants would otherwise share one data directory.

### `auth.global_key`

- Default: `null`
- Env var: `PAVEDB_AUTH__GLOBAL_KEY`
- Description: Admin bearer token. Server startup generates admin.key when omitted.

### `auth.api_keys`

- Default: `{}`
- Env var: `PAVEDB_AUTH__API_KEYS`
- Description: Map of tenant name to bearer token when auth.mode is static. Set
  PAVEDB_AUTH__API_KEYS to a JSON object (for example, {"acme":"secret"}) or set
  individual keys with PAVEDB_AUTH__API_KEYS__<TENANT>. Tenant names must match
  ^[a-z0-9][a-z0-9-]{0,62}$ (lowercase ASCII, digits and hyphen) -- the same rule as
  collection names. Lowercase-only keeps a name from colliding with another under case
  folding or Unicode normalization on case-insensitive filesystems (APFS, NTFS, SMB),
  where two such tenants would otherwise share one data directory. To revoke a tenant's
  key, delete its entry from the tenants file, set a new value, or set it to null in
  config.yml or the JSON environment map; null also removes a key that only the tenants
  file supplies. Environment override names are case-folded and match an existing config
  key case-insensitively.

### `auth.tenants_file`

- Default: `null`
- Env var: `PAVEDB_AUTH__TENANTS_FILE`
- Description: Optional sidecar YAML file containing auth.api_keys and tenants.* values.

## Vector Store

### `vector_store.type`

- Default: `"faiss"`
- Env var: `PAVEDB_VECTOR_STORE__TYPE`
- Description: Vector store backend used for local indexes.

## Embedding

Embedder instances are configured under `embedder.instances`.
Instance names are operator-defined. `config.yml.example`
shows commented Ollama and OpenAI examples; only the
`native` instance is active by default.

Any instance `config` field — plus `model` — also has a global
per-type fallback at `embedder.<type>.<field>` (for example
`embedder.sbert.device` or `embedder.openai.batch_size`). The
global value applies when an instance of that type omits the
field from its own `config`; the per-instance value always wins.

OpenAI-compatible instances (`openai`, `ollama`) accept
`timeout_s`: the total budget in seconds for one embed request,
SDK retries included (default 60). It bounds how long an
unresponsive provider can hold a worker slot.

They also accept `workers`: the per-instance concurrent-call
bound. `auto` (the default) uses at most four clone-safe HTTP
clients. Worker count is runtime-only and never changes a
collection's vector-space identity.

### `embedder.default`

- Default: `null`
- Env var: `PAVEDB_EMBEDDER__DEFAULT`
- Description: Default embedder selector. Use a user-defined instance key, type,
  type:model, or full vector key. Required when more than one embedder instance is
  configured.

### `embedder.routing.policy`

- Default: `"first"`
- Env var: `PAVEDB_EMBEDDER__ROUTING__POLICY`
- Description: How each collection load chooses among configured instances in the same
  vector space. First uses config order; round_robin rotates; weighted rotates by each
  instance weight; failover tries in order until one initializes. Routing is runtime-
  only and is never persisted in collection catalog state.

### `embedder.routing.priority.ingest`

- Default: `null`
- Env var: `PAVEDB_EMBEDDER__ROUTING__PRIORITY__INGEST`
- Description: Operator preference: one or more configured embedder instances (comma-
  separated or a list) tried in order for this operation class (ingest); the first
  instance sharing the collection's vector space runs it — one entry per model family.
  Nothing compatible, or the preferred instance unable to serve, falls back to
  embedder.routing.policy. Never a caller choice.

### `embedder.routing.priority.search`

- Default: `null`
- Env var: `PAVEDB_EMBEDDER__ROUTING__PRIORITY__SEARCH`
- Description: Operator preference: one or more configured embedder instances (comma-
  separated or a list) tried in order for this operation class (search); the first
  instance sharing the collection's vector space runs it — one entry per model family.
  Nothing compatible, or the preferred instance unable to serve, falls back to
  embedder.routing.policy. Never a caller choice.

### `embedder.routing.priority.maintenance`

- Default: `null`
- Env var: `PAVEDB_EMBEDDER__ROUTING__PRIORITY__MAINTENANCE`
- Description: Operator preference: one or more configured embedder instances (comma-
  separated or a list) tried in order for this operation class (maintenance); the first
  instance sharing the collection's vector space runs it — one entry per model family.
  Nothing compatible, or the preferred instance unable to serve, falls back to
  embedder.routing.policy. Never a caller choice.

### `embedder.warmup_default`

- Default: `null`
- Env var: `PAVEDB_EMBEDDER__WARMUP_DEFAULT`
- Description: Warm the default embedder at server startup before the first query. Null
  uses the startup policy: enabled outside dev, disabled in dev.

### `embedder.instances.native.type`

- Default: `"sbert"`
- Env var: `PAVEDB_EMBEDDER__INSTANCES__NATIVE__TYPE`
- Description: Built-in native embedder instance type. Native means PaveDB runs the
  embedder itself rather than calling an external service.

### `embedder.instances.native.model`

- Default: `"sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"`
- Env var: `PAVEDB_EMBEDDER__INSTANCES__NATIVE__MODEL`
- Description: Default sentence-transformers model for the native instance.

### `embedder.instances.native.weight`

- Default: `1`
- Env var: `PAVEDB_EMBEDDER__INSTANCES__NATIVE__WEIGHT`
- Description: Positive routing weight for the native instance when
  embedder.routing.policy is weighted. Custom instances accept the same sibling field.

### `embedder.instances.native.config.runtime`

- Default: `"auto"`
- Env var: `PAVEDB_EMBEDDER__INSTANCES__NATIVE__CONFIG__RUNTIME`
- Description: Native SBERT runtime mode: auto, process, or direct. On macOS with FAISS,
  auto selects process; direct is blocked.

### `embedder.instances.native.config.device`

- Default: `"auto"`
- Env var: `PAVEDB_EMBEDDER__INSTANCES__NATIVE__CONFIG__DEVICE`
- Description: Optional native SBERT device override; cpu | cuda | mps | auto lets the
  adapter choose.

### `embedder.instances.native.config.batch_size`

- Default: `null`
- Env var: `PAVEDB_EMBEDDER__INSTANCES__NATIVE__CONFIG__BATCH_SIZE`
- Description: Optional native SBERT encode batch size; null uses the adapter default.

### `embedder.instances.native.config.trust_remote_code`

- Default: `false`
- Env var: `PAVEDB_EMBEDDER__INSTANCES__NATIVE__CONFIG__TRUST_REMOTE_CODE`
- Description: Pass `trust_remote_code=True` to Sentence-Transformers when loading the
  model. Required by models with custom architectures (for example `nomic-ai/nomic-
  embed-text-v1.5`). Execution detail; not part of the vector-space identity, and not
  inherited by a collection that names a different model.

### `embedder.instances.native.config.endpoint`

- Default: `null`
- Env var: `PAVEDB_EMBEDDER__INSTANCES__NATIVE__CONFIG__ENDPOINT`
- Description: TEI-compatible `/embed` server used when `runtime` is `remote`. The
  remote server must run the identical model in float32; the endpoint is not part of the
  vector-space identity, so numerical equivalence with local runtimes is the operator's
  contract.

### `embedder.instances.native.config.query_prefix`

- Default: `null`
- Env var: `PAVEDB_EMBEDDER__INSTANCES__NATIVE__CONFIG__QUERY_PREFIX`
- Description: Prefix prepended to query texts before encoding (e5-style `query: `).
  Changes produced vectors and therefore participates in the vector-space identity.

### `embedder.instances.native.config.passage_prefix`

- Default: `null`
- Env var: `PAVEDB_EMBEDDER__INSTANCES__NATIVE__CONFIG__PASSAGE_PREFIX`
- Description: Prefix prepended to passage texts before encoding (e5-style `passage: `).
  Changes produced vectors and therefore participates in the vector-space identity.

### `embedder.instances.native.config.workers`

- Default: `"auto"`
- Env var: `PAVEDB_EMBEDDER__INSTANCES__NATIVE__CONFIG__WORKERS`
- Description: Native SBERT process worker count; auto picks a conservative local
  default capped for mixed search/ingest tail stability.

## Ingest

### `ingest.max_file_size_mb`

- Default: `500`
- Env var: `PAVEDB_INGEST__MAX_FILE_SIZE_MB`
- Description: Reject document uploads larger than this many megabytes; -1 disables the
  limit.

### `ingest.max_batch_size_mb`

- Default: `null`
- Env var: `PAVEDB_INGEST__MAX_BATCH_SIZE_MB`
- Description: Reject a batch ingest request whose documents add up to more than this
  many megabytes of text; null uses ingest.max_file_size_mb, -1 disables the limit.

### `ingest.max_concurrent`

- Default: `7`
- Env var: `PAVEDB_INGEST__MAX_CONCURRENT`
- Description: Maximum number of concurrent ingest operations; -1 disables the cap.

### `ingest.max_concurrent_per_tenant`

- Default: `-1`
- Env var: `PAVEDB_INGEST__MAX_CONCURRENT_PER_TENANT`
- Description: Maximum share of ingest.max_concurrent that one tenant may hold. -1 (the
  default) disables the share: any tenant may fill the whole ingest pool, which is what
  a single-tenant instance wants. Set it on a shared instance, where one tenant filling
  the pool starves every other. Note ingest.max_concurrent (7) is smaller than
  tenants.default_max_concurrent (42), so the per-tenant request cap cannot bind on
  ingest and this is the only per-tenant ingest bound.

## Search

### `search.max_concurrent`

- Default: `42`
- Env var: `PAVEDB_SEARCH__MAX_CONCURRENT`
- Description: Maximum number of concurrent search operations; -1 disables the cap.

### `search.max_concurrent_per_tenant`

- Default: `-1`
- Env var: `PAVEDB_SEARCH__MAX_CONCURRENT_PER_TENANT`
- Description: Maximum share of search.max_concurrent that one tenant may hold. -1 (the
  default) disables the share: any tenant may fill the whole search pool, which is what
  a single-tenant instance wants. Set it on a shared instance, where one tenant filling
  the pool starves every other. Note search.max_concurrent (42) is not smaller than
  tenants.default_max_concurrent (42), so the per-tenant request cap only binds once a
  tenant already holds the entire pool. A search costs far less than an ingest, so this
  share can be generous: about two thirds of the pool still leaves room for the other
  tenants. A request over this share is refused with 503 search_overloaded; a request
  over tenants.default_max_concurrent is refused earlier, with 429 tenant_rate_limited.

### `search.timeout_ms`

- Default: `30000`
- Env var: `PAVEDB_SEARCH__TIMEOUT_MS`
- Description: Per-search timeout in milliseconds; 0 disables the timeout.

### `search.modes`

- Default: `['vector', 'boost', 'hybrid']`
- Env var: `PAVEDB_SEARCH__MODES`
- Description: Search modes this instance serves. vector ranks by vector similarity
  alone; boost adds an exact-token nudge inside the vector candidate window; hybrid
  fuses vector and full-text rankings. Must include vector. A collection's default mode
  and any requested mode must be listed here, or the search answers 400
  search_mode_disabled.

### `search.boost_weight`

- Default: `0.1`
- Env var: `PAVEDB_SEARCH__BOOST_WEIGHT`
- Description: Largest score the boost mode adds, for a chunk matching every query
  token; a chunk matching m of n tokens gains weight * m / n. Must be greater than 0 and
  at most 1.

## Chunking

### `chunking.strategies`

- Default: `['fixed', 'none']`
- Env var: `PAVEDB_CHUNKING__STRATEGIES`
- Description: Chunking strategies collections may choose at creation. fixed cuts free
  text into overlapping windows of characters; none keeps each text as one chunk, for
  callers that chunk before ingesting. PDF pages and CSV rows stay one chunk each under
  either.

### `chunking.min_size`

- Default: `200`
- Env var: `PAVEDB_CHUNKING__MIN_SIZE`
- Description: Smallest chunk size, in characters, a collection may choose for the fixed
  strategy.

### `chunking.max_size`

- Default: `8000`
- Env var: `PAVEDB_CHUNKING__MAX_SIZE`
- Description: Largest chunk size, in characters, a collection may choose for the fixed
  strategy; also the longest text the none strategy accepts as one chunk (longer is 400
  chunk_too_large).

### `chunking.min_overlap`

- Default: `0`
- Env var: `PAVEDB_CHUNKING__MIN_OVERLAP`
- Description: Smallest overlap, in characters, a collection may choose for the fixed
  strategy.

### `chunking.max_overlap`

- Default: `2000`
- Env var: `PAVEDB_CHUNKING__MAX_OVERLAP`
- Description: Largest overlap, in characters, a collection may choose for the fixed
  strategy. Any chosen overlap must also be at most a quarter of the chosen size.

### `chunking.default_strategy`

- Default: `"fixed"`
- Env var: `PAVEDB_CHUNKING__DEFAULT_STRATEGY`
- Description: Strategy for collections created without one; must be listed in
  chunking.strategies.

### `chunking.default_size`

- Default: `1000`
- Env var: `PAVEDB_CHUNKING__DEFAULT_SIZE`
- Description: fixed chunk size for collections that do not name one.

### `chunking.default_overlap`

- Default: `200`
- Env var: `PAVEDB_CHUNKING__DEFAULT_OVERLAP`
- Description: fixed chunk overlap for collections that do not name one.

## Tenants

### `tenants.default_max_concurrent`

- Default: `42`
- Env var: `PAVEDB_TENANTS__DEFAULT_MAX_CONCURRENT`
- Description: Default per-tenant concurrent request cap; -1 disables the cap.

### `tenants.default_max_rpm`

- Default: `-1`
- Env var: `PAVEDB_TENANTS__DEFAULT_MAX_RPM`
- Description: Default moving-window request limit per tenant and minute; -1 disables
  the limit. Per-tenant max_rpm in tenants.yml overrides it.

### `tenants.default_max_rph`

- Default: `-1`
- Env var: `PAVEDB_TENANTS__DEFAULT_MAX_RPH`
- Description: Default moving-window request limit per tenant and hour; -1 disables the
  limit. Per-tenant max_rph in tenants.yml overrides it.

### `tenants.default_max_collections`

- Default: `-1`
- Env var: `PAVEDB_TENANTS__DEFAULT_MAX_COLLECTIONS`
- Description: Default per-tenant collection-count limit enforced at creation; -1
  disables the cap. Per-tenant max_collections in tenants.yml overrides it.

### `tenants.default_max_chunks_per_collection`

- Default: `-1`
- Env var: `PAVEDB_TENANTS__DEFAULT_MAX_CHUNKS_PER_COLLECTION`
- Description: Default cap on indexed chunks per collection enforced at ingest; -1
  disables the cap. Per-tenant max_chunks_per_collection in tenants.yml overrides it.

### `tenants.default_max_chunks_per_tenant`

- Default: `-1`
- Env var: `PAVEDB_TENANTS__DEFAULT_MAX_CHUNKS_PER_TENANT`
- Description: Default cap on indexed chunks across all of a tenant's collections,
  enforced at ingest; -1 disables the cap. Per-tenant max_chunks_per_tenant in
  tenants.yml overrides it. This is the tenant's storage ceiling:
  max_chunks_per_collection alone bounds one collection, so a tenant allowed several has
  no ceiling without this.

### `tenants.default_max_archives_per_day`

- Default: `-1`
- Env var: `PAVEDB_TENANTS__DEFAULT_MAX_ARCHIVES_PER_DAY`
- Description: Collection archive operations (dump or restore) a tenant may start per
  day; -1 = unlimited. Override per tenant as tenants.<name>.max_archives_per_day.
  Charged on the persistent per-operation rate window.

### `tenants.default_max_reindex_per_month`

- Default: `-1`
- Env var: `PAVEDB_TENANTS__DEFAULT_MAX_REINDEX_PER_MONTH`
- Description: Reindex jobs a tenant may start per 30 days; -1 = unlimited. Override per
  tenant as tenants.<name>.max_reindex_per_month. Charged on the persistent per-
  operation rate window.

## Server

### `server.host`

- Default: `"127.0.0.1"`
- Env var: `PAVEDB_SERVER__HOST`
- Description: Default server bind host after startup policy enforcement.

### `server.port`

- Default: `8086`
- Env var: `PAVEDB_SERVER__PORT`
- Description: Default server bind port.

### `server.reload`

- Default: `false`
- Env var: `PAVEDB_SERVER__RELOAD`
- Description: Enable uvicorn reload when running the packaged server.

### `server.workers`

- Default: `1`
- Env var: `PAVEDB_SERVER__WORKERS`
- Description: Number of Uvicorn worker processes launched by pavesrv. Values greater
  than 1 are experimental, allowed only in dev mode, and may corrupt data; packaged
  production startup rejects them.

### `server.max_request_body_mb`

- Default: `16`
- Env var: `PAVEDB_SERVER__MAX_REQUEST_BODY_MB`
- Description: Reject general request bodies above this many MB with 413 before routing
  or authentication; -1 disables the cap. Documents use ingest.max_file_size_mb. Archive
  restore authenticates before upload and is exempt.

### `server.timeout_keep_alive`

- Default: `75`
- Env var: `PAVEDB_SERVER__TIMEOUT_KEEP_ALIVE`
- Description: Uvicorn keep-alive timeout in seconds.

### `ui.enabled`

- Default: `null`
- Env var: `PAVEDB_UI__ENABLED`
- Description: Serve the interactive OpenAPI/Swagger UI (the /ui bundle and FastAPI
  /docs, /redoc, /openapi.json). Null uses the startup policy: enabled in dev, disabled
  in production.

## Logging

### `log.level`

- Default: `"INFO"`
- Env var: `PAVEDB_LOG__LEVEL`
- Description: Base stderr log level.

### `log.ops_log`

- Default: `null`
- Env var: `PAVEDB_LOG__OPS_LOG`
- Description: Destination for structured operation logs: null/off, stdout, or a file
  path. A new file is created mode 0640 (its directory 0750) because every line names a
  tenant and collection. PaveDB does not rotate it; rotate externally, e.g. logrotate
  with copytruncate.

### `log.access_log`

- Default: `null`
- Env var: `PAVEDB_LOG__ACCESS_LOG`
- Description: Destination for uvicorn access logs: null/off, stdout, or a file path.

### `log.debug`

- Default: `[]`
- Env var: `PAVEDB_LOG__DEBUG`
- Description: Logger namespaces forced to DEBUG level.

### `log.watch`

- Default: `[]`
- Env var: `PAVEDB_LOG__WATCH`
- Description: Logger namespaces made one level more verbose than the base log level.

### `log.quiet`

- Default: `['uvicorn', 'uvicorn.access', 'uvicorn.error', 'fastapi', 'sqlalchemy', 'urllib3', 'httpx']`
- Env var: `PAVEDB_LOG__QUIET`
- Description: Noisy dependency logger namespaces made one level quieter than the base
  log level.

## Instance

### `instance.name`

- Default: `null`
- Env var: `PAVEDB_INSTANCE__NAME`
- Description: Display instance name used by the API metadata and UI.

### `instance.desc`

- Default: `"Inspectable Retrieval Database"`
- Env var: `PAVEDB_INSTANCE__DESC`
- Description: Description used by the API metadata and UI.
