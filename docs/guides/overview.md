<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# PaveDB in one page

PaveDB is an inspectable retrieval database. You hand it files, it chunks,
embeds and indexes them, and it answers semantic queries with the exact snippet
that matched and where it came from. By default, text searches keep their query
record and replay trail as well as their source provenance.

Use the same engine embedded in Python, self-host it over HTTP, or point the
same clients at managed PaveDB. One interface carries the prototype into
production without replacing the retrieval stack.

When you run it yourself: one process, one data directory. No cluster to
operate, no external database.

## The model

Four nouns, in order of containment:

| | |
|---|---|
| **Tenant** | A namespace. Carries its own API key, quotas and data directory. |
| **Collection** | A set of documents that share one embedding model. Search happens inside one collection. |
| **Document** | What you ingested — a PDF, CSV, TXT, or a string you passed directly. Identified by a `docid` you choose. |
| **Chunk** | The embedded return unit. Carries source location and metadata. |

A collection pins its embedder when you create it. That binding is enforced:
vectors produced by a different model are rejected rather than mixed into an
index where they would silently degrade results.

By default, each text search is written to a **query log** with its parameters,
timing and result ids, and can be **replayed** later. Raw-vector searches are
not replayable because their query vectors are not stored.

## The pieces

You will touch some subset of these; they all drive the same service layer.

- **`pavesrv`** — the HTTP server. The REST API is the wire protocol everything
  else speaks.
- **`pavecli`** — the CLI. Same operations, useful for setup, inspection and
  scripting.
- **Python SDK** (`pavedb-sdk`) — `connect()` returns handles for collections
  and searches. Works against a URL, or against a local directory with no
  server at all.
- **Elixir client** — HTTP only.
- **Store and embedder** — internal, but you choose them by configuration: which
  vector backend holds the index, which model produces the vectors.

If you are deciding where to start: use the local CLI for an offline instance,
or run the server and drive it with an HTTP client.

## Doing the basic things

The shortest useful path, in any of the interfaces:

1. **Create a collection** — choose a name and, if you do not want the default,
   an embedder.
2. **Ingest** — hand it a file or a string with a `docid`. Re-ingesting the same
   `docid` replaces that document; no restart, no reindex step.
3. **Search** — send a query and a `k`. Each hit carries its score, its text and
   the provenance of the chunk it came from.
4. **Inspect** — for a logged text search, pull the query back out, inspect it,
   and replay it.

Concrete commands and payloads live in the CLI reference and the OpenAPI
document; this page deliberately does not repeat them.

## Running it for real

Three things decide whether a deployment is sound:

- **Auth.** `auth.mode=static` with a key per tenant. The server refuses to
  start unauthenticated outside dev mode — that refusal is the feature.
- **The data directory.** One directory holds every tenant, collection, index
  and log. One supported local entry point owns it at a time. Back it up as a
  unit; it is the whole state.
- **Health.** `/health/live` answers without touching the model;
  `/health/ready` verifies the data directory is writable. Point your
  orchestrator at the second one.

## Where to go next

| You want | Go to |
|---|---|
| Every config key, default and env var | `docs/reference/config.md` |
| Every command and flag | `docs/reference/cli.md` |
| Every endpoint, request and response | `openapi.json` |
| The embedder and backend contracts | `docs/reference/protocols.md` |
| Why it works this way, and the hard parts | the PaveDB book |
| To change PaveDB itself | `CONTRIBUTING.md` |

The references above are generated from the code, so they do not drift. This
guide is written by hand and stays deliberately short: it orients you, the
references answer you, and the book goes deep.
