# PaveDB 0.9.0 — Inspectability

## English

PaveDB 0.9.0, **Inspectability**, is the first release after the PatchVec
rebrand.

### API

- Starts a tentative API freeze.
- Standardizes the response envelope with `ok`, `code`, `error`, `request_id`,
  and `latency_ms`.
- Propagates `X-Request-ID` through the response header, `ops_event`, and
  `query_log`.
- Adds `GET /collections/{t}/{c}/documents[/{docid}]`; listing includes chunk
  counts, and detail is enriched with collection information.

### Inspectability

- Adds a chunk inspector with chunk content, metadata, and its offset in the
  original document.
- Adds a persistent per-collection query log with query text, filters, returned
  IDs, and timing.
- Exposes `query_id` in `SearchResponse`, so replay does not scan the log.
- Replay reruns the query against the current index.
- Adds `GET/POST /v1/admin/queries/{id}[/replay]`; these resolver endpoints
  continue locating the collection after a rename.
- Reports the exact response fields
  `timing.{embed,search,filter,hydrate}_ms`.

### Store and concurrency

- Stores backend and embedder configuration per collection.
- Uses a reentrant read/write lock to allow concurrent searches.
- Moves catalog writes inside the collection write lock, avoiding disk/catalog
  inconsistency.
- Fixes races found by `bench-stress`.

### Benchmarks

The new suites cover every endpoint while the base suite remains comparable.
On a 10-core Apple M4-series machine:

- Search: 1,200 queries at concurrency 42, p50 around 224 ms, around 168 ops/s.
- Mixed stress: 60 seconds at concurrency 8, around 120 ops/s, zero errors.

### macOS port

- Runs SBERT in a subprocess to isolate the FAISS and PyTorch OpenMP runtimes.
- Runs the GNU Make 4+ and Bash 4+ compatibility check at Makefile parse time.

### Next: PaveDB 1.0

The next release brings library mode with or without a server, OpenAPI, CLI,
and configuration-variable reference documentation, and the API freeze.

### Links

- [GitLab repository](https://gitlab.com/flowlexi/pavedb)
- [GitHub mirror](https://github.com/rodrigopitanga/pavedb)
- [PyPI project](https://pypi.org/project/pavedb/)
