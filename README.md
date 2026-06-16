<!-- (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> --> 
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# 🛣️ PaveDB — Vector search you can inspect, replay & deploy in minutes

PaveDB is a single-process vector search engine for AI applications.

It ingests your documents, chunks and embeds them, and gives you semantic search with
full provenance — document id, page, character offset, and the exact snippet that
matched.

Built for developers shipping **RAG (Retrieval-Augmented Generation)** systems, PaveDB
provides a straightforward service for **vector search, embeddings pipelines, and
semantic retrieval**. Embed it in your app, drive it from a CLI, or call its **REST API
from any language** — the same codebase, so you can ship your first version quickly and
keep growing as your application scales. No cluster. No opaque pipelines.

Drop a file in. Search it. See exactly what came back — and why. Minutes after your
first commit.

## ✨ See it in action

Ingest a file, search it, and — the part most engines hide — inspect exactly what
happened and reproduce it later:

```python
import pave

db = pave.connect("./data")
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
```

The query log, replay, and chunk-level provenance are first-class — not bolted-on
debugging — and available the same way from the CLI, the REST API, or any client.

## ⚙️ Why PaveDB

- **Ingest files, not embeddings** — hand it a PDF, CSV, or TXT (more formats to come)
  and PaveDB chunks, embeds, and indexes it. No preprocessing pipeline to build.
- **Full provenance on every hit** — every search result traces back to a document,
  page, and character offset. Latency and request traceability are built into every
  response.
- **Inspect and replay every query** — each search is logged with its params, timing, and
  results; pull it back up, replay it byte-for-byte, and drill into the exact chunk that
  matched. Observability is the product, not an add-on.
- **Multi-tenant by default** — tenant/collection namespacing is built in, not bolted
  on (and transparent when you just don't need it).
- **Embed it, script it, or serve it** — use the Python client (more clients soon), drive
  the CLI, or call the HTTP server from any environment. Same ops, same provenance.
- **Pluggable embeddings** — swap models per collection; wire in local or hosted
  embedding backends.

## 🧭 How to

PaveDB exposes the same operations two ways: through a **client interface** and a plain
**REST API**. The client interface is identical across languages and transports — the
Python client ships today (Elixir and JavaScript next), and from v1.1 the same client also
talks to a remote server over HTTP. There's also a **CLI**. Beneath every networked client
sits the REST API: an open wire protocol you can call directly or wrap in your own client,
in any language. The examples below use the Python client — `pip install pavedb` (Python
3.10–3.14); see [Install options](#-install-options) for CPU-only wheels, Docker, and
source.

### 🐍 From Python

The fastest path to a first search — no server, no config. `connect()` to a directory to
persist, or with no argument for an ephemeral store discarded on close.

```python
import pave

db = pave.connect("./data")              # persistent; pave.connect() for ephemeral
books = db.create_collection("books")

books.ingest("20k_leagues.txt", docid="verne-20k", metadata={"lang": "en"})
books.add("Captain Nemo commands the Nautilus.", docid="note-1")  # index a string

for hit in books.search("captain nemo", k=3):
    print(hit["score"], hit["meta"]["docid"], hit["text"][:60])
```

From v1.1 the same client also reaches a remote server —
`pave.connect("http://your-host:8086")`, unchanged code. Whichever client or transport you
use, every hit carries the same provenance: document id, offset, and the matched snippet.

### ⌨️ From the CLI (local, no server)

`pavecli` runs the full workflow against a local instance — no server process required.

```bash
pavecli init   # create the default instance under ~/pavedb
curl -LO https://raw.githubusercontent.com/rodrigopitanga/pavedb/main/demo/20k_leagues.txt

pavecli create-collection demo books
pavecli ingest demo books 20k_leagues.txt --docid=verne-20k --metadata='{"lang":"en"}'
pavecli search demo books "captain nemo" -k 3
```

Full command reference: [`docs/reference/cli.md`](docs/reference/cli.md).

### 🌐 Server & REST API

Run a server — `pavesrv` (from a PyPI install) or a Docker image (see
[Install options](#-install-options)) — and PaveDB exposes a plain HTTP API on
`http://localhost:8086`. This is the open wire protocol every networked client wraps
(networked clients arrive in v1.1) and the one you'd target to build your own — in any
language. No black box: every route is documented in OpenAPI at `/openapi.json` and
browsable at `/ui`. Create and ingest mirror the CLI. Set an auth key
(`PAVEDB_GLOBAL_KEY`) so the server accepts requests; then a search looks like:

```bash
export PAVEDB_GLOBAL_KEY=super-sekret
pavesrv
curl -H "Authorization: Bearer $PAVEDB_GLOBAL_KEY" \
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

Pass an `X-Request-ID` header to correlate a call across the logs and the response. Health
and metrics live at `/health` and `/health/metrics`.

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
GitLab (CUDA builds publish as `latest-gpu`, CPU-only as `latest-cpu`), then run it with
the API port mapped locally:

```bash
docker pull registry.gitlab.com/flowlexi/pavedb/pavedb:latest-cpu
docker run -d --name pavedb -p 8086:8086 \
  registry.gitlab.com/flowlexi/pavedb/pavedb:latest-cpu
```

The server is now reachable at `http://localhost:8086` — drive it as in
[Server & REST API](#-server--rest-api).

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
`pavecli <command> ... --home /path/to/instance`). See `config.yml.example` for every
setting.

### Logging

PaveDB writes human-readable logs to stderr and optionally emits structured JSON lines
(one per search/ingest/delete) for production observability. Enable the ops stream in
`config.yml`:

```yaml
log:
  ops_log: stdout   # null (off) | stdout | /path/to/ops.jsonl
```

## 📈 Growing your app with PaveDB

The same engine scales from a throwaway prototype to a remote deployment — and your code
barely changes along the way:

1. **Prototype with zero setup.** `pave.connect()` opens an ephemeral store in a temp
   directory, discarded on close. No config, nothing left behind.
2. **Persist locally.** Point at a directory — `pave.connect("./data")` — and your data
   survives restarts. Same code, now durable.
3. **Validate it as a service.** Serve that *exact* directory over HTTP, no copying:
   ```bash
   pavesrv --data-dir ./data
   ```
   Run your staging checks against the real wire protocol your production deploy will
   speak. In v1.1 your app switches transports with one line —
   `pave.connect("http://localhost:8086")`, same interface.
4. **Deploy remotely.** Run the prebuilt Docker image on your server (see
   [Install options](#-install-options)).
5. **Migrate your data.** Snapshot the local instance and restore it into the remote one —
   a point-in-time copy of every tenant and collection:
   ```bash
   # local: snapshot the data dir
   pavecli dump-archive --data-dir ./data --output snapshot.zip

   # remote: load the snapshot into the container, then restart it
   docker cp snapshot.zip pavedb:/tmp/snapshot.zip
   docker exec pavedb pavecli restore-archive /tmp/snapshot.zip
   docker restart pavedb
   ```

## 📚 Reference

- CLI: [`docs/reference/cli.md`](docs/reference/cli.md)

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
