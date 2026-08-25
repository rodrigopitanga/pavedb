<!-- (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# PaveDB — Retrieval you can inspect. Infrastructure you can own.

PaveDB is an inspectable retrieval database for AI applications. By default,
text searches keep their source provenance, stored query record, and replay
trail.

Start embedded in Python, self-host the same engine over HTTP, or point the same
clients at [managed PaveDB](https://cloud.flowlexi.com/). The API stays stable
and the archive remains portable, so the prototype is not a throwaway and the
hosted path keeps an exit.

## Highlights
- One engine and interface from embedded prototype to self-hosted or managed
  production
- Query history and replay for text searches, enabled by default
- Deterministic provenance: every text-backed hit returns a document id,
  snippet, and source-specific page, row, or offset
- Multi-tenant collections: `/collections/{tenant}/{name}`
- Upload and search TXT, CSV, and PDF
- Metadata filters on search (`{"filters": {"docid": "DOC-1"}}`)
- Embedded Python, REST, and CLI entry points
- Health/metrics endpoints + Prometheus exporter
- Local and hosted embedding choices; default stack is FAISS + SBERT

## Requirements
- Python 3.10–3.14

## Install (PyPI)
```bash
python -m venv .venv
source .venv/bin/activate
pip install pavedb
```

CPU-only deployments can use the PyTorch CPU wheel index:
```bash
pip install "pavedb[cpu]" \
  --index-url https://download.pytorch.org/whl/cpu \
  --extra-index-url https://pypi.org/simple
```

## Quickstart

Embedded, with no server or config:

```python
from pavesdk.client import connect

db = connect("./data")
books = db.create_collection("books")
books.add("Captain Nemo commands the Nautilus.", docid="note-1")
hits = books.search("submarine captain", k=3)
```

Serve the same engine over HTTP:

```bash
# Start the server (installed entry point)
pavesrv

# Or run with uvicorn manually if you prefer:
uvicorn pave.main:app --host 0.0.0.0 --port 8086
```

Auth defaults to `static`. `none` is refused outside dev mode. For production,
set a key:
```bash
export PAVEDB_AUTH__MODE=static
export PAVEDB_AUTH__GLOBAL_KEY="your-secret"
```

## Minimal config (optional)
By default PaveDB runs with sensible local defaults. For a user install,
customize `~/pavedb/config.yml`:
```yaml
vector_store:
  type: faiss
embedder:
  default: native
  instances:
    native:
      type: sbert
auth:
  mode: static
  global_key: ${PAVEDB_GLOBAL_KEY}
```
Then export:
```bash
export PAVEDB_GLOBAL_KEY="your-secret"
```
If you keep the file elsewhere, point the runtime at it explicitly:
```bash
export PAVEDB_CONFIG=/path/to/config.yml
```

## CLI example
```bash
pavecli create-collection demo books
pavecli ingest demo books demo/20k_leagues.txt --docid=verne-20k \
  --metadata='{"lang":"en"}'
pavecli search demo books "captain nemo" -k 5
```

## REST example
```bash
# Create a collection
curl -X POST http://localhost:8086/v1/collections/demo/books \
  -H "Authorization: Bearer your-secret"

# Upload a TXT document
curl -X POST http://localhost:8086/v1/collections/demo/books/documents \
  -H "Authorization: Bearer your-secret" \
  -F "file=@demo/20k_leagues.txt" -F "docid=verne-20k" \
  -F 'metadata={"lang":"en"}'

# Search (GET, no filters)
curl -G --data-urlencode "q=hello" \
  -H "Authorization: Bearer your-secret" \
  http://localhost:8086/v1/collections/demo/books/search

# Search (POST, with filters)
curl -X POST http://localhost:8086/v1/collections/demo/books/search \
  -H "Authorization: Bearer your-secret" \
  -H "Content-Type: application/json" \
  -d '{"q":"captain nemo","k":5,"filters":{"docid":"verne-20k"}}'
```

## License
AGPL-3.0-or-later — (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
