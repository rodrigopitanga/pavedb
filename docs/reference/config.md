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

### `common_enabled`

- Default: `false`
- Env var: `PAVEDB_COMMON_ENABLED`
- Description: Also search a shared common collection for every tenant request.

### `common_tenant`

- Default: `"global"`
- Env var: `PAVEDB_COMMON_TENANT`
- Description: Tenant that owns the common collection.

### `common_collection`

- Default: `"common"`
- Env var: `PAVEDB_COMMON_COLLECTION`
- Description: Collection name used for common shared results.

## Authentication

### `auth.mode`

- Default: `"none"`
- Env var: `PAVEDB_AUTH__MODE`
- Description: Authentication mode. Use static outside dev; none is accepted only under
  the startup policy's dev safeguards.

### `auth.default_access_tenant`

- Default: `null`
- Env var: `PAVEDB_AUTH__DEFAULT_ACCESS_TENANT`
- Description: Tenant label assigned to unauthenticated requests when auth.mode is none.

### `auth.global_key`

- Default: `null`
- Env var: `PAVEDB_AUTH__GLOBAL_KEY`
- Description: Bearer token that grants admin access when auth.mode is static.

### `auth.api_keys`

- Default: `{}`
- Env var: `PAVEDB_AUTH__API_KEYS`
- Description: Map of tenant name to bearer token when auth.mode is static.

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

### `embedder.type`

- Default: `"sbert"`
- Env var: `PAVEDB_EMBEDDER__TYPE`
- Description: Default embedder adapter for new collections.

### `embedder.warmup_default`

- Default: `true`
- Env var: `PAVEDB_EMBEDDER__WARMUP_DEFAULT`
- Description: Warm the default embedder at server startup before the first query.

### `embedder.sbert.runtime`

- Default: `"auto"`
- Env var: `PAVEDB_EMBEDDER__SBERT__RUNTIME`
- Description: SBERT runtime mode: auto, process, or direct.

### `embedder.sbert.model`

- Default: `"sentence-transformers/all-MiniLM-L6-v2"`
- Env var: `PAVEDB_EMBEDDER__SBERT__MODEL`
- Description: Default sentence-transformers model for SBERT collections.

### `embedder.sbert.device`

- Default: `null`
- Env var: `PAVEDB_EMBEDDER__SBERT__DEVICE`
- Description: Optional SBERT device override; null lets the adapter choose.

### `embedder.sbert.batch_size`

- Default: `null`
- Env var: `PAVEDB_EMBEDDER__SBERT__BATCH_SIZE`
- Description: Optional SBERT encode batch size; null uses the adapter default.

### `embedder.openai.model`

- Default: `"text-embedding-3-small"`
- Env var: `PAVEDB_EMBEDDER__OPENAI__MODEL`
- Description: Default OpenAI embedding model.

### `embedder.openai.api_key`

- Default: `null`
- Env var: `PAVEDB_EMBEDDER__OPENAI__API_KEY`
- Description: OpenAI API key. OPENAI_API_KEY is also accepted by the adapter.

### `embedder.openai.base_url`

- Default: `null`
- Env var: `PAVEDB_EMBEDDER__OPENAI__BASE_URL`
- Description: Optional OpenAI-compatible base URL override.

### `embedder.openai.dim`

- Default: `null`
- Env var: `PAVEDB_EMBEDDER__OPENAI__DIM`
- Description: Embedding dimension for OpenAI collections; required when the dimension
  cannot be inferred from collection input.

### `embedder.openai.batch_size`

- Default: `null`
- Env var: `PAVEDB_EMBEDDER__OPENAI__BATCH_SIZE`
- Description: Optional OpenAI request batch size; null uses the adapter default.

### `embedder.ollama.model`

- Default: `"nomic-embed-text"`
- Env var: `PAVEDB_EMBEDDER__OLLAMA__MODEL`
- Description: Default Ollama embedding model.

### `embedder.ollama.base_url`

- Default: `"http://localhost:11434/v1"`
- Env var: `PAVEDB_EMBEDDER__OLLAMA__BASE_URL`
- Description: OpenAI-compatible Ollama endpoint.

### `embedder.ollama.dim`

- Default: `768`
- Env var: `PAVEDB_EMBEDDER__OLLAMA__DIM`
- Description: Embedding dimension for the default Ollama model.

### `embedder.ollama.batch_size`

- Default: `null`
- Env var: `PAVEDB_EMBEDDER__OLLAMA__BATCH_SIZE`
- Description: Optional Ollama request batch size; null uses the adapter default.

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

- Default: `0`
- Env var: `PAVEDB_TENANTS__DEFAULT_MAX_CONCURRENT`
- Description: Default per-tenant concurrent request cap; 0 disables the cap.

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

## Logging

### `log.level`

- Default: `"INFO"`
- Env var: `PAVEDB_LOG__LEVEL`
- Description: Base stderr log level.

### `log.ops_log`

- Default: `null`
- Env var: `PAVEDB_LOG__OPS_LOG`
- Description: Destination for structured operation logs: null, stdout, or a file path.

### `log.access_log`

- Default: `null`
- Env var: `PAVEDB_LOG__ACCESS_LOG`
- Description: Destination for uvicorn access logs: null, stdout, or a file path.

## Instance

### `instance.name`

- Default: `"PaveDB"`
- Env var: `PAVEDB_INSTANCE__NAME`
- Description: Display name used by the API metadata and UI.

### `instance.desc`

- Default: `"PaveDB Vector Search Microservice"`
- Env var: `PAVEDB_INSTANCE__DESC`
- Description: Description used by the API metadata and UI.
