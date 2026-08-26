# PaveDB 0.9.6

<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

## English

PaveDB 0.9.6 continues the security hardening started in 0.9.5 and adds
hand-written guides for operators. It is a 1.0 preview release.

### Security

This release closes defects in request handling, tenant isolation, provider
credentials, and archive safety. Operators running 0.9.5 should upgrade.

- **In-memory request bodies and document uploads are bounded before
  authentication.** The whole body used to be read and parsed before any
  credential was checked, so an unauthenticated caller could make the process
  buffer an arbitrary payload. Oversized JSON and document uploads now answer
  `413 request_too_large` before routing. Whole-instance restore checks the
  admin credential before consuming the upload and does not inherit the
  single-document ceiling.
- **Batch ingest is capped on the total.** The size limit applied per document,
  so a batch of many documents each just under the cap passed while the total
  was many times the limit.
- **Embedder types are limited to the modules PaveDB ships.** A collection
  could name an arbitrary dotted module and have it imported into the API
  process.
- **Unconfigured provider models cannot inherit operator credentials.** An
  OpenAI model that does not match a configured instance is rejected at
  collection creation instead of receiving an explicit or ambient operator
  key.
- **Read paths no longer create collections.** Searching or deleting from a
  name that does not exist used to bring that collection into being, outside
  the per-tenant collection quota.
- **Concurrency slots are released exactly once.** A timed-out search or a
  disconnected ingest released its slot while the work continued on a pool
  thread, so the counters disagreed with reality and a tenant could exceed its
  own cap.
- **Shared-scope search enters the per-tenant gate.** Those routes bypassed the
  concurrency cap entirely.
- **Remote embedder calls are bounded in total.** A per-request timeout is not
  a budget when the client retries timeouts; a slow provider could pin a worker
  for minutes.
- **Metric labels cannot be grown without limit.** An unauthenticated caller
  could mint permanent series in the shared catalog by varying the request path
  or the HTTP verb.
- **Index durability.** A rebuild no longer drops raw (BYOV) vectors silently,
  and `flush()` refuses to overwrite a populated index with an empty one. An
  invalid or inconsistent FAISS/ID-map pair now fails closed, and a rebuild
  with any unreconstructible chunk is refused before it can publish a partial
  index.
- **Archive dumps preserve live SQLite state.** A dump no longer removes the
  live catalog WAL and shared-memory files held by another worker. The catalog
  is checkpointed for the archive without unlinking another process's files.
- **The packaged server constrains production to one worker.** Multiple
  workers keep independent FAISS caches over the same data directory and can
  lose or serve stale vectors. `server.workers>1` is now rejected outside dev
  mode; dev mode permits it only with an explicit data-corruption warning.
  The packaged `pave.main:app` target now rejects direct ASGI loading; use
  `pavesrv`. Multiple replicas sharing one data directory remain unsupported.

### Fairness on shared instances

Two new settings bound what one tenant can take from a pool everyone shares.
Both are **off by default**, because the typical instance serves one tenant and
a share is pure throughput loss there:

- `ingest.max_concurrent_per_tenant`
- `search.max_concurrent_per_tenant`

Turn them on when tenants you do not control share an instance. The reasoning,
and why the existing `tenants.default_max_concurrent` does not cover this, is
in the new guide.

### Documentation

New hand-written guides in `docs/guides/`: what PaveDB is, which component to
reach for, the basic operations, and what an operator has to get right,
including a section on running a shared instance. They orient; the generated
references answer in full; the book goes deep.

### Upgrade note

No success-response schema change and no data migration. Oversized requests can
now return `413`; these other behaviours are worth noting:

- Search on a collection that does not exist still answers `200` with no
  matches — but no longer creates that collection as a side effect. If you
  relied on searching to bring a collection into being, create it explicitly.
- If `common_enabled` is set, the configured shared corpus is created at
  startup instead of by the first shared search.
- An OpenAI model must match a configured embedder instance. Configure every
  model tenants are allowed to select; unconfigured names are rejected before
  any provider client is created.
- Packaged production startup now rejects `server.workers>1`. Set it to `1`;
  dev mode remains available for disposable multi-worker experiments.
- Direct loading of `pave.main:app`, such as `uvicorn pave.main:app`, is
  rejected. Use `pavesrv`; tests should call `build_app(get_cfg())` directly.
- An invalid FAISS/ID-map pair, or any chunk that cannot be reconstructed,
  now makes that collection unavailable instead of risking a partial rebuild.
  Repair or restore the index state before serving the collection.

The startup checks introduced in 0.9.5 are unchanged: tenant names must match
`^[a-z0-9][a-z0-9-]{0,62}$`, tokens must be quoted non-empty strings, and no
two tenants may share a token.

### Links

- [Project website](https://pavedb.org/)
- [GitLab repository](https://gitlab.com/flowlexi/pavedb)
- [GitHub mirror](https://github.com/rodrigopitanga/pavedb)
- [PyPI project](https://pypi.org/project/pavedb/)
