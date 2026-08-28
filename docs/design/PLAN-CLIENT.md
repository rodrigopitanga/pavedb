<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Client / Library Plan (P1-15 + P1-24 + P1-75 + P2-32)

PaveDB's core principle: **the service layer is the API; HTTP is
just one transport.** This plan defines a single client interface
that every transport implements, so caller code is identical
whether PaveDB runs in-process or behind a server.

## Distribution

Package split:

- `pavedb-sdk`: Python distribution containing the public
  SDK API, `HttpClient`, transport-neutral `Client` /
  `Collection` contracts, exceptions, connection dispatch, and
  shared request/response helpers. It must not import server,
  store, service, or embedder modules at import time.
- `pavedb`: server/local-engine distribution. Depends on
  `pavedb-sdk` and provides the local provider that backs
  `LocalClient` with `pave.service`, `LocalStore`, and embedders.

Install behavior:

```bash
pip install pavedb-sdk
# HTTP client only

pip install pavedb-sdk pavedb
# HTTP client plus local persisted engine
```

There is no package dependency cycle. `pavedb` depends on
`pavedb-sdk`; `pavedb-sdk` discovers local mode through an
optional provider only when a caller asks for a local target.

SDK package versions are a linear SDK release line, not mirrored
PaveDB core versions. SDK `0.1.x` targets the PaveDB `/v1` API;
the SDK exposes the API prefix as `PAVEDB_API_PREFIX` and its own
release as `__version__`.

Runtime flow:

```text
pavesdk.client.connect("https://host")
  -> HttpClient
  -> PaveDB HTTP API

pavesdk.client.connect("./data")
  -> discover local provider registered by installed `pavedb`
  -> pave.localclient.LocalClient
  -> pave.service
  -> LocalStore / CatalogDB / CollectionDB / FaissBackend / embedders
```

If only `pavedb-sdk` is installed, local targets fail with
`LocalClientUnavailable` (`localClientUnavailable`) telling the user
to install `pavedb`.

History note: P1-15 first landed the local handle API inside
`pave/client.py` and exposed `pave.connect()`. After the standalone
SDK split, transport-neutral pieces moved to `pavesdk`; public
`connect()` lives only in `pavesdk.client`, and `pavedb` keeps only
the service/store-backed `LocalClient` in `pave/localclient.py`.
The core package registers local mode through the
`pave.localclient:provider` entry point so the client wheel does not
directly depend on the local engine package.

Sequence:

- **P1-15 — Embedded/library mode (v1.0).** In-process
  `LocalClient` + `Collection` over `service.py`. No uvicorn.
- **P1-24 — Python HTTP client (v1.0).** `HttpClient`
  implementing the same `Client`/`Collection` interface — mimics
  the library. This is the *next* item after P1-15.
- **P1-75 — local data-directory ownership fence (v0.9.6).**
  One process owns a local data directory; competing server, CLI,
  or local-client opens fail before constructing `LocalStore`.
- **P2-32 — remote CLI usability evaluation (v1.1).** Evaluate
  whether a thin CLI over the SDK helps Cloud agents and
  orchestrators manage several PaveDB sites.

## Object model — handle-based

Two levels: `Client` → `Collection`. A `Collection` handle carries
`(tenant, name)` so methods never repeat them.

```python
from pavesdk.client import connect

db = connect("./data")
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

## `pavesdk.client.connect()` — scheme-based dispatch

`pavesdk.client.connect()` is thin sugar over explicit constructors
(`LocalClient(path)`, `HttpClient(url)`); it picks one by URL
scheme. **No host/path heuristics** — a bare string is always a
path, a network target always needs a scheme. Ambiguity here
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

Clients are context managers (`with connect() as db:`) with
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

## Local data-directory ownership — P1-75

`LocalStore` coordinates threads inside one process. Its FAISS
cache, SQLite handles, catalog, and chunk sidecars do not form one
cross-process transaction, so a second process must never open the
same live directory.

The supported local owners take one non-blocking OS lease:

- the `pavesrv` supervisor holds it for the server lifetime;
- `pavecli` holds it while a command uses its direct local store;
- a persistent `LocalClient` holds it until `close()`.

The lease file sits beside the resolved data directory, not inside
it, because whole-instance restore replaces the directory contents.
It is never a PID-based source of truth and is not removed on
shutdown; the kernel lock releases on process exit. There is no
force switch or read-only bypass. Tests may still construct
`build_app()` directly, and dev multi-worker mode remains explicitly
unsafe; neither is a supported second local owner.

## Remote CLI usability evaluation — P2-32

The CLI today prints the full `{ok, latency_ms, timing, query_id,
request_id, …}` JSON envelope, a tested contract (`test_cli.py`,
`test_cli_reference.py`). The SDK now provides local and HTTP
transports, but core PaveDB must not import or construct its HTTP
client.

The v1.1 evaluation starts from agents and orchestrators managing
several Cloud sites through stable, non-interactive commands. It
must decide whether a CLI adds a useful process boundary over
calling the SDK directly, and whether `--host` is the right target
selection UX. The minimum shape is one explicit site per invocation,
deterministic endpoint and credential selection, machine-readable
output, bounded timeouts, and preserved exception-to-exit-code
behavior. It is not a fleet scheduler or credential store.

If the evaluation justifies delivery, the HTTP-capable command shell
belongs in `pavedb-sdk`; PaveDB keeps the local provider. Preserve the
current CLI envelope rather than adding a raw escape hatch to the
public client API solely for CLI compatibility.
