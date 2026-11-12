# PaveDB 0.9.8

<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

## English

PaveDB 0.9.8 makes tenant limits explicit and enforceable, preserves metadata
values as supplied, and brings the embedded Python client up to the collection
operations introduced in 0.9.7. This is a 1.0 preview maintenance release.
**Operators upgrading from 0.9.7 must review their quota configuration first.**

Version 0.9.8.1 republishes the corrected and expanded release notes for 0.9.8
and includes fixes to the CI/CD infrastructure and automation processes, with 
no runtime changes.

### What is new since 0.9.7

- **A quota can now mean no access.** All count, rate, and concurrency limits
  follow the same rule: `0` denies new work, a positive integer sets a limit,
  and `-1` means unlimited. This covers tenant requests per minute and hour,
  concurrent requests, collection and chunk counts, daily collection archives,
  reindex starts, and the search, ingest, archive, and reindex operation pools.
  Request-body, file-ingest, and batch-ingest size ceilings use the same `0` / 
  `-1` convention and still accept fractional megabytes.
- **Tenant policies with explicit exceptions.** Instance defaults apply to
  tenants without an override; an individual tenant can explicitly use `0` to
  deny an operation or `-1` to remove its limit. For example,
  `tenants.default_max_reindex_per_month: 0` disables tenant-initiated reindex
  starts, while selected tenants can be allowed to reindex. Archive budgets use
  a rolling 24-hour window; reindex budgets use a rolling 30-day window. These 
  counters remain persistent across restarts.
- **Refusals behave as limits, not retries.** A zero request-rate allowance
  returns `429 tenant_rate_limited` with no `Retry-After`, because time alone
  cannot reopen the quota. Denied requests do not consume allowance in another
  rate window. A zero chunk allowance rejects ingest before embedding; failed
  quota checks leave an existing document intact rather than deleting it on
  re-ingest. Existing collections and chunks are not removed when a limit is
  set to zero.
- **Metadata survives a round trip.** String metadata values are stored and
  returned without rewriting punctuation, quotes, backslashes, whitespace, or
  SQL-like fragments. For example, a value such as
  `O'Brien; notes -- revised` remains exactly that value rather than being
  altered during ingestion. Metadata *keys* retain their existing normalization.
- **Consistent metadata filtering.** Exact-value filtering and SQLite filter
  pushdown now agree on those preserved values, including document IDs and
  chunk-level metadata that overrides document metadata. Mixed string/numeric
  alternatives no longer lose valid matches when only some values can be
  pushed down. This fixes cases where search could exclude a result that the
  canonical filter would have accepted.
- **Embedded Python client parity.** The local provider now implements the
  SDK collection operations added in 0.9.7: creation with `search_mode`,
  `chunking`, and `priority_key`; per-collection archive export and restore
  (including replacement); and collection reindex initiation, job inspection,
  and cancellation. The same SDK collection workflows can run against a local
  data directory without an HTTP server.

### Configuration and correctness

Invalid quota settings now fail validation instead of silently weakening a
limit: booleans, fractional or string-valued counts, and integers below `-1`
are rejected. Size ceilings reject invalid or non-finite values; finite
fractional-MB limits remain supported. A null per-tenant override inherits the
instance default, and `ingest.max_batch_size_mb: null` continues to inherit the
single-file ingest ceiling. Operator/admin maintenance privileges are unchanged.

### Upgrade note — action required

**An explicit `0` in a 0.9.7 quota meant unlimited; in 0.9.8 it means no
allowance.** Before upgrading:

1. Back up `config.yml` and `tenants.yml`. Change **only** legacy quota zeros
   that were intended to mean unlimited to `-1`. Review `tenants.default_max_*`
   and named-tenant `max_*` values; `search`, `ingest`, `archive`, and `reindex`
   concurrency settings; and `ingest.max_file_size_mb`,
   `ingest.max_batch_size_mb`, and `server.max_request_body_mb`.
2. Check `PAVEDB_*` environment overrides, Compose/Kubernetes manifests, and
   configuration generators as well as YAML. Where tenant configuration is
   generated (including by a management console), change its source, not just
   the generated file. Keep intentional deny-all zeros and unrelated settings
   such as `search.timeout_ms: 0` unchanged.
3. If documents ingested before 0.9.8 contain metadata values with characters
   previously rewritten (`'`, `;`, `"`, `` ` ``, `\`, NUL, `--`, `/*`, or `*/`),
   **re-ingest those documents** to recover their original values. The upgrade
   does not reconstruct values already altered on disk. Collections without
   affected metadata need no re-ingestion.

No database rewrite or rate-window reset is required for these changes. Do not
apply zero-as-deny settings to a 0.9.7 server: that version interprets them as
unlimited. The full migration checklist and metadata examples are in the
[operations guide](guides/running-it.md#quota-values-and-upgrading-from-097)
and [metadata upgrade note](guides/running-it.md#metadata-values-stored-before-098).

### Python 3.10 End of Life

Support for Python 3.10 will be discontinued in the next minor or major (whichever
comes first) release.

PaveDB will continue to support Python versions provided by Debian stable and
oldstable, currently Python 3.11 and later.

### Links

- [Project website](https://pavedb.org/)
- [GitLab repository](https://gitlab.com/flowlexi/pavedb)
- [GitHub mirror](https://github.com/rodrigopitanga/pavedb)
- [PyPI project](https://pypi.org/project/pavedb/)
