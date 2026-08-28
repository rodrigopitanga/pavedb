<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Components

Everything below drives the same service layer, so an operation behaves
identically whichever one you pick. Choose by how you want to talk to it.

## The server — `pavesrv`

One process. It owns the data directory and exposes the REST API, which is the
wire protocol every networked client speaks. If you are deploying PaveDB, this is
the only thing you deploy.

Start it with `pavesrv`. The packaged `pave.main:app` target rejects direct ASGI
loading because the entry point owns startup policy. Tests should call
`build_app(get_cfg())` directly instead of starting an external server. Custom
ASGI wrappers are unsupported.

## The CLI — `pavecli`

The same operations from a shell: set up an instance, ingest, search, inspect
the query log, take an archive. It works directly against a data directory —
there is no HTTP transport, so it is a local operator tool, not a client.
It refuses to open a directory owned by `pavesrv` or a persistent local Python
client; use an HTTP client to operate on a running server.

Best for setup, one-off inspection and scripting. `pavecli init` writes the
config and tenant files an instance needs.

## The Python SDK — `pavedb-sdk`

Handle-based: `connect()` gives you a client, and collections are objects you
call methods on. It has two transports and the same API on both:

- against a URL — talks HTTP to a server
- against a local directory — runs the engine in your process, no server

Install it alone for HTTP use, or alongside `pavedb` for the embedded mode.

## The Elixir client

HTTP only. Add `pavedb_client` to your Mix dependencies and point it at a
running server.

## Store and embedder

Internal seams, but you pick them by configuration rather than by code:

- the **store** holds chunks, metadata and the vector index on disk
- the **embedder** turns text into vectors — a local model, or a remote
  provider you configure

A collection records which embedder produced its vectors, and refuses vectors
from a different one. That is why swapping a model means a new collection, not
a config edit.

## Which one should I use?

| If you are | Use |
|---|---|
| Deploying PaveDB for others | `pavesrv`, configured per [Running it](running-it.md) |
| Setting up or inspecting an instance | `pavecli` |
| Writing a Python application | the SDK, HTTP transport |
| Prototyping with no infrastructure | the SDK, local directory |
| Writing anything else | the REST API directly |
