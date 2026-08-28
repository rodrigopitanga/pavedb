<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Running it

What an operator has to get right. Every key below is documented in full, with
its default and environment variable, in [`config.md`](../reference/config.md).

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

For a running server, use `GET /v1/admin/archive` to download a snapshot and
`PUT /v1/admin/archive` to restore one. Those routes coordinate with the live
store; do not run `pavecli dump-archive` or `restore-archive` beside it.

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

A body over its ceiling is refused with `413 request_too_large` before the
request reaches a route, so an oversized client sees that rather than a
timeout. Document uploads get the ingest ceiling; other bodies get the tighter
`server.max_request_body_mb`. Whole-instance restore checks the admin credential
before consuming the upload and is exempt from both limits. The handler reads
the archive into memory, so leave enough headroom for its compressed size.

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

A shared instance without these means one tenant can consume the disk, the
index memory, and the pools that every other tenant depends on.

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
