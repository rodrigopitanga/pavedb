export-queriesexport-queries<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
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

Client foundation and remote CLI evaluation:

- **P1-15 — Embedded/library mode (v1.0).** In-process
  `LocalClient` + `Collection` over `service.py`. No uvicorn.
- **P1-24 — Python HTTP client (v1.0).** `HttpClient`
  implements the shared `BaseClient`/`Collection` interface alongside
  the local provider.
- **P1-75 — local data-directory ownership fence (v0.9.6).**
  One supported local entry point owns a data directory; competing server, CLI,
  or local-client opens fail before constructing `LocalStore`.
- **P2-32 — remote CLI usability evaluation (v1.1).** Evaluation complete;
  remote CLI not implemented. A shell over the SDK helps only callers that
  cannot import Python, belongs in `pavedb-sdk` if it is delivered at all,
  and is never a mode on `pavecli`.

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
the same shape, preserving parity. The P2-32 evaluation found that
reachability claim holds only on a quiet collection: `queries()`
reads the newest log entry, which a concurrent search can win. See
the P2-32 section for the envelope the transport should keep.

## API breadth for P1-15

Principle: **the library surface equals the CLI surface** — both
wrap the same `service.py`. The CLI commands the library omits are
environment/ops (`init`, `reset-metrics`) and the on-disk admin key
(`admin show-key`, `admin rotate-key`).

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
- each `LocalClient`-created store holds it until `close()`.

The lease file sits at `.pavedb.lock` inside the resolved data
directory so bind-mounted aliases share the same fence. Archive dump
omits it, restore rejects an archive-supplied copy, and live restore
preserves it. It is never a PID-based source of truth and is not
removed on persistent shutdown; the kernel lock releases on process
exit. There is no force switch or read-only bypass. Tests may still
construct `build_app()` directly, and dev multi-worker mode remains
explicitly unsafe; neither is a supported second local owner.

## Remote CLI usability evaluation — P2-32

The evaluation is complete; a remote CLI is not implemented. Remote management
already works for Python callers, so a CLI buys a process boundary rather
than a capability. The boundary is worth paying for only outside Python,
and only after the SDK
stops discarding the search envelope. The outcome is a narrow,
conditional yes to a separate command shell in `pavedb-sdk`, and a flat
no to a remote mode on `pavecli`.

### Starting position

`pavecli` prints the full service envelope: `pavecli search` emits
`{ok, matches, mode, latency_ms, timing, request_id, query_id}`
straight from `service.search`, and that shape is a tested
contract — `tests/test_cli.py` asserts `out["ok"]` command by command,
and the generated `docs/reference/cli.md` is pinned by
`tests/test_cli_reference.py`. On the other side,
`pavesdk.client.connect("https://host", api_key=…, timeout=…)` returns an
`HttpClient` implementing the same `Client`/`Collection` interface the
local provider implements, so driving one remote site from Python needs
no new code. Core PaveDB must not import or construct that HTTP client.

### Findings

**F1 — a CLI adds no remote capability, only a process boundary.**
`connect()` plus `HttpClient` already drive a remote instance across the
bulk of the HTTP API (F6 bounds what is still missing), and a CLI adds no
route of its own. What it adds is the boundary: it serves callers that
cannot import Python — shell jobs, CI steps, Makefiles, unit files — and
agent harnesses restricted to spawning processes. Tool-calling agents are
served better by the MCP server (P2-35), and a control plane written in
another language cannot use a Python CLI either; it drives the HTTP API
directly. "Agents and orchestrators" therefore narrows to shell-level
orchestrators: a real audience, smaller than the roadmap line implies.

**F2 — packaging decides where the shell lives.** `pavedb-sdk` depends on
`httpx` alone; `pavedb` pulls `fastapi`, `uvicorn`, `faiss-cpu`,
`sentence-transformers`, `openai`, and `pypdf`. A remote mode on
`pavecli` would make every remote-only caller install the local engine to
reach an instance it never runs. The shell belongs in `pavedb-sdk`.

**F3 — `--host` is the wrong option, twice over.** `server.host`
(`PAVEDB_SERVER__HOST`) already names the address an instance binds, so
`--host` would mean two different things in one product. And a bare host
revives the heuristics `connect()` refuses on purpose: `localhost` is a
path, not an address. The target must carry a scheme, be required, and
name exactly one site per invocation.

