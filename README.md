<!-- (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> --> 
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# 🛣️ PaveDB — Retrieval you can inspect. Infrastructure you can own.

PaveDB is an inspectable retrieval database for AI applications. By default,
text searches keep their source provenance, stored query record, and replay
trail instead of disappearing into an opaque pipeline.

Start in-process with Python, serve the same engine over HTTP on your own
infrastructure, or point the same clients at
[managed PaveDB](https://cloud.flowlexi.com/). The API stays stable and the
archive remains portable, so the prototype is not a throwaway and the hosted
path keeps an exit.

PaveDB ingests your documents, chunks and embeds them, and gives you semantic
search with full provenance — document id, exact snippet, and source-specific
page, row, or character offset. Embed it in your app, drive it from a CLI, or
call its **REST API from any language**. No cluster. No opaque pipelines.

Drop a file in. Search it. See exactly what came back — and why. Minutes after your
first commit.

## ✨ See it in action

Ingest a file, search it, and — the part most engines hide — inspect exactly what
happened and reproduce it later:

```python
from pavesdk.client import connect

db = connect("./data")
books = db.create_collection("books")
books.ingest("20k_leagues.txt", docid="verne-20k")

# 1. search — every hit traces back to its source
hits = books.search("submarine captain", k=3)
hits[0]["meta"]["docid"], hits[0]["meta"]["offset"]   # ('verne-20k', 21000)

# 2. observe — the search was logged automatically, with params and timing
q = books.queries(limit=1)[0]
record = books.get_query(q["query_id"])               # full query-log entry

# 3. replay — re-run that exact query, byte-for-byte, any time later
again = books.replay(q["query_id"])

# 4. live update — re-ingest the same docid to swap content, no restart
books.ingest("20k_leagues_v2.txt", docid="verne-20k")
db.close()
```

The query log, replay, and chunk-level provenance are first-class — not bolted-on
debugging — and available the same way from the CLI, the REST API, or any client.

## ⚙️ Why PaveDB

- **Ingest files, not embeddings** — hand it a PDF, CSV, or TXT (more formats to come)
  and PaveDB chunks, embeds, and indexes it. No preprocessing pipeline to build.
- **Source-specific provenance on text hits** — every result traces back to a
  document and the relevant page, row, or character offset. Latency and request
  traceability are built into every response.
- **Inspect and replay text queries** — with query logging enabled (the
  default), each text search keeps its params, timing, and results. Pull it back
  up, replay it, and inspect the exact chunk that matched. Replaying after the
  corpus changes and comparing against the preserved log is how you observe
  retrieval drift — changed results are evidence, not a defect.
- **Multi-tenant by default** — tenant/collection namespacing is built in, not bolted
  on (and transparent when you just don't need it). Tenants carry their own API keys,
  persistent per-minute and per-hour request limits, concurrency caps, and hard
  collection and chunk quotas, so a single instance can safely serve a shared plan.
- **Embed it, script it, or serve it** — use the Python SDK, the Elixir or
  TypeScript client, drive the CLI, or call the HTTP server from any
  environment. Same ops, same provenance.
- **Pluggable embeddings** — swap models per collection; wire in local or hosted
  embedding backends. Each collection's embedder identity is pinned server-side, so
  vectors from mismatched models are rejected instead of silently poisoning an index.
- **Production-safe by default** — durable storage out of the box, transactional
  forward-only schema migrations, a server that refuses to boot unauthenticated in
  production, and zero telemetry.

## 🧭 How to

PaveDB exposes the same operations through client libraries, a **CLI**, and a
plain **REST API**. The Python SDK supports local embedded and remote HTTP
transports; the Elixir and TypeScript clients use HTTP. The REST API is the wire
protocol beneath every networked client and can be wrapped in any language.
The Python examples below use Python 3.10–3.14; see
[Install options](#-install-options) for CPU-only wheels, Docker, and source.

The standalone Python SDK is published on
[PyPI](https://pypi.org/project/pavedb-sdk/), with a public mirror on
[GitHub](https://github.com/rodrigopitanga/pavedb-sdk-python) and active development
on [GitLab](https://gitlab.com/flowlexi/pavedb-sdk-python). Install it by itself for
HTTP mode, or install it alongside `pavedb` when you want the same handle API backed
by the local embedded engine.

### 🐍 From Python

The fastest path to a first search — no server, no config. Use the SDK
handle API with `pavedb` installed alongside it; local targets are backed by
PaveDB's embedded engine.

```python
from pavesdk.client import connect

db = connect("./data")                   # persistent; connect() for ephemeral
books = db.create_collection("books")

books.ingest("20k_leagues.txt", docid="verne-20k", metadata={"lang": "en"})
books.add("Captain Nemo commands the Nautilus.", docid="note-1")  # index a string

for hit in books.search("captain nemo", k=3):
    print(hit["score"], hit["meta"]["docid"], hit["text"][:60])
db.close()
```

Use the same SDK handle API against a remote server with
`connect("http://your-host:8086")`. Whichever transport you use, every hit
carries the same provenance: document id, matched snippet, and source-specific
page, row, or offset.

### ⚗️ From Elixir

Add `{:pavedb_client, "~> 0.1.0"}` to your Mix dependencies and run
`mix deps.get`. Set `PAVEDB_TOKEN` to the server key; the client connects to a
running PaveDB server:

```elixir
client =
  PaveDBClient.connect("http://localhost:8086",
    api_key: System.fetch_env!("PAVEDB_TOKEN")
  )

{:ok, books} = PaveDBClient.create_collection(client, "books")
{:ok, _} = PaveDBClient.Collection.add(books, "Captain Nemo commands the Nautilus.")
{:ok, %{"matches" => hits}} = PaveDBClient.Collection.search(books, "captain nemo", k: 3)
Enum.each(hits, &IO.inspect/1)
```

The client is published on [Hex](https://hex.pm/packages/pavedb_client), with a public
mirror on [GitHub](https://github.com/rodrigopitanga/pavedb-client-elixir) and active
development on [GitLab](https://gitlab.com/flowlexi/pavedb-client-elixir).

### 🟨 From JavaScript / TypeScript

Install [`@flowlexi/pavedb-client`](https://www.npmjs.com/package/@flowlexi/pavedb-client)
— zero dependencies, plain `fetch`, works from JavaScript (`import` on
Node >= 20, `require` on Node >= 22) with TypeScript types included:

```js
import { PaveDBClient } from "@flowlexi/pavedb-client";

const db = new PaveDBClient({
  baseUrl: "http://127.0.0.1:8086",
  apiKey: process.env.PAVEDB_API_KEY,
});

await db.addDocument("book", "notes", { content: "Nemo commands the Nautilus." });
const hits = await db.search("book", "notes", { query: "submarine captain", top_k: 3 });
```

The client is published on [npm](https://www.npmjs.com/package/@flowlexi/pavedb-client),
with a public mirror on [GitHub](https://github.com/rodrigopitanga/pavedb-client-js)
and active development on [GitLab](https://gitlab.com/flowlexi/pavedb-client-js).

### ⌨️ From the CLI (local, no server)

`pavecli` runs the full workflow against a local instance — no server process
required. It opens the data directory directly and refuses to run while a
server or persistent local Python client owns that directory; use an HTTP
client for a running server.

```bash
pavecli init   # create the default instance under ~/pavedb
curl -LO https://raw.githubusercontent.com/rodrigopitanga/pavedb/main/demo/20k_leagues.txt

pavecli create-collection demo books
pavecli ingest demo books 20k_leagues.txt --docid=verne-20k --metadata='{"lang":"en"}'
pavecli search demo books "captain nemo" -k 3
```

Full [command reference](docs/reference/cli.md).

### 🌐 Server & REST API

Run a server — `pavesrv` (from a PyPI install) or a Docker image (see
[Install options](#-install-options)) — and PaveDB exposes a plain HTTP API on
`http://localhost:8086`. This is the open wire protocol every networked client wraps
and the one you'd target to build your own — in any language. No black box:
the wire contract is committed as [`openapi.json`](openapi.json). Runtime
`/openapi.json` and `/ui` are enabled in dev; in production, set
`PAVEDB_UI__ENABLED=true` to expose them. Create and ingest mirror the CLI. Set
an auth key (`PAVEDB_AUTH__GLOBAL_KEY`) so the server accepts requests; then a
search looks like:

```bash
export PAVEDB_AUTH__GLOBAL_KEY=super-sekret
pavesrv
curl -H "Authorization: Bearer $PAVEDB_AUTH__GLOBAL_KEY" \
  "http://localhost:8086/v1/collections/demo/books/search?q=captain+nemo&k=3"
```

Every hit comes back with the same provenance as the Python and CLI paths, plus latency
and request id:

```json
{
  "matches": [
    {
      "id": "verne-20k::chunk_42",
      "score": 0.82,
      "text": "Captain Nemo conducted me to the central staircase ...",
      "tenant": "demo",
      "collection": "books",
      "match_reason": "semantic",
      "meta": {
        "docid": "verne-20k",
        "filename": "20k_leagues.txt",
        "offset": 21000,
        "lang": "en",
        "ingested_at": "2026-03-07T12:00:00Z"
      }
    }
  ],
  "latency_ms": 12.4,
  "request_id": "req-5f3a-b812"
}
```

Pass an `X-Request-ID` header to correlate a call across the logs and the
response. Health and metrics live at `/health`, `/health/metrics`, and
`/metrics`. `/health` and `/health/live` stay anonymous for probes;
`/health/ready` omits local filesystem paths. Under `auth.mode=static`,
`/health/metrics` and `/metrics` require the admin bearer token. The
Prometheus endpoint includes `pavedb_http_requests_total` labeled by
method, route template, and status class.

## 📦 Install options

### 🐍 PyPI

Install into an isolated virtual environment (**requires Python 3.10–3.14**):

```bash
mkdir -p ~/pv && cd ~/pv  # or wherever
python -m venv .venv-pv
source .venv-pv/bin/activate
python -m pip install --upgrade pip
pip install pavedb
```

> **CPU-only deployments:** the default PyTorch wheel from PyPI includes CUDA
> support (~2 GB). For a leaner, CPU-only torch install, point pip at the PyTorch
> CPU index:
> ```bash
> pip install "pavedb[cpu]" \
> --index-url https://download.pytorch.org/whl/cpu \
> --extra-index-url https://pypi.org/simple
> ```

### 🐳 Docker (prebuilt images)

Pull the image that fits your hardware from the
[Flowlexi Container Registry](https://gitlab.com/flowlexi/pavedb/container_registry) on
GitLab (published images are CPU-only, tagged `latest-cpu` / `<version>-cpu`; build a
CUDA image locally with `make docker-build RELEASE_TORCH=cuda`), then run it with
the API port mapped locally:

```bash
docker pull registry.gitlab.com/flowlexi/pavedb/pavedb:latest-cpu
docker run -d --name pavedb -p 8086:8086 \
  -e PAVEDB_AUTH__GLOBAL_KEY \
  -v pavedb-data:/data \
  registry.gitlab.com/flowlexi/pavedb/pavedb:latest-cpu
```

The named `pavedb-data` volume preserves the instance home (`PAVEDB_HOME=/data`)
across container replacement. The image runs as the unprivileged `pavedb` user
(UID 1000), so a bind-mounted host directory must be writable by that UID. Volumes
from releases before 1.0 were mounted at `/root/pavedb` as root; mount them at `/data` and
`chown -R 1000:1000` their contents. The server is now reachable at `http://localhost:8086` — drive it
as in [Server & REST API](#-server--rest-api).

### 🛠️ From source

Building from source relies on `Makefile` shortcuts (`make install-dev`, `make serve`,
`make test`, `make check`, etc.). The full contributor workflow, target reference, and
coding style live in [CONTRIBUTING.md](CONTRIBUTING.md). Performance benchmarks are
documented in [README-benchmarks.md](README-benchmarks.md).

## 🔧 Configuration

PaveDB runs on sensible defaults with no config file. For CLI or server use, `pavecli init`
creates an instance under `~/pavedb` — a `config.yml` and a `data/` directory. Settings
resolve in order of precedence: the `PAVEDB_*` environment scheme (`PAVEDB_SERVER__PORT`,
`PAVEDB_AUTH__MODE`, `PAVEDB_DATA_DIR`, …) overrides `config.yml`, and `PAVEDB_CONFIG` can
point at an explicit file. For alternate instances, `pavecli init /path/to/instance` and
target it with `pavesrv --home=/path/to/instance` (or
`pavecli --home /path/to/instance <command> ...`). See `config.yml.example` for every
setting. Per-tenant secrets and limits — API keys, moving-window request rates,
concurrency caps, and hard collection/chunk quotas — live in a `tenants.yml` sidecar,
so one instance can run a shared multi-tenant plan with no extra infrastructure.

### Logging

PaveDB writes human-readable logs to stderr and optionally emits structured JSON lines
(one per search/ingest/delete) for production observability. Enable the ops stream in
`config.yml`:

```yaml
log:
  ops_log: stdout   # null (off) | stdout | /path/to/ops.jsonl
```

## 📈 Growing your app with PaveDB

The same engine scales from a throwaway prototype to self-hosted or managed
production — and your code barely changes along the way:

1. **Prototype with zero setup.** `connect()` opens an ephemeral store in a temp
   directory, discarded on close. No config, nothing left behind.
2. **Persist locally.** Point at a directory — `connect("./data")` — and your data
   survives restarts. Same code, now durable — and it survives upgrades too: newer
   PaveDB versions migrate the on-disk schema forward transactionally and refuse
   downgrades loudly instead of corrupting data.
3. **Validate it as a service.** Serve that *exact* directory over HTTP, no copying:
   ```bash
   pavesrv --data-dir ./data
   ```
   Close the embedded client first; one supported local entry point owns a data
   directory at a time.
   Run your staging checks against the real wire protocol your production deploy will
   speak. Your app switches transports with one line:
   `connect("http://localhost:8086")`.
4. **Deploy it yourself.** Run the prebuilt Docker image on your server (see
   [Install options](#-install-options)). A misconfigured deploy fails loudly:
   outside dev mode the server refuses to start without authentication rather than
   quietly exposing your data.
5. **Or use managed PaveDB.** Point the same client at
   [Flowlexi Cloud](https://cloud.flowlexi.com/). It runs the same engine and API;
   you keep the keys, archive, and exit.
6. **Move data without changing formats.** Close the process that owns a local
   data directory before using the offline CLI. Restore into a running remote
   server through its admin API:
   ```bash
   # local: run only after closing the embedded client or server
   pavecli --data-dir ./data dump-archive --output snapshot.zip

   # remote: the server coordinates the live replacement
   export PAVEDB_URL=http://localhost:8086
   export PAVEDB_TOKEN=super-sekret
   curl -X PUT \
     -H "Authorization: Bearer $PAVEDB_TOKEN" \
     -F "file=@snapshot.zip;type=application/zip" \
     "$PAVEDB_URL/v1/admin/archive"
   ```

   Archives carry each collection's vector-space identity, not credentials or
   operational settings such as provider endpoints and batch sizes. Before
   replacing live data, restore validates locally, without loading models or
   making network requests, that every archived vector-space identity is
   supported by the destination configuration. A failed replacement rolls back
   the live tree; an incomplete rollback leaves a recovery marker for repair.

   A single collection can move independently without replacing the rest of
   the instance. The destination tenant and collection come from the restore
   URL, so an archive can be imported under a new namespace:

   ```bash
   curl -H "Authorization: Bearer $PAVEDB_TOKEN" \
     -o books.zip \
     "$PAVEDB_URL/v1/admin/collections/acme/books/archive"

   curl -X POST \
     -H "Authorization: Bearer $PAVEDB_TOKEN" \
     -F "file=@books.zip;type=application/zip" \
     "$PAVEDB_URL/v1/admin/collections/imported/library/archive"
   ```

   Collection archives contain `manifest.json`, `meta.db`, the FAISS index,
   and collection sidecars. Restore accepts the current PaveDB and collection
   schema version only, and resolves the archived embedder specification from
   local configuration before changing the destination collection.

7. **Rebuild a collection's vector space.** Start an admin reindex job when a
   collection must move to another configured embedding model. The live index
   remains unchanged until the staged replacement is complete:

   ```bash
   job_id=$(curl -fsS -X POST \
     -H "Authorization: Bearer $PAVEDB_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"embed_model":"sentence-transformers/all-MiniLM-L6-v2"}' \
     "$PAVEDB_URL/v1/admin/collections/acme/books/reindex" \
     | jq -r .job_id)

   curl -H "Authorization: Bearer $PAVEDB_TOKEN" \
     "$PAVEDB_URL/v1/admin/reindex/$job_id"
   ```

   `POST .../$job_id/pause` and `POST .../$job_id/resume` control a job at
   persisted batch boundaries; `DELETE .../$job_id` cancels it. Paused jobs
   survive restart and refuse to resume if the source corpus changed.

## 📚 Reference

- [Guides](docs/guides/README.md) — start here: what PaveDB is, its components,
  the basic operations, and what an operator has to get right
- [CLI](docs/reference/cli.md) — `pavecli` commands and flags
- [Configuration](docs/reference/config.md) — config fields, defaults, and environment overrides
- [Store protocols](docs/reference/protocols.md) — `Embedder`/backend stability contract

## 🗺️ Roadmap

Short/mid-term tasks and long-term plans are all tracked in
[`ROADMAP.md`](ROADMAP.md). Pick one, open an issue titled `claim: <task ID>`, and
ship a patch. If you find a bug, file it under the *Issues* tab.

## 📜 License

PaveDB is free software: you can use it, copy it, redistribute it and/or modify it
free of charge under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or (at your option) any
later version.

PaveDB is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR
PURPOSE.  See the GNU Affero General Public License for more details.

SPDX-License-Identifier: AGPL-3.0-or-later
Copyright (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
