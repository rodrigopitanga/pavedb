<!-- (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# 👾 Contributing to PaveDB

PaveDB accepts code and docs from people who ship patches. Follow the steps below and
keep PRs focused.

## Environment setup

```bash
# clone and enter the repo first
git clone https://gitlab.com/flowlexi/pavedb.git
cd pavedb

# default torch wheel (CUDA on Linux); TORCH_WHEEL_CPU=1 for the CPU-only wheel
make install-dev

# optional: run the service right away
TORCH_WHEEL_CPU=1 make serve
```

**macOS:** `brew install make bash` and invoke `gmake` instead of `make`.
The system `/usr/bin/make` is GNU Make 3.81 (2006) and `/bin/bash` is
3.2 (2007); the Makefile requires Make 4+ and bash 4+, and aborts at
parse time otherwise.

`make serve` in the source tree runs with `DEV=1`, so it uses defaults and explicit
env overrides. If you need to exercise a file-based config from the checkout, pass it
explicitly, for example `CONFIG=./config.yml make serve` or
`PYTHONPATH=. python -m pave.main --config ./config.yml --tenants ./tenants.yml`.

The packaged entrypoints now also accept explicit instance paths:
`pavesrv --home ~/pavedb-staging` and `pavecli --home ~/pavedb-staging list-tenants`.

Run the test suite before pushing (`TORCH_WHEEL_CPU=1` if you installed CPU wheels):

```bash
# TORCH_WHEEL_CPU=1 if you installed CPU-only deps
make test
```

Need to inspect behaviour without reloads? Run
`DEV=0 AUTH_MODE=static GLOBAL_KEY=<your-secret> make serve` for an almost
production-like stack, or call the server module directly:

```bash
PAVEDB_AUTH__GLOBAL_KEY=<your-secret> PYTHONPATH=. python -m pave.main \
  --config ./config.yml --tenants ./tenants.yml
```

## Workflow

1. Fork and clone the repository on GitLab.
2. Create a branch with the matching prefix: `feat/tenant-search`,
   `bug/csv-metadata`, or `rev/r3-26-cli-filters`.
3. Make the change, keep commits scoped, and include tests whenever applicable.
4. Run `make test` and `make check` before submitting.
5. Open a GitLab merge request referencing the issue you claimed.
6. Merge requests must pass the GitLab pipeline before they merge to `main`.

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
- Use Python 3.10+ syntax (e.g. `dict` instead of `Dict`).

### Typing style

- Type public contracts and cross-module boundaries: schemas, protocols, ABCs,
  dataclasses, store/backend/embedder interfaces, and functions with non-obvious
  return shapes.
- Prefer useful annotations over full coverage. Small private helpers, local
  callbacks, tests, fixtures, and FastAPI route handlers do not need annotations
  when the value is obvious from the body or framework declaration.
- Avoid adding `dict[str, Any]` mechanically. Use a named alias, `TypedDict`,
  Pydantic model, dataclass, or a more precise collection type when the shape is
  stable and important.
- Treat loose JSON-like aliases as vocabulary, not strong typing. Future Python
  client types should come from `pave/schemas.py`, OpenAPI, or SDK-specific
  request/response types, not internal store/service aliases.
- Keep `-> None` on public side-effect methods and lifecycle hooks. Do not add it
  just to satisfy annotation coverage in small tests or one-off helpers.
- Let local variables be inferred unless an annotation makes a mixed or nullable
  value easier to understand.

## Commit messages

Use the `[tag]` prefix format for commits that affect functionality:

```
[tag] Short imperative description (≤72 chars)
```

**Style:** One-liners are preferred. A body, when needed, is neutral and
objective, at most 3-4 lines, and never refers to the author, the maintainer,
or tooling. For complex commits, skip a line and add a breakdown:

```
[core] Add collection move across all layers

- Store: abstract method + FaissStore implementation with deadlock-safe locking
- Service: move_collection() with error handling
- API: POST /admin/collections/{tenant}/{name}/move endpoint
- CLI: move-collection command
```

**Available tags** (mapped to changelog sections):

| Tag | Changelog Section | Use for |
|-----|-------------------|---------|
| `[core]` | Core | Cross-cutting features, service logic layer |
| `[sdk]` | SDK | Python SDK package, client interface, and SDK repo changes |
| `[api]` | API | REST endpoints, request/response schemas |
| `[cli]` | CLI | Command-line interface changes |
| `[store]` | Store | Vector store backends, indexing |
| `[conf]` | Configuration | Configuration management |
| `[fix]` | Bug Fixes | Bug fixes |
| `[perf]` | Performance | Optimizations, benchmarks |
| `[build]` | Build | Build system, dependencies (make, pip) |
| `[pkg]` | Packaging | PyPI/Docker packaging |
| `[doc]` | Documentation | User-facing guides and references |
| `[test]` | Testing | Test suite changes |
| `[bench]` | Benchmarks | Benchmark scripts, results |
| `[infra]` | Infrastructure | CI/CD, deployment scripts |
| `[log]` | Logging | Log streams, observability, metrics |
| `[ui]` | UI | Web UI changes |

**Two tags max**, most relevant first: `[api][cli] Add delete document endpoint`

Use `[sdk]` for commits in the standalone Python SDK repository. The same tag also
applies inside PaveDB when a change exists primarily to support the SDK/client
interface boundary.

**Issue keys:** `R<wave>-<n>` review findings (`R1-103`), `P<n>-<id>` roadmap
and plan items (`P2-28`), and `B<n>`, `F<n>`, `G<n>` bugs, features and goals
(`B76`). A trailing `(KEY)` on the first line closes that issue once the
commit lands:

```
[log] dev stream cleanup (P2-28)
```

To reference an issue without closing it, name the key in the body only
(for example `Follows B79.`). Do not use `Closes:` or `Refs:` trailers, and
never put `(refs KEY)` on the first line.

**Review follow-ups:** a fix from review of a commit is squashed into that
commit, not landed on its own. Keep it separate only when folding would
conflict with unrelated work in between.

**Chores** use `chore(<scope>):`, with a tag above as scope, for anything that
does not belong in the changelog: tests, CI, benchmarks, internal docs and
plans, roadmap edits. A `doc`, `test`, `infra`, or `bench` change that refers
to an issue key is never a chore; it keeps its `[tag]`:

```
chore(test): allow five seconds for cancelled search worker startup
chore(infra): stabilize CI on unpinned runners
chore(docs): update roadmap
```

**Changelog:** only `[tag]` commits are included, filed under their first tag,
so lead with the area: `[store][fix]`, not `[fix][store]`. Use a bare `[fix]`
only for a fix spanning several areas. `chore` commits are skipped.

**Reporters:** an issue reported by someone outside the project carries a
`reported-by::<handle>` label. A commit that closes it carries a
`Reported-by: Name (@handle)` trailer, added by the committer; merge request
pipelines reject a closing commit without it, and `make changelog` thanks
every reporter. Optionally, `make hooks` installs a `commit-msg` hook that
adds the trailer from GitLab when `glab` is set up.

**Commit identity:** configure Git with the project maintainer identity before
committing. Do not rely on fallback machine hostnames or unset Git identity. This
policy applies to PaveDB and the standalone PaveDB SDK repositories.

## Issues and task claims

- [`ROADMAP.md`](ROADMAP.md) lists tasks that need owners.
- Bugs and feature requests can be filed on GitHub; code review and merges happen
  through GitLab merge requests.
- Do not close referenced issues from a branch or open MR. Mention the issue key
  in the trailing `(KEY)` or `(KEY, KEY)` commit subject; default-branch CI
  closes it after merge, including scheduled reconciliation. No closing trailer
  is required. For roadmap `P` keys, the task title must also be fully struck
  through in `ROADMAP.md`; partial work stays open.
- Do not open a separate claim issue: CI syncs every roadmap row to a GitLab
  issue titled `<task ID>: <task>`. Describe the approach on that issue and link
  the merge request there.
- Good first issues live under the `bite-sized` label. Submit a draft PR
  within a few days of claiming.

## Feature plans

Substantial features are designed in `docs/design/` before implementation. Read the
relevant plan before picking up a task from [`ROADMAP.md`](ROADMAP.md).
Plan links are maintained in the `Plan Docs` section of [`ROADMAP.md`](ROADMAP.md).

When writing a new plan, follow the structure in existing documents: objectives,
config schema, ops event schema or data model, implementation notes, files
changed, not-in-scope list.

## Pull request checklist

- [ ] Tests pass locally (`make test`, always add `TORCH_WHEEL_CPU=1`, also run
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
- CLI entrypoints are defined in `pave/cli.py`; source-tree runs use the Makefile
  targets or `python -m pave.cli` / `python -m pave.main`.

## Concurrency model

Supported local entry points first take a non-blocking OS lease on
`.pavedb.lock` inside the canonical data directory. One supported local entry
point owns that directory: `pavesrv` holds the lease for its lifetime,
`pavecli` for one store command, and a persistent `LocalClient` until `close()`.
This is a same-filesystem ownership fence, not a distributed or NFS lease.
Internal tests may still construct `LocalStore` directly, and dev multi-worker
mode remains explicitly unsafe.

Per-collection SQLite + per-collection FAISS gives us portable
collections (dump = zip a directory) but it also means consistency across
collections, the catalog, and the filesystem is enforced in the application,
not by a single transactional store. Three lock layers do the work; reach
for the right one for what you're protecting:

```
Owner lease       (DataDirLease, one supported local entry point)
  └─ State lock        (_StoreStateLock,     reentrant rw)
       └─ Collection lock  (_CollectionReadWriteLock,  reentrant reads)
            └─ DB-internal cvs  (CollectionDB / CatalogDB gates)
                 └─ SQLite WAL (file-level, transparent to us)
```

| Lock | Defined in | Held by |
|---|---|---|
| `DataDirLease` | `pave/data_dir_lease.py` | `pavesrv`, direct `pavecli` store commands, and persistent `LocalClient` instances. |
| `_StoreStateLock` | `pave/stores/local.py` | `restore_archive` / `dump_archive` take the write side; every other op takes the read side via the per-collection helpers. |
| `_CollectionReadWriteLock` | `pave/stores/local.py` | `search` and other reads take `_collection_read_lock`; `index_records`, `purge_doc`, `delete_collection`, `create_collection`, `move_collection` take `_collection_write_lock` (variadic `_collection_write_locks` for multi-collection ops like move). |
| `CollectionDB` / `CatalogDB` internal cvs | `pave/metadb.py` | Taken inside the db classes' `_reader`/`_writer` context managers. Don't take these from `pave/stores/*`. |

Rules we've actually learned the hard way:

1. **Catalog writes happen *inside* the per-collection write lock.** If you
   write to the catalog after releasing the collection lock, a concurrent
   delete/move/restore can leave catalog and disk inconsistent. See
   `create_collection`, `delete_collection`, `move_collection`,
   `index_records` for the pattern.
2. **Reads can nest; writes can't upgrade from reads.**
   `_CollectionReadWriteLock` tracks readers per thread, so
   `search()` may legally re-enter via `_read_collection_db`. A thread
   that holds a read lock and tries to take the write lock will deadlock
   waiting for itself — that's an upgrade, not a reentry, and we don't
   support it.
3. **Pre-check the catalog inside the lock when conflict matters.**
   `move_collection` checks the catalog for a row at the target name
   before touching disk; otherwise a UNIQUE collision surfaces as a 500
   instead of a 409 and disk/catalog end up out of sync.

Catalog-only operations, including persistent request and operation-window rate
charges, take the state read lock through `_with_catalog_retry` before opening or
using a catalog handle. This keeps them outside archive dump/restore's exclusive
checkpoint, cache-flush, and replacement window.

Collection archive restore holds the state write lock while checking destination
chunk quotas and installing the collection, so concurrent restores cannot consume
the same remaining tenant budget. Extraction and archive preflight run outside it.

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

- `make install` — install runtime deps (`TORCH_WHEEL_CPU=1` for CPU-only).
- `make serve` — start the dev server (autoreload, auth=none).
- `make test` — run the full pytest suite.
- `make check` — end-to-end smoke test (ingest + search + delete).
  Reuses `:8086` if active, otherwise starts an ephemeral server.
- `make benchmark` — latency + stress benchmarks (`BENCH_SAVE=1`
  to persist results, `BENCH_TAG=<tag>` for naming).
- `make release VERSION=x.y.z` — bump, test, package tracked
  archives, commit, tag, and push tags. Local PyPI and Docker builds
  are opt-in with `PYPI_BUILD=1` and `DOCKER_BUILD=1`; by default,
  publishing builds are delegated to CI. Set `RELEASE_PUBLISH=1` to
  build and publish locally.
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
up to 16 non-whitespace characters (e.g. `rc1`, `a0`, `b2`). A
four-component `vX.Y.Z.W` tag is a package/image correction release.

| Tag pattern | PyPI | TestPyPI | GitLab Package Registry | Docker |
|-------------|------|----------|-------------------------|--------|
| `vX.Y.Z` | ✓ | — | ✓ | CPU only |
| `vX.Y.Z.W` | ✓ | — | ✓ | CPU only |
| `vX.Y.ZrcN` (N ≤ 999) | — | ✓ | ✓ | CPU only |
| `vX.Y.Z<other>` | — | — | ✓ | — |

In practical terms, the Flowlexi GitLab pipeline does:

- **Every pipeline**: GitLab SAST and Secret Detection, plus at most one
  suite tier (CPU deps). Merge-request pipelines, and branch pipelines with
  no open MR, run the fast tier (`pytest`, the `make test-fast` selection);
  the default branch and every tag run the full suite (`pytest-full`), which
  is the gate that has to be green to release. A branch pipeline whose
  branch already has an open MR runs no tier at all: the MR pipeline
  already covers that push, and running the suite twice per push buys
  nothing. The full suite costs more wall clock than every merge should
  pay, so it runs where coverage is mandatory rather than on each push.
  Whichever tier runs, it runs one at a time across the project, so a
  second pipeline's tier sits in `waiting_for_resource` instead of sharing
  a runner's disk with the first; that wait is not charged against the
  job's own timeout. A push that supersedes a pipeline cancels its tier.
- **`vX.Y.Z` and `vX.Y.Z.W` tags**: publish packages to PyPI and the GitLab
  Package Registry, and publish Docker `-cpu` and `latest-cpu` images.
- **`vX.Y.ZrcN` tags**: publish package to TestPyPI and GitLab Package
  Registry, and publish Docker CPU image only (`-cpu`, `latest-cpu`).
- **`vX.Y.Z<other>` tags**: publish only to GitLab Package Registry.

Every standard stable tag must include two human-reviewed source files:
`docs/RELEASE-vX.Y.Z.md` has the first release H1 and only a non-empty
`## English` section; `docs/RELEASE-vX.Y.Z_pt.md` has the identical first H1
and only a non-empty `## Português (Brasil)` section. Major and minor
releases (`Z == 0`) use `PaveDB X.Y.0 — <human-chosen name>`; patch releases
use exactly `PaveDB X.Y.Z` without a name. Historical PatchVec title exceptions
are retained. Both files
ship in the sdist and stay out of the wheel. CI validates the pair, then
mechanically combines the full English file with the Portuguese file without
its duplicate H1 into the exact Release body on both GitLab and GitHub. CI
never translates or generates prose.

The announcement jobs run only after publication. Retrying updates an existing
release from the same reviewed title and Markdown. `GITHUB_MIRROR_TOKEN` must
have `Contents: write` permission on the GitHub repository, and the mirror job
must have pushed the tag before GitHub announcement. Four-component correction
tags keep their package and CPU-image publication behavior but deliberately
have no announcement file and create no GitLab or GitHub Release object.
