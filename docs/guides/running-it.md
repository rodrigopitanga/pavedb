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

Stop the server before you back it up. Copying the directory underneath a
running instance can catch SQLite mid-write, and `pavecli dump-archive` is an
offline tool: it operates on the data directory directly and does not
coordinate with a live server.

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
- `server.max_request_body_mb` — every non-upload request body, checked before
  routing and before authentication

A body over its ceiling is refused with `413 request_too_large` before the
request reaches a route, so an oversized client sees that rather than a
timeout. Upload endpoints get the ingest ceiling; everything else gets the
tighter `server.max_request_body_mb`, because a JSON body is parsed into
memory rather than spooled to disk.

## Running a shared instance

The defaults assume one tenant. If several tenants you do not control share an
instance, three more settings stop one of them from consuming what the others
depend on:

- `ingest.max_concurrent_per_tenant` — how many of the ingest slots one tenant
  may hold. **Off by default.** On a single-tenant instance a share is pure
  throughput loss, so it is opt-in — but without it one tenant can hold every
  slot and everyone else gets `503`. Note `tenants.default_max_concurrent` is
  larger than the ingest pool, so the general per-tenant request cap cannot
  bind here; this setting is the only per-tenant ingest bound.
- per-tenant collection and chunk caps — bound how much of the disk and index
  memory a tenant can claim.
- `tenants.default_max_concurrent` — the general per-tenant request cap.

A shared instance without these means one tenant can consume the disk, the
index memory, and the ingest pool that every other tenant depends on.

## Containers

The published image runs the server with a data directory you are expected to
mount. Two things people get wrong:

- **Mount the data directory.** Without a volume, the container layer holds your
  index and it dies with the container.
- **Pass the key by environment.** Baking `admin.key` into an image layer
  publishes it to anyone who can pull the image.

`server.host`, `server.port` and `server.workers` control the bind. With more
than one worker, remember that they share the data directory.

## The UI

`ui.enabled` gates the built-in OpenAPI UI. It is off by default in production
and should stay that way on a public listener: it is a browser surface that
carries an admin key.
