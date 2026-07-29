<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Python API and Runnable Examples Plan (P1-62)

`pavedb-sdk` is the canonical home for the public Python API reference and
fully runnable Python examples used by the PaveDB book and docs.

## Decision

The SDK owns these examples because the public `Client` / `Collection`
interface is their subject. It supports both embedded mode (with `pavedb`
installed) and HTTP mode without duplicating programs across the engine,
website, and book repositories.

PaveDB keeps server configuration, CLI, deployment, recovery, and operational
fixtures. `pavedb-site` renders an examples index. The book keeps short
print-friendly excerpts and points at tagged SDK programs.

## SDK target layout

```
pavedb-sdk/
  PYTHON_API.md
  generate_python_api.py
  examples/
    README.md
    data/
    book/
      ch02_intuition.py
      ch05_ingest.py
      ch09_over_the_wire.py
      lattice/
```

Use the existing root and `examples/` surfaces; do not add a second docs
framework or example package.

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

## Example migration

Move the Python examples currently under `pavedb-book/examples/` into
`pavedb-sdk/examples/book/`:

- the chapter 2, 5, and 9 programs;
- the Lattice retrieval, RAG, and evaluation programs; and
- the small public case-study data required to run them.

Reconcile them with the SDK's existing examples instead of keeping parallel
copies. Every program must:

- use only released public APIs;
- run from a fresh checkout with documented commands;
- avoid checkout-relative data outside `examples/`;
- clean up temporary state or name persistent output explicitly; and
- use license-clean sample data with provenance recorded beside it.

The Elixir chapter example moves to `pavedb-client-elixir`. The LibreDWG/CAD
example remains separate because its native dependency does not belong in
either Python distribution.

## Packaging

- Include `PYTHON_API.md`, `examples/`, and example data in the SDK sdist.
- Exclude them from the wheel unless a small installed example is genuinely
  self-contained and tested as installed package data.
- Remove the current claim that checkout-relative demo data is available to an
  installed wheel.

## Book handoff

After the SDK examples pass CI and a release tag exists:

1. the book verifies each substantial excerpt against its canonical program;
2. references use the SDK tag and repository path;
3. duplicate runnable Python files leave the book repo; and
4. the book build and editorial checks remain green.

This handoff is tracked as BK-09 in the book ROADMAP.

## CI

The SDK CI must:

1. run unit tests and `generate_python_api.py --check`;
2. build and Twine-check sdist/wheel artifacts;
3. assert the expected documentation/example contents;
4. install the wheel in a clean environment; and
5. run all canonical examples against the supported PaveDB release, starting a
   real HTTP server for transport examples.

Mock-only success is insufficient for examples advertised as runnable.

## Acceptance

P1-62 is complete when:

- `PYTHON_API.md` is generated, current, and present in the SDK sdist;
- the migrated Python/book examples run in CI from documented commands;
- no example relies on a missing checkout-relative file;
- the wheel stays limited to justified runtime content; and
- the book and website reference the tagged SDK sources without maintaining
  another runnable copy.