**F4 — the envelope gap is an SDK gap, not a CLI gap.**
`HttpClient._json` drops `ok` and `_search` returns `data["matches"]`
only, so every SDK caller loses `mode`, `latency_ms`, `timing`,
`request_id`, and the `query_id` that `replay()` takes as its
argument. Recovering that id from `queries()` is a race under concurrent
search. Preserving the envelope is thus not CLI-compatibility work: the
public search surface cannot reach the inspectability PaveDB sells. Fix
it in the SDK first, and the command shell formats what the transport
already returns — no raw escape hatch on the public client API.

**F5 — exit codes and error metadata need a remote contract.**
`main_cli` already exits 1 for `{ok: false}` results, 130 for a paused
reindex, and 2 for argument errors; successful results exit 0. What is
missing is a distinction between retryable and terminal remote failures.
The server already sends `Retry-After` when throttling (`pave/auth.py`),
and `_error` includes `request_id` and `latency_ms` (`pave/main.py`).
But `HttpClient._raise_data` retains only `code` and `message`, dropping
the HTTP status and diagnostic metadata. SDK callers therefore lack a
reliable status-based retry policy and cannot correlate failures with
server logs. Preserve this information in the SDK before defining the
remote shell's error and exit-code contract.

**F6 — a remote shell must follow shipped HTTP and SDK capabilities.**
The SDK and `pavecli` have different coverage, but the divergence is
not bidirectional to the extent previously claimed. The SDK exposes
tenant provisioning and key methods, although tenant-key routes remain
unshipped (P2-38). The local CLI has `init` and `admin show-key` /
`rotate-key`, which operate on instance files and have no remote
equivalent, plus `reset-metrics` (`DELETE /v1/admin/metrics`), which
lacks an SDK method. Its catalog-wide `list-queries` uses local
`query_home` pointers; P1-51 deliberately excluded a corresponding
HTTP endpoint. Catalog-wide `get-query` and `replay-query` also differ
from the SDK's collection-scoped routes. `move-collection` only renames
within one tenant. Query export (P2-13), document versioning (P2-14),
telemetry (P2-22), bulk deletion (P2-24), and profiles (P2-26) are
unshipped roadmap work, not existing CLI commands. A remote shell must
therefore be a tested subset of supported HTTP and SDK operations,
not a mechanical copy of `pavecli`.

### Decisions

1. **No remote mode on `pavecli`.** The local CLI stays the local owner of
   a data directory (P1-75) and keeps its envelope. `--host` is refused on
   naming (F3) and on packaging (F2).
2. **A delivered shell is its own console script in `pavedb-sdk`,** built
   on `HttpClient`, taking one required scheme-bearing target per
   invocation.
3. **Delivery is conditional and narrow.** Where `curl` against the
   documented API already suffices, ship nothing: the API is public,
   `openapi.json` ships in the repo, and a second command surface owes
   parity maintenance forever (F6). The commands that earn their keep are
   the ones `curl` makes awkward — multipart ingest, binary archive dump
   and restore, and keyset-paged query export.
4. **Credentials never arrive on argv,** which is world-readable through
   `/proc` on Linux: the shell reads the key from a file or stdin, with at
   most one documented environment variable as the fallback. `PAVEDB_` is
   the server's config prefix and its overrides are allowlisted from
   declared config keys, so a client variable parked there is inert but
   misleading; the `PAVEDB_URL`/`PAVEDB_API_KEY` pair used by the SDK
   examples is a convention to decide on, not one to inherit silently.

### Prerequisites before any delivery

In order, all of them SDK-side:

1. The transport preserves the service envelope, so a caller can read
   `query_id`, `timing`, `latency_ms`, `request_id`, and `mode` from a
   search (F4).
2. Errors carry the status class, `request_id`, and `Retry-After`, so a
   caller can separate retryable from terminal (F5).
3. The SDK covers the endpoints `pavecli` already reaches, so the shell's
   subset is a choice rather than an accident (F6) — catalog-wide
   `list-queries`, which has no endpoint by design, stays outside it.

None of the three is CLI work and each improves the SDK on its own.
Scheduling them, and the shell itself, is a roadmap decision in
`pavedb-sdk`, where the code would live.

### Out of scope, confirmed

Fleet scheduling, a site registry, multi-site fan-out, credential
distribution, and failover policy stay outside PaveDB. One invocation
targets one instance; the caller that holds several sites is the one that
iterates over them.
