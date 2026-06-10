<!-- (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# 👾 Contributing to PaveDB

PaveDB accepts code and docs from people who ship patches. Follow the steps below and
keep PRs focused.

## Environment setup

```bash
# clone and enter the repo first
git clone https://github.com/rodrigopitanga/pavedb.git
cd pavedb

# GPU deps by default; add USE_CPU=1 if you want CPU-only torch wheels
make install-dev

# optional: run the service right away
USE_CPU=1 make serve
```

**macOS:** `brew install make bash` and invoke `gmake` instead of `make`.
The system `/usr/bin/make` is GNU Make 3.81 (2006) and `/bin/bash` is
3.2 (2007); the Makefile requires Make 4+ and bash 4+, and aborts at
parse time otherwise.

`make serve` in the source tree runs with `DEV=1`, so it uses defaults and explicit
env overrides. If you need to exercise a file-based config from the checkout, pass it
explicitly, for example `CONFIG=./config.yml make serve` or
`./pavesrv.sh --config ./config.yml --tenants ./tenants.yml`.

The packaged entrypoints now also accept explicit instance paths:
`pavesrv --home ~/pavedb-staging` and `pavecli list-tenants --home ~/pavedb-staging`.

Run the test suite before pushing (`USE_CPU=1` if you installed CPU wheels):

```bash
# USE_CPU=1 if you installed CPU-only deps
make test
```

Need to inspect behaviour without reloads? Run
`DEV=0 AUTH_MODE=static GLOBAL_KEY=<your-secret> make serve` for an almost
production-like stack, or call the wrapper script directly:
`PAVEDB_AUTH__GLOBAL_KEY=<your-secret> ./pavesrv.sh --config ./config.yml --tenants ./tenants.yml`.

## Workflow

1. Fork and clone the repository (menu above).
2. Create a branch named after the task (`feature/tenant-search`, `fix/csv-metadata`,
   etc.).
3. Make the change, keep commits scoped, and include tests whenever applicable.
4. Run `make test` and `make check` before submitting.
5. Open a pull request referencing the issue you claimed.

## Code style

- Prefer direct, readable Python. Keep imports sorted and avoid wildcard imports.
- Follow PEP 8 defaults, keep line length ≤ 88 characters, and run `ruff` locally if you
  have it installed.
- Do not add framework abstractions unless they solve a concrete problem.
- Avoid adding dependencies without discussing them in an issue first.
- If you add or update a dependency, check license compatibility with `AGPL-3.0-or-later`
  and update [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md).
- For non-trivial new files or substantial edits, update/add copyright headers as needed.
- Add SPDX identifiers when creating new files.
- Use Python 3.10+ syntax (e.g. `dict` instead of `Dict`)

## Commit messages

Use the `[tag]` prefix format for commits that affect functionality:

```
[tag] Short imperative description (≤72 chars)
```

**Style:** One-liners are preferred. For complex commits, skip a line and add a
breakdown:

```
[core] Add collection rename across all layers

- Store: abstract method + FaissStore implementation with deadlock-safe locking
- Service: rename_collection() with error handling
- API: PUT /collections/{tenant}/{name} endpoint
- CLI: rename-collection command
```

**Available tags** (mapped to changelog sections):

| Tag | Changelog Section | Use for |
|-----|-------------------|---------|
| `[core]` | Core | Cross-cutting features, service logic layer |
| `[api]` | API | REST endpoints, request/response schemas |
| `[cli]` | CLI | Command-line interface changes |
| `[store]` | Store | Vector store backends, indexing |
| `[conf]` | Configuration | Configuration management |
| `[fix]` | Bug Fixes | Bug fixes |
| `[perf]` | Performance | Optimizations, benchmarks |
| `[bench]` | Benchmarks | Benchmark scripts, results |
| `[build]` | Build | Build system, dependencies (make, pip) |
| `[pkg]` | Packaging | PyPI/Docker packaging |
| `[doc]` | Documentation | README, docstrings, guides |
| `[test]` | Testing | Test suite changes |
| `[log]` | Logging | Log streams, observability, metrics |
| `[ui]` | UI | Web UI changes |
| `[infra]` | Infrastructure | CI/CD, deployment scripts |

**Two tags max**, most relevant first: `[api][cli] Add delete document endpoint`

**Feature plan reference:** when a commit implements work tracked in `docs/`,
include the plan ID in parentheses at the end of the first line:

```
[log] dev stream cleanup (P2-28)
```

**Chores** use `chore:` for maintenance that doesn't affect functionality:

```
chore: update copyright headers
chore(deps): bump sentence-transformers to 5.x
```

**Changelog:** Only commits starting with `[tag]` or `chore:` are included. Release
commits (`chore(release): vX.Y.Z`) are auto-skipped.

## Issues and task claims

- [`ROADMAP.md`](ROADMAP.md) lists tasks that need owners.
- To claim a task, open an issue titled `claim: <task ID>` and describe the
approach.
- Good first issues live under the `bite-sized` label. Submit a draft PR
within a few days of claiming.

## Feature plans

Substantial features are designed in `docs/` before implementation. Read the
relevant plan before picking up a task from [`ROADMAP.md`](ROADMAP.md).
Plan links are maintained in the `Plan Docs` section of [`ROADMAP.md`](ROADMAP.md).

When writing a new plan, follow the structure in existing documents: objectives,
config schema, ops event schema or data model, implementation notes, files
changed, not-in-scope list.

## Pull request checklist

- [ ] Tests pass locally (`make test`, always add `USE_CPU=1`, also run
without it if you have a properly configured GPU).
- [ ] Inform OS and pip freeze.
- [ ] Docs updated when behavior changes.
- [ ] Dependency changes include a license-compatibility check and an updated
  [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md).
- [ ] PR description states what changed and why.
- [ ] PR is self-contained.
- [ ] If it closes an issue, mention it; if it closes a [ROADMAP.md] item, strike it
  through.

## Benchmarks

- `make benchmark` runs latency + stress benchmarks with tuned defaults. It
  reuses `http://127.0.0.1:8086` when already active; otherwise it starts
  ephemeral local servers with temporary `data_dir` (one clean server per
  bench).
- Save outputs with `BENCH_SAVE=1` and an optional tag:
  `make benchmark BENCH_SAVE=1 BENCH_TAG=sqlite-phase1-before`
- If no tag is provided, a `<branch>-<shortsha>` tag is used.
- Results are saved under `benchmarks/results/` with a UTC timestamp.

Ship code, not questions. If you need help, post logs and the failing command instead of
asking for permission to ask.

## API response policy

- Use HTTP status codes for success vs failure (no 200 for errors).
- Errors must use the standard envelope:
  `{"ok": false, "code": "...", "error": "...", "details"?}`.
- Error `code` values are created in the service layer whenever possible.
- Service raises `ServiceError(code, message)` for exceptional failures.
- API/CLI render the error envelope and preserve HTTP status codes.
- Success responses stay unwrapped (simple payloads).
- Cross-cutting metadata (e.g., `request_id`, `latency_ms`) may appear as
  top-level fields on success and error responses.
- Typed error schema: `ErrorResponse` in `pave/schemas.py` documents the
  envelope for OpenAPI and future client SDKs.

## Architecture

- Stores live under `pave/stores/*` (`LocalStore` today).
- Future vector backend adapters live under `pave/backends/*`.
- Embedding adapters reside in `pave/embedders/*`
  (`SbertEmbedder`, `OpenAIEmbedder`).
- `pave/service.py` wires the FastAPI application and injects the store into
`app.state`.
- CLI entrypoints are defined in `pave/cli.py`; shell shims `pavecli.sh`/`pavesrv.sh`
  wrap the same commands for repo contributors.

## Concurrency model

Per-collection SQLite + per-collection FAISS gives us portable
collections (dump = zip a directory) but it also means consistency across
collections, the catalog, and the filesystem is enforced in the application,
not by a single transactional store. Three lock layers do the work; reach
for the right one for what you're protecting:

```
State lock        (_StoreStateLock,     reentrant rw)
  └─ Collection lock  (_CollectionReadWriteLock,  reentrant reads)
       └─ DB-internal cvs  (CollectionDB / CatalogDB writer/reader gates)
            └─ SQLite WAL (file-level, transparent to us)
```

| Lock | Defined in | Held by |
|---|---|---|
| `_StoreStateLock` | `pave/stores/local.py` | `restore_archive` / `dump_archive` take the write side; every other op takes the read side via the per-collection helpers. |
| `_CollectionReadWriteLock` | `pave/stores/local.py` | `search` and other reads take `_collection_read_lock`; `index_records`, `purge_doc`, `delete_collection`, `create_collection`, `rename_collection` take `_collection_write_lock` (variadic `_collection_write_locks` for multi-collection ops like rename). |
| `CollectionDB` / `CatalogDB` internal cvs | `pave/metadb.py` | Taken inside the db classes' `_reader`/`_writer` context managers. Don't take these from `pave/stores/*`. |

Rules we've actually learned the hard way:

1. **Catalog writes happen *inside* the per-collection write lock.** If you
   write to the catalog after releasing the collection lock, a concurrent
   delete/rename/restore can leave catalog and disk inconsistent. See
   `create_collection`, `delete_collection`, `rename_collection`,
   `index_records` for the pattern.
2. **Reads can nest; writes can't upgrade from reads.**
   `_CollectionReadWriteLock` tracks readers per thread, so
   `search()` may legally re-enter via `_read_collection_db`. A thread
   that holds a read lock and tries to take the write lock will deadlock
   waiting for itself — that's an upgrade, not a reentry, and we don't
   support it.
3. **Pre-check the catalog inside the lock when conflict matters.**
   `rename_collection` checks the catalog for a row at the target name
   before touching disk; otherwise a UNIQUE collision surfaces as a 500
   instead of a 409 and disk/catalog end up out of sync.

If you add a new lock or a new lock-holding path, update this section and
the docstring on the affected lock class.

## Logging conventions

All modules use `log = get_logger()` at module level (no underscore prefix).
Never use `%s` format strings — use f-strings.

**Debug internals** (`log.debug`) — `AREA-EVENT: payload`, all-caps, dash-separated:

```
SEARCH-SQL: query='foo' sql='SELECT ...'
SEARCH-FILTER-POST: {'docid': ['doc1']}
INGEST-PREPARED: 3 chunks ['DOC1::0', 'DOC1::1'] ...
SEARCH-OUT: 5 hits [('DOC1::0', 0.923), ...] ...
```

**Info summaries** (`log.info`) — natural language with `key=value` pairs:

```
search tenant=acme coll=books k=5 hits=3 ms=12.34 req=abc123
ingest tenant=acme coll=books docid=DOC1 chunks=4 ms=234.56
```

**Warnings** (`log.warning`) — full English sentences describing what went wrong and
what action was taken (e.g. "starting fresh", "skipping record").

## Testing

The suite is split into **fast** (default) and **slow** tests:

```bash
make test-fast   # seconds — no model loaded, FakeEmbedder only
make test        # full suite, loads real embeddings for slow tests
make test-relevance  # opt-in public-corpus retrieval regression checks
```

`make test-relevance` is intentionally separate from `make test` for now.
It may download a public Hugging Face dataset and a multilingual
sentence-transformers model on first run.

### Fast vs slow tests

Non-slow tests have `FakeEmbedder` injected automatically by the `conftest`
autouse fixture. `FakeEmbedder` is deterministic and instant, but not
semantically meaningful.

Slow tests use the real `FaissStore` with `SbertEmbedder` and a small
sentence-transformers model (`paraphrase-MiniLM-L3-v2`).

**Mark a test (or a whole module) as slow when it:**

- creates a real `FaissStore` directly (`FaissStore()` in the test/fixture)
- needs real semantic similarity (filter ordering, multilingual, ranking)
- is an end-to-end upload-and-search pipeline test

Add `pytestmark` at the top of the module to mark every test in the file:

```python
import pytest

pytestmark = pytest.mark.slow
```

Or decorate individual tests:

```python
@pytest.mark.slow
def test_semantic_ranking(client): ...
```

### How embedding injection works

The autouse fixture in `conftest.py` checks for the `slow` marker:

| Test type | Embeddings class | Model |
|-----------|-----------------|-------|
| fast (default) | `FakeEmbedder` (monkeypatched) | none |
| `@pytest.mark.slow` | `SbertEmbedder` | `paraphrase-MiniLM-L3-v2` |

Because `FakeEmbedder` is injected at the module level, tests that create
`FaissStore()` directly (instead of using the `app` fixture) **must** be marked
slow — otherwise `FaissStore` will receive `FakeEmbedder` and real semantic
search behaviour will not be exercised.

### Forcing FakeEmbedder in a single test

If you need to unit-test non-embedding logic inside `FaissStore` without
loading a model, patch `get_embedder()` explicitly and skip the slow marker:

```python
from tests.utils import FakeEmbedder
import pave.stores.faiss as store_mod
from pave.stores.faiss import FaissStore

def test_purge_clears_index(monkeypatch, tmp_path):
    monkeypatch.setattr(
        store_mod,
        "get_embedder",
        lambda: FakeEmbedder(),
        raising=True,
    )
    store = FaissStore()
    ...
```

## Makefile Targets

Run `make help` for the full list and flags. Key targets:

- `make install` — install runtime deps (`USE_CPU=1` for CPU-only).
- `make serve` — start the dev server (autoreload, auth=none).
- `make test` — run the full pytest suite.
- `make check` — end-to-end smoke test (ingest + search + delete).
  Reuses `:8086` if active, otherwise starts an ephemeral server.
- `make benchmark` — latency + stress benchmarks (`BENCH_SAVE=1`
  to persist results, `BENCH_TAG=<tag>` for naming).
- `make release VERSION=x.y.z` — bump, test, build, tag, push
  tags. Set `RELEASE_PUBLISH=1` to also publish to PyPI and
  Docker registries.
- `make clean` — remove caches and build outputs (keeps `.venv`).

### Maintainer git remotes

Release defaults expect a GitLab remote. One-time setup:

```bash
git remote add gitlab git@gitlab.com:flowlexi/pavedb.git
git remote -v
```

### If `make release` fails

`make release VERSION=x.y.z` is safe to re-run after fixing the
failure:

- **Failed before commit** (e.g. tests): bumped files are uncommitted.
  Fix and re-run, or `git checkout -- .` to start clean.
- **Failed after commit, before tag**: re-run skips the commit (nothing
  staged) and creates the tag normally.
- **Failed after tag** (e.g. Docker): the tag is auto-deleted on
  failure; re-run recreates it. If the tag survived, re-run prompts
  `Re-tag? [y/N]`.
- **Failed after package upload**: re-run is still safe; uploads use
  `twine --skip-existing`.

## Release tags and GitLab CI

Pushing a tag to the GitLab repository triggers the release pipeline
automatically (no manual steps). Tag format:
`vX.Y.Z` where each component is 1–4 digits, with an optional suffix
up to 16 non-whitespace characters (e.g. `rc1`, `a0`, `b2`).

| Tag pattern | PyPI | TestPyPI | GitLab Package Registry | Docker |
|-------------|------|----------|-------------------------|--------|
| `vX.Y.Z` | ✓ | — | ✓ | CPU + GPU |
| `vX.Y.ZrcN` (N ≤ 999) | — | ✓ | ✓ | CPU only |
| `vX.Y.Z<other>` | — | — | ✓ | — |

In practical terms, the Flowlexi GitLab pipeline does:

- **Every pipeline** (branches and tags): run `pytest` (CPU deps) plus
  GitLab SAST and Secret Detection.
- **`vX.Y.Z` tags**: publish package to PyPI and GitLab Package Registry,
  and publish Docker `-gpu` and `-cpu` images (including `latest-*` tags).
- **`vX.Y.ZrcN` tags**: publish package to TestPyPI and GitLab Package
  Registry, and publish Docker CPU image only (`-cpu`, `latest-cpu`).
- **`vX.Y.Z<other>` tags**: publish only to GitLab Package Registry.
