<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Client / Library Plan (P1-15 + P1-24 + CLI port)

PaveDB's core principle: **the service layer is the API; HTTP is
just one transport.** This plan defines a single client interface
that every transport implements, so caller code is identical
whether PaveDB runs in-process or behind a server.

## Distribution

Package split:

- `pavedb-client`: Python distribution containing the public
  client API, `HttpClient`, transport-neutral `Client` /
  `Collection` contracts, exceptions, connection dispatch, and
  shared request/response helpers. It must not import server,
  store, service, or embedder modules at import time.
- `pavedb`: server/local-engine distribution. Depends on
  `pavedb-client` and provides the local provider that backs
  `LocalClient` with `pave.service`, `LocalStore`, and embedders.

Install behavior:

```bash
pip install pavedb-client
# HTTP client only

pip install pavedb-client pavedb
# HTTP client plus local persisted engine
```

There is no package dependency cycle. `pavedb` depends on
`pavedb-client`; `pavedb-client` discovers local mode through an
optional provider only when a caller asks for a local target.

Runtime flow:

```text
pavedb_client.connect("https://host")
  -> HttpClient
  -> PaveDB HTTP API

pavedb_client.connect("./data")
  -> discover local provider registered by installed `pavedb`
  -> pavedb.local_client.LocalClient
  -> pave.service
  -> LocalStore / CatalogDB / CollectionDB / FaissBackend / embedders
```

If only `pavedb-client` is installed, local targets fail with a
clear `local_client_unavailable` error telling the user to install
`pavedb`.

Implementation note: move transport-neutral pieces out of the
current `pave/client.py` into the client package, but keep the
service/store-backed `LocalClient` implementation in the `pavedb`
distribution. Use Python entry points or an equivalent provider
registry so the client wheel does not directly depend on the local
engine package.

Sequence:

- **P1-15 — Embedded/library mode (v1.0).** In-process
  `LocalClient` + `Collection` over `service.py`. No uvicorn.
- **P1-24 — Python HTTP client (v1.0).** `HttpClient`
  implementing the same `Client`/`Collection` interface — mimics
  the library. This is the *next* item after P1-15.
- **CLI port (sub-task of the HTTP client item).** Move the CLI
  off direct `service.py` calls onto the client abstraction, so
  `pavecli --remote http://…` swaps `LocalClient` for `HttpClient`.

## Object model — handle-based

Two levels: `Client` → `Collection`. A `Collection` handle carries
`(tenant, name)` so methods never repeat them.

```python
import pave

db = pave.connect("./data")
books = db.create_collection("books")
books.ingest("book.txt", docid="verne")
books.add("Captain Nemo commands the Nautilus.", docid="note-1")
hits = books.search("submarine captain", k=3)   # -> list[dict]

books = db.collection("books")                   # fetch a handle
db.list_collections()                            # -> [Collection, ...]
db.collection("books", tenant="acme")            # multi-tenant when needed
```

Tenant lives on the client (default `"default"`), overridable per
call. This is the canonical interface the HTTP client copies 1:1.

## `connect()` — scheme-based dispatch

`connect()` is thin sugar over explicit constructors
(`LocalClient(path)`, later `HttpClient(url)`); it picks one by
URL scheme. **No host/path heuristics** — a bare string is always
a path, a network target always needs a scheme. Ambiguity here
(e.g. `"localhost"`) is a silent footgun, so we refuse to guess.

| call | result | lifetime |
|---|---|---|
| `connect()` | ephemeral local client (temp dir) | deleted on `close()` / `with` exit |
| `connect("./data")` | persistent local client | on disk |
| `connect("http://…")` / `https://` | HTTP client | remote |

`connect("localhost")` is a **path** (`./localhost`), not HTTP. To
reach a local server: `connect("http://localhost:8080")`.

