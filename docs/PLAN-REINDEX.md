<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Per-collection reindex jobs (P1-55)

## Objective

Let an operator rebuild one collection's vectors against a different configured
embedder without changing document or chunk metadata. Reindex is an admin-only,
durable job because encoding can outlive one request or server process.

## HTTP surface

| Method | Path | Effect |
|---|---|---|
| `POST` | `/v1/admin/collections/{tenant}/{name}/reindex` | Create and start a job |
| `GET` | `/v1/admin/reindex/{job_id}` | Read durable state and chunk progress |
| `POST` | `/v1/admin/reindex/{job_id}/pause` | Pause after a persisted batch |
| `POST` | `/v1/admin/reindex/{job_id}/resume` | Resume a paused or failed job |
| `DELETE` | `/v1/admin/reindex/{job_id}` | Cancel and discard staged vectors |

The create body uses the same `embedder_type`, `embed_model`, and
`embedder_config` vocabulary as collection creation. The resolved target must
name a different vector space. Identity/BYOV is not a text reindex target.

## Job model

`CatalogDB.reindex_jobs` stores the source and target collection config, a
source-content digest, progress counters, timestamps, status, and the last
error. At most one unfinished job exists for a collection. States are:

```text
queued -> running -> committing -> succeeded
             |             |
             +-> pausing -> paused -> queued
             +-> cancelling -> cancelled
             +-> failed -> queued
```

Jobs left in an active encoding state by shutdown become `paused` on the next
open. A commit interrupted between the staged/live index rename is completed
when the target index is intact; otherwise the previous index is restored.

## Storage and concurrency

Each job builds a FAISS index in `.pavedb-reindex/{job_id}/index` and flushes
after every bounded batch. Resume trusts the staged index only when its record
IDs form the expected prefix of the source manifest. The source digest covers
the collection vector-space config, chunk IDs, and chunk text, so a paused job
fails closed if its corpus changes.

The worker holds the collection write lock while encoding. Existing searches
finish before the job starts; new collection reads and writes wait until the
job pauses, cancels, or commits. Status and control requests remain available
through the independent catalog lock. Completion swaps the staged index under
the collection lock, updates `collection.json` and the catalog, reloads the
target embedder/backend, then removes the rollback copy.

## Not in scope

- Reconstructing text for BYOV chunks that intentionally have no sidecar.
- Cross-instance or distributed workers; the data-directory owner runs jobs.
- Keeping the collection writable during a vector-space migration.
- Rechunking source documents or changing their metadata.
