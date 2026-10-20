<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Running it

What an operator has to get right. Every key below is documented in full, with
its default and environment variable, in [`config.md`](../reference/config.md).

## Production checklist

Work through this before the first tenant arrives. Each step links to the
section that explains it.

1. **Authentication.** `auth.mode: static`, one token per tenant in the
   tenants file, and the admin key in `admin.key` (created 0600 at first
   start) or `auth.global_key` from the environment. A request without a token
   must get `401`. See [Auth](#auth).
2. **Persistent data.** Put `data_dir` and the instance home (`admin.key`,
   `tenants.yml`) on durable storage writable by the server user and nobody
   else. In a container, mount a volume. See
   [The data directory](#the-data-directory) and [Containers](#containers).
3. **Resource limits.** Size the ingest, body and search ceilings for your
   hardware. On a shared instance, also set the per-tenant shares, request-rate
   windows and collection/chunk caps. See [Limits](#limits) and
   [Running a shared instance](#running-a-shared-instance).
4. **Health.** Readiness probe on `/health/ready`, liveness on
   `/health/live`. See [Health](#health).
5. **Metrics.** Scrape `/metrics` (Prometheus text) or read `/health/metrics`
   (JSON). Both require the admin key under static auth.
6. **Query-log retention.** Decide whether to keep query text. See
   [The query log](#the-query-log).
7. **Backup and restore.** Schedule archives, and rehearse a restore into a
   scratch instance before you need one. See
   [Backups and portability](#backups-and-portability).

## Auth

Two modes, and only one of them belongs in production.

- `auth.mode=static` — bearer tokens. `auth.api_keys` maps a tenant name to its
  token; `auth.global_key` is the admin credential that spans tenants.
- `auth.mode=none` — no credentials at all. Refused outside dev mode, and forced
  onto loopback when it is allowed.

The server refusing to start unauthenticated is a feature, not an obstacle. If
startup fails with a message about `auth.mode=none`, it just prevented you from
serving your tenants' data to the internet.

Tenant names must match `^[a-z0-9][a-z0-9-]{0,62}$`, tokens must be quoted
strings, and no two tenants may share a token. All three are checked at startup
and name the offending entry, because each has a way of failing silently:
a YAML value like `yes` becomes the token `True`, and a shared token
authenticates its holder as whichever tenant is listed first.

Keys live in `config.yml` or in the sidecar named by `auth.tenants_file`.
`pavecli init` creates both.

## The data directory

`data_dir` holds everything: every tenant, collection, vector index, metadata
database and query log. There is no external database.

That makes backups simple — take the directory as a unit — and it makes the
directory the thing to protect.

Each canonical local data directory has one supported local owner. `pavesrv`,
direct `pavecli` store commands, and a persistent local Python client take the
same non-blocking ownership lease. A second supported owner fails before
opening the store. To work with a running server, use an HTTP client instead
of the local CLI. The fence is for entry points on one filesystem; it is not a
distributed or NFS lease, and it does not make dev multi-worker mode safe.

Stop the server before copying the data directory. Copying it underneath a
running instance can catch SQLite mid-write. `pavecli dump-archive` is an
offline tool and refuses to run while another supported process owns the data
directory.

## Backups and portability

Two archive kinds, for two jobs.

**Whole-instance archive: the backup.** `GET /v1/admin/archive` downloads a
snapshot of a running server and `PUT /v1/admin/archive` restores one. Those
routes coordinate with the live store; do not run `pavecli dump-archive` or
`restore-archive` beside it. Offline, with the server stopped, the CLI
commands do the same. The archive is the data directory: the catalog and,
per collection, its documents, chunk text and metadata, vectors, query log
and embedder identity. It holds no credentials. `admin.key`, the tenants
file, `config.yml` and provider API keys live outside the data directory, so
back them up separately. Restore replaces the whole instance and accepts
archives whose vector spaces the destination's configuration can serve.

**Collection archive: portability.** `GET` on
`/v1/admin/collections/{tenant}/{name}/archive` downloads one collection and
`POST` on a destination path restores it as a new collection, under the same
or a new tenant/collection name, without touching anything else. The
destination must not exist yet (`409 collection_exists`), and it counts
against the tenant's collection limit. `PUT` on the same path instead rolls an
existing collection back to the snapshot (`404` if it does not exist). A
tenant can do the same for its own collections with its own key on
`/v1/collections/{tenant}/{name}/archive`. The zip holds `manifest.json`
(PaveDB and schema version, embedder and backend spec), `meta.db` with the
collection's documents, chunks and query log, the FAISS index, and the
collection sidecars. Restore requires the same PaveDB and schema version and
resolves the embedder from the destination's configuration before it writes
anything. `pavecli dump-archive demo/books` and `pavecli restore-archive
demo/books books.zip` (`--replace` for the `PUT` case) do the same offline,
under the same stopped-server rule as the instance archive.

Use the first to recover an instance and the second to move one collection
between instances, environments or tenants.

## The query log

Every text search is recorded in its collection's metadata database by
default: the query text, `k`, filters, mode, timing, and the ranked result
ids. That record is what makes replay and drift comparison possible. It also
means query text is stored data. This release has no automatic retention
window: entries stay until their collection is deleted, and they travel with
both archive kinds. Set `query_log.enabled: false` if query text must not be
kept; searches still work, but no longer produce a record to inspect or
replay.

## Reindex

Rebuilding a collection against another embedding model is an admin job:
`POST /v1/admin/collections/{tenant}/{name}/reindex` starts it,
`GET /v1/admin/reindex/{job_id}` reports status and progress, and pause, resume
and cancel act at persisted batch boundaries. The new index is staged beside the
live one and swapped in at the end; an interrupted swap is rolled forward or
back at startup, never left half-installed, and a job interrupted earlier comes
back paused and resumes from its last persisted batch.

A running job holds its collection for the whole rebuild, not only for the swap.
Searches, ingests, deletes, a collection archive dump or restore, and a
whole-instance restore all wait behind it until it commits; other collections
keep serving. Treat a reindex as a maintenance window for that one collection —
pausing keeps the staged progress and hands the collection back at the next
batch boundary.

## Health

| Endpoint | Checks | Use for |
|---|---|---|
| `/health/live` | the process answers; does not touch the model | liveness |
| `/health/ready` | the data directory is writable | readiness |
| `/health` | status and version | humans |
| `/health/metrics` | operational counters | scraping |

`/health/ready` also reports the vector backend it verified, which is the
quickest way to confirm an instance is running the store you think it is.

Point an orchestrator at `/health/ready`. `/health/live` will answer while the
data directory is unwritable, which is exactly when you do not want traffic.

## Limits

Set these before you have tenants, not after:

- `ingest.max_file_size_mb` — largest single document accepted
- `ingest.max_concurrent` — instance-wide ingest slots
- `ingest.max_batch_size_mb` — total across one batch, not per document
- `server.max_request_body_mb` — general request bodies, checked before routing
  or authentication; `0` disables the cap

Chunking bounds belong here too: `chunking.min_size`/`max_size` and
`chunking.min_overlap`/`max_overlap` limit what a collection may choose at
creation, `chunking.strategies` which strategies it may choose, and the
`chunking.default_*` keys what it gets when it chooses nothing. Startup
refuses bounds that contradict each other, and an overlap above a quarter of
the chunk size. `chunking.max_size` also caps a `none` chunk.

A body over its ceiling is refused with `413 request_too_large` before the
request reaches a route, so an oversized client sees that rather than a
timeout. Document uploads get the ingest ceiling; other bodies get the tighter
`server.max_request_body_mb`. Whole-instance restore checks the admin credential
before consuming the upload and is exempt from both limits. The handler reads
the archive into memory, so leave enough headroom for its compressed size.

Request rate is the other ceiling, and it is off by default.
`tenants.default_max_rpm` and `tenants.default_max_rph` give every tenant a
moving-window budget per minute and per hour; `0` disables a window, and
`max_rpm` or `max_rph` under a tenant in the sidecar overrides the default for
that tenant alone. The counters live in the catalog, so a restart does not hand
a tenant a fresh budget, and a refused request is not charged against it. A
tenant over budget gets `429 tenant_rate_limited` with `Retry-After` and
`X-RateLimit-Limit`, `-Remaining` and `-Window`; requests that pass carry the
same headers, which is how a client sees what it has left. The per-tenant
concurrency cap below answers with the same code, so `-Window` is what tells the
two apart. Admin credentials bypass the budget entirely, so verify a limit with
a tenant token rather than the admin key.

## Running a shared instance

The defaults assume one tenant. If several tenants you do not control share an
instance, four more settings stop one of them from consuming what the others
depend on:

- `ingest.max_concurrent_per_tenant` — how many of the ingest slots one tenant
  may hold. **Off by default.** On a single-tenant instance a share is pure
  throughput loss, so it is opt-in — but without it one tenant can hold every
  slot and everyone else gets `503`. Note `tenants.default_max_concurrent` is
  larger than the ingest pool, so the general per-tenant request cap cannot
  bind here; this setting is the only per-tenant ingest bound.
- `search.max_concurrent_per_tenant` — the same share for the search pool, and
  **off by default** for the same reason. `tenants.default_max_concurrent` (42)
  is not smaller than `search.max_concurrent` (42), so that cap only bites once
  a tenant already holds every search slot — by which point its co-tenants are
  already seeing `503`. A search costs far less than an ingest, so this share
  can be more generous than the ingest one; around two thirds of the pool
  bounds a single tenant while barely costing it throughput.
- per-tenant collection and chunk caps — bound how much of the disk and index
  memory a tenant can claim.
- `tenants.default_max_concurrent` — the general per-tenant request cap.

Two of these can reject the same search, so the codes are distinct and one of
them always answers first. `tenants.default_max_concurrent` is enforced before
the route runs and answers `429 tenant_rate_limited`; the search share is
enforced at the pool and answers `503 search_overloaded`, naming the
`tenant-scoped search share` rather than the `instance-scoped search cap`. Set
the share below `tenants.default_max_concurrent` and it is the one you will
see; a `429` means the tenant is over its own request budget, before the pool
was ever consulted.

Collection archives and reindex jobs are whole-collection operations, so
they carry the same two dials with tighter defaults: `archive.max_concurrent`
(2) / `archive.max_concurrent_per_tenant` (1) and `reindex.max_concurrent`
(1) / `reindex.max_concurrent_per_tenant` (1) bound simultaneous work, and
`tenants.default_max_archives_per_day` / `tenants.default_max_reindex_per_month`
(both 0 = unlimited, per-tenant overrides as usual) charge the same
persistent rate windows the request budget uses — one counter engine, one
number-resolution path, whatever the window length. A refused operation
answers `503 <op>_capacity` (concurrency) or `429 <op>_rate_limited`
(window), and never spends the window when refused on concurrency.

A shared instance without these means one tenant can consume the disk, the
index memory, and the pools that every other tenant depends on.

## Bounding what a tenant stores

`max_chunks_per_collection` bounds one collection, so a tenant allowed ten of
them has a ceiling ten times that cap — which is no ceiling you can publish
from one number. `max_chunks_per_tenant` is that number: the indexed chunks a
tenant may hold across every collection it owns, refused at ingest with
`429 tenant_chunk_limit_reached`. Set it per tenant as
`tenants.<name>.max_chunks_per_tenant` in `tenants.yml`, or for every tenant
as `tenants.default_max_chunks_per_tenant`; 0 disables it. Set both caps, or
the per-collection cap alone bounds nothing at the tenant level. A write that
breaks both answers `tenant_chunk_limit_reached`, because the per-collection
code means there is room in another collection and here there is none.

It counts chunks rather than megabytes on purpose. Deleting a document gives
a chunk back immediately; it gives no disk back until a vacuum, because
SQLite keeps freed pages and the write-ahead log grows on the way. A cap
measured on file sizes would be one a tenant could not escape by deleting its
own data. To translate the number for a customer, multiply by your average
chunk size — the chunk count is what the engine enforces, and a document
count is what they can picture.

Delete paths are never charged against the ceiling, so a tenant that hits it
can always free its own budget. Re-ingesting a document at the ceiling is
allowed while it yields no more chunks than the version it replaces, since
the check is on the count the write leaves behind, not on the write itself.

## Containers

The published image runs the server with a data directory you are expected to
mount. Two things people get wrong:

- **Mount the data directory.** Without a volume, the container layer holds your
  index and it dies with the container.
- **Pass the key by environment.** Baking `admin.key` into an image layer
  publishes it to anyone who can pull the image.

`server.host`, `server.port` and `server.workers` control the bind. Keep
`server.workers=1` in production. Multiple workers currently keep independent
FAISS caches over the same data directory, so `pavesrv` rejects them in
production. Dev mode permits `server.workers>1` only for experiments and warns
that it may corrupt data; use disposable data. The packaged `pave.main:app`
target rejects direct ASGI loading; custom ASGI wrappers remain unsupported.
Multiple replicas sharing one data directory remain unsupported.

## The UI

`ui.enabled` gates the built-in OpenAPI UI. It is off by default in production
and should stay that way on a public listener: it is a browser surface that
carries an admin key.