Ephemeral mode is a `tempfile.mkdtemp()` cleaned up on close. It
is not a true zero-disk store. `LocalStore` is built around a
`data_dir` (SQLite catalog, FAISS files, sidecars). The temp-dir
approach gives the same "nothing persists" UX with no store
changes; label it *ephemeral*, not *in-memory*, to stay truthful.

Clients are context managers (`with pave.connect() as db:`) with
`.close()` — required for ephemeral cleanup, used elsewhere to
release handles.

## Error model — exceptions

Methods return clean data on success and raise on failure. An
`HttpClient` maps status codes → exceptions naturally, so
in-process and over-HTTP code are interchangeable. Returning the
service-layer `{ok, error}` dicts would force callers to check
`ok` and be awkward to keep identical across transports.

`PaveError(.code, .message)` base, translated from the service
layer's `error_type` / `ServiceError`:

- `NotFoundError` — `error_type == "not_found"`
- `InvalidRequest` — `error_type == "invalid"`
- `Conflict` — `error_type == "conflict"`
- `Unavailable` — embedder unavailable / overloaded

Service functions both return `ok=False` dicts and raise
`ServiceError` depending on the path; the client normalizes both
into `PaveError`.

`search` returns `list[dict]` of matches (the envelope's
`timing`/`query_id` are still logged and reachable via the query
inspection methods). The HTTP client unwraps the JSON envelope to
the same shape, preserving parity.

## API breadth for P1-15

Principle: **the library surface equals the CLI surface** — both
wrap the same `service.py`. The only CLI commands the library
omits are environment/ops (`init`, `reset-metrics`).

**Core — collections + search**
- `Client`: `create_collection`, `collection(name)`,
  `list_collections`, `list_tenants`, `delete_collection`
- `Collection`: `ingest(file)`, `add(text)`, `add_many(docs)`,
  `search(q, k, filters=, include_common=)`, `get(docid)`,
  `list_documents`, `delete(docid)`

**Inspectability** (the differentiator the roadmap protects)
- `Collection`: `detail()`, `list_chunks(docid)`, `get_chunk(rid)`,
  `get_chunk_content(rid)`
- `Collection`: `queries(limit, offset)`, `get_query(qid)`,
  `replay(qid)`
- `Collection`: `rename(new_name)`, `update(display_name=)`

**Instance** (full CLI parity, cheap)
- `Client`: `dump_archive(path=None)`, `restore_archive(bytes)`

**Out of scope for P1-15**
- Metrics / health / `catalog_metrics` — ops/server concern; add
  later as `client.stats()` if wanted.
- Auth, rate limiting, concurrency gating, request-id tracing —
  HTTP-transport concerns. The library is the trusted in-process
  path.
- A dedicated global/"shared collections" search method —
  `include_common=` stays a `search` kwarg; no separate entrypoint.
- Future store features not built yet (per-collection archive
  P1-54, reindex P1-55).

`add(text)` for in-memory strings has no CLI equivalent but is the
key embedded ergonomic (REPL: add a string, search it). It calls
the service-layer raw text ingest path and stores no filename
metadata.

The HTTP client maps `add(text)` to JSON document ingest on
`POST /documents`, and `add_many(docs)` to batch document ingest
on `POST /documents:batch`. P1-16 is not a separate raw-text API;
raw text is one document payload shape. Do not emulate it through
fake file uploads.

## CLI port — deferred, not now

The CLI today prints the full `{ok, latency_ms, timing, query_id,
request_id, …}` JSON envelope, a tested contract (`test_cli.py`,
`test_cli_reference.py`). Porting it onto `LocalClient` now would
either regress that output or force an "envelope/raw" accessor onto
the fresh public API just to feed an internal caller.

Defer the port to the HTTP-client item, where it actually buys
something (CLI-over-HTTP) and both transports are in hand to design
the rich result object once. Until then no logic is duplicated:
CLI and `LocalClient` both sit on `service.py`. The port is a
separate commit with its own exception→exit-code/JSON-error tests.
