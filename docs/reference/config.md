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
- Description: Map of tenant name to bearer token when auth.mode is static. Tenant names
  must match ^[a-z0-9][a-z0-9-]{0,62}$ (lowercase ASCII, digits and hyphen) -- the same
  rule as collection names. Lowercase-only keeps a name from colliding with another
  under case folding or Unicode normalization on case-insensitive filesystems (APFS,
  NTFS, SMB), where two such tenants would otherwise share one data directory.

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

### `embedder.default`

- Default: `null`
- Env var: `PAVEDB_EMBEDDER__DEFAULT`
- Description: Default embedder selector. Use a user-defined instance key, type,
  type:model, or full vector key. Required when more than one embedder instance is
  configured.

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
- Description: Reject document uploads larger than this many megabytes; 0 disables the
  limit.

### `ingest.max_concurrent`

- Default: `7`
- Env var: `PAVEDB_INGEST__MAX_CONCURRENT`
- Description: Maximum number of concurrent ingest operations; 0 disables the cap.

## Search

### `search.max_concurrent`

- Default: `42`
- Env var: `PAVEDB_SEARCH__MAX_CONCURRENT`
- Description: Maximum number of concurrent search operations; 0 disables the cap.

### `search.timeout_ms`

- Default: `30000`
- Env var: `PAVEDB_SEARCH__TIMEOUT_MS`
- Description: Per-search timeout in milliseconds; 0 disables the timeout.

## Text Preprocessing

### `preprocess.txt_chunk_size`

- Default: `1000`
- Env var: `PAVEDB_PREPROCESS__TXT_CHUNK_SIZE`
- Description: Plain-text chunk size in characters.

### `preprocess.txt_chunk_overlap`

- Default: `200`
- Env var: `PAVEDB_PREPROCESS__TXT_CHUNK_OVERLAP`
- Description: Plain-text chunk overlap in characters.

## Tenants

### `tenants.default_max_concurrent`

- Default: `42`
- Env var: `PAVEDB_TENANTS__DEFAULT_MAX_CONCURRENT`
- Description: Default per-tenant concurrent request cap; 0 disables the cap.

### `tenants.default_max_collections`

- Default: `0`
- Env var: `PAVEDB_TENANTS__DEFAULT_MAX_COLLECTIONS`
- Description: Default per-tenant collection-count limit enforced at creation; 0
  disables the cap. Per-tenant max_collections in tenants.yml overrides it.

### `tenants.default_max_chunks_per_collection`

- Default: `0`
- Env var: `PAVEDB_TENANTS__DEFAULT_MAX_CHUNKS_PER_COLLECTION`
- Description: Default cap on indexed chunks per collection enforced at ingest; 0
  disables the cap. Per-tenant max_chunks_per_collection in tenants.yml overrides it.

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
- Description: Number of uvicorn worker processes.

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
  path.

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

- Default: `"Vector Search Microservice"`
- Env var: `PAVEDB_INSTANCE__DESC`
- Description: Description used by the API metadata and UI.
