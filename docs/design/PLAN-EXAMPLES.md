<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Python API and Runnable Examples Plan (P1-62 / P1-66)

`pavedb-sdk` is the canonical home for the public Python API reference and
fully runnable Python examples used by the PaveDB book and docs.
`pavedb-client-elixir` owns the corresponding Elixir example. P1-62 delivered
the generated API reference; P1-66 owns the example migration, execution, and
distribution checks below.

## Decision

The SDK owns these examples because the public `Client` / `Collection`
interface is their subject. It supports both embedded mode (with `pavedb`
installed) and HTTP mode without duplicating programs across the engine,
website, and book repositories.

PaveDB keeps server configuration, CLI, deployment, recovery, and operational
fixtures. `pavedb-site` renders an examples index. The book keeps short
print-friendly excerpts and points at tagged SDK programs.

## Target layouts

```
pavedb-sdk/
  PYTHON_API.md
  generate_python_api.py
  examples/
    README.md
    data/
    01_intuition.py
    02_ingest.py
    03_over_the_wire.py
    04_lattice_retrieval.py
    05_lattice_rag.py
    06_lattice_evaluation.py

pavedb-client-elixir/
  examples/
    README.md
    01_over_the_wire.exs
```

Use the existing root and `examples/` surfaces; do not add a second docs
framework or example package. Runnable programs live directly under
`examples/`, use a contiguous two-digit prefix, and run in lexical order. Data
may be grouped below `examples/data/`, but runnable programs must not be nested
by book chapter or case study. The numbering is repository-local: the first
Elixir example is `01` even though it corresponds to the third Python example.

## Python API reference

`generate_python_api.py`:

- uses the standard library to inspect an explicit allowlist of public
  functions, classes, methods, and exceptions;
- writes stable Markdown to `PYTHON_API.md`;
- records signatures, concise docstrings, and module ownership;
- supports `--check` for CI drift detection; and
- excludes private helpers and implementation modules by default.

The allowlist is the supported API declaration. Adding a public symbol requires
updating its docstring, the allowlist, and the generated reference in one
change.

## Example migration (P1-66)

Move the Python examples currently under `pavedb-book/examples/` into the SDK's
top-level `examples/` directory:

| Book source | Canonical program |
|---|---|
| Chapter 2 intuition | `pavedb-sdk/examples/01_intuition.py` |
| Chapter 5 ingest | `pavedb-sdk/examples/02_ingest.py` |
| Chapter 9 HTTP transport | `pavedb-sdk/examples/03_over_the_wire.py` |
| Lattice retrieval | `pavedb-sdk/examples/04_lattice_retrieval.py` |
| Lattice RAG | `pavedb-sdk/examples/05_lattice_rag.py` |
| Lattice evaluation | `pavedb-sdk/examples/06_lattice_evaluation.py` |
| Elixir HTTP client | `pavedb-client-elixir/examples/01_over_the_wire.exs` |

Move the small public case-study data required by these programs into
`pavedb-sdk/examples/data/`. The Elixir program uses inline sample records so
the client repositories do not acquire duplicate data fixtures.

Reconcile them with the SDK's existing examples instead of keeping parallel
copies. Every program must:

- use only released public APIs;
- run from a fresh checkout with documented commands;
- avoid checkout-relative data outside `examples/`;
- clean up temporary state or name persistent output explicitly; and
- use license-clean sample data with provenance recorded beside it.

`examples/README.md` in each client repository is the runnable index. It lists
every canonical program once, in numeric order, with the exact command,
required environment variables, expected external services, and cleanup
behavior. CI keeps an explicit expected-program list rather than accepting any
file that happens to match a glob; adding or removing a program therefore
requires an intentional CI and index update.

The LibreDWG/CAD example remains separate because its native dependency does
not belong in either client distribution.

## Runtime contract

- Each program is a real entry point, not a snippet imported only by tests.
- Programs accept service URLs and credentials through documented environment
  variables; CI never rewrites their source to run them.
- Local Python programs use temporary state by default. HTTP programs create
  uniquely named collections and remove them on successful completion.
- Tests may provide small deterministic inputs, but mock transports do not
  satisfy the runnable-program gate.
- An example fails with a non-zero exit when its advertised create, ingest,
  search, or inspection result is absent. Printing a response without checking
  it is not a runnable verification.

## Packaging

- Include `PYTHON_API.md`, `examples/`, and example data in the SDK sdist.
- Exclude `PYTHON_API.md`, `examples/`, and example data from the SDK wheel.
- Remove the current claim that checkout-relative demo data is available to an
  installed wheel.
- Include `examples/README.md` and `examples/01_over_the_wire.exs` in the
  Elixir Hex package. Exclude book source, test fixtures, and generated runtime
  output.

## Book handoff

After the SDK examples pass CI and a release tag exists:

1. the book verifies each substantial excerpt against its canonical program;
2. references use the SDK tag and repository path;
3. duplicate runnable Python files leave the book repo; and
4. the book build and editorial checks remain green.

This handoff is tracked as BK-09 in the book ROADMAP.

## CI (P1-66)

The SDK CI must:

1. run unit tests and `generate_python_api.py --check`;
2. build and Twine-check sdist/wheel artifacts;
3. assert the exact numbered program list, SDK sdist contents, and wheel
   exclusions;
4. install the wheel in a clean environment; and
5. run all canonical examples against the supported PaveDB release, starting a
   real HTTP server for transport examples.

The Elixir client CI must:

1. run formatting and unit tests;
2. build and unpack the Hex package;
3. assert the exact numbered program list and package contents; and
4. run every canonical example with `mix run` against a real PaveDB server.

Example jobs run on merge requests and the default branch. They may share a
server setup helper, but each program gets isolated collection state. Release
jobs consume the already checked artifacts instead of rebuilding an unchecked
package.

Mock-only success is insufficient for examples advertised as runnable.

## Acceptance

P1-62 is complete when:

- `PYTHON_API.md` is generated, current, and present in the SDK sdist; and
- the SDK wheel contains no top-level documentation payload.

P1-66 is complete when:

- both client repositories contain the numbered top-level program sets above;
- every documented command runs in CI against a real local engine or HTTP
  server as appropriate;
- no example relies on data outside its owning repository's `examples/`
  directory;
- the built SDK and Hex artifacts pass the declared inclusion and exclusion
  checks; and
- the book and website reference tagged client sources without maintaining
  another runnable copy.
