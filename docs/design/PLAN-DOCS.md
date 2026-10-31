<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Distribution Documentation Plan (P1-46 / P1-47 / P1-53 / P1-62 / P1-66)

PaveDB 1.0 delivers one documentation set through three explicit owners:

- PaveDB owns server, CLI, configuration, OpenAPI, operations, release, and
  supported-extension documentation.
- `pavedb-sdk` owns the public Python API reference and runnable Python/book
  examples.
- `pavedb-site` consumes released documentation and renders the website. It
  does not own product facts or Python introspection.

The source distributions are the handoff boundary. A release is documentable
when its sdist contains the same public sources that the site renders.

Standard stable releases also carry two reviewed source files in the PaveDB
sdist, but not its wheel: `docs/RELEASE-vX.Y.Z.md` has the first release H1
and only `## English`; `docs/RELEASE-vX.Y.Z_pt.md` has the same first H1 and
only `## Português (Brasil)`. CI validates both files, then mechanically
combines the full English file with the Portuguese file without its duplicate H1 into
the single GitLab and GitHub Release body; it never translates or generates
prose. Major/minor titles are named; patch titles are not, and historical
PatchVec title exceptions remain exact. Four-component correction tags publish
the existing package and CPU image only, without announcement files or Release
objects.

## v1.0 adoption documentation (P3-54)

Selected 2026-09-20. Extend the existing overview, running-it, and
basic-operations guides; add a page only when the existing guide cannot hold
the task clearly. Keep product sources here and executable Python in the SDK.

- Explain embedded, HTTP, and managed operation through the same engine.
  State one supported local owner per data directory and distinguish larger
  instances or multiple isolated workloads from a distributed query/index.
- Provide an actionable production checklist: authentication, persistent data,
  resource limits, health/readiness, metrics, query-log retention, backup and
  restore, with commands and configuration from the supported release.
- Document committed collection export/import and same-version restore as
  available in the release accompanying the new copy. Distinguish collection
  portability from whole-instance backups and show the retained contents.
- Teach inspection through query, ranked hits, source/chunk, stored query
  record, corpus change, replay, and comparison against the original log.
  Changed results make retrieval drift visible; identical historical results
  are not the goal. Explain the configured log-retention window and which
  recorded fields remain available after the source changes.
- Link the SDK's full query-replay example (SDK roadmap P1) from the guides.
  Compare result IDs/ranks, scores within the same vector space, and recorded
  provenance. Do not require a new drift service or historical snapshot engine.
- Align product descriptions across release-owned README/package/image
  metadata with inspectable retrieval and embedded/HTTP operation; preserve
  discoverable semantic-search/vector-database terms and measured claim limits.

Acceptance: the guides and example agree on the named release, commands and
links work, the production checklist is usable, and the published site renders
these sources through the existing pinned-sdist pipeline. This is queued
documentation work, not a new runtime feature or a release already performed.

## Original documentation release boundary

The v0.9.3 release freezes the current runtime after one hardening/review
cycle. It does not add this documentation delivery. PaveDB 1.0 then locks the
supported contract by shipping:

1. complete distribution documentation;
2. the generated Python API reference;
3. the rendered `/docs/` site.

P1-66 extends that boundary for v1.1 with tested canonical Python and Elixir
examples. It does not move their ownership into the PaveDB engine package.

## Ownership

| Owner | Owns | Does not own |
|---|---|---|
| PaveDB | Product guides, CLI/config/protocol references, OpenAPI, operations, release/legal docs, packaging checks | Hugo layouts, SDK API duplication |
| `pavedb-sdk` | Python API signatures/docstrings, client compatibility, runnable Python/book examples, example data | Server administration, site rendering |
| `pavedb-site` | Hugo layout, navigation, link/asset transformation, deployment | Product documentation sources, Python imports |
| `pavedb-book` | Explanatory prose and short print excerpts | Canonical runnable programs |

Server configuration or operational fixtures stay in PaveDB. Python programs
using the public `Client` / `Collection` interface live in the SDK because the
same surface drives embedded and HTTP modes.

## PaveDB distribution contents

The PaveDB sdist must contain:

- `README.md`, `ABOUT.md`, `CHANGELOG.md`, and `LICENSE`;
- `THIRD_PARTY_LICENSES.md`;
- `config.yml.example`;
- `openapi.json`;
- `docs/reference/cli.md`;
- `docs/reference/pavecli.1`;
- `docs/reference/config.md`;
- `docs/reference/protocols.md`;
- public user/operations guides added for 1.0; and
- any assets referenced by those public documents.

It must exclude `docs/design/`, ROADMAP, private planning, build output, and
site-renderer configuration.

The PaveDB wheel contains only runtime code/data, required templates/assets,
configuration schema data, and license metadata. Markdown guides and the
OpenAPI snapshot do not need to be installed into `site-packages`.

## SDK distribution contents

The SDK sdist must contain:

- `README.md` and `LICENSE`;
- generated `PYTHON_API.md`;
- `examples/README.md`;
- tested, numbered programs directly under `examples/`; and
- the small, license-clean data required by those examples.

The SDK wheel contains code, types, license metadata, and runtime package data.
Top-level documentation and book examples remain sdist-only. Do not install
examples that depend on checkout-relative files.

P1-62 defines reference generation and P1-66 defines example migration in
[`PLAN-EXAMPLES.md`](PLAN-EXAMPLES.md).

## Generation

The current P1-53 generators remain PaveDB-owned:

- `scripts/cli_to_markdown.py` -> `docs/reference/cli.md` and
  `docs/reference/pavecli.1`;
- `scripts/config_reference.py` -> `config.yml.example` and
  `docs/reference/config.md`; and
- `scripts/openapi_snapshot.py` -> `openapi.json`.

Their committed outputs are checked for drift. `protocols.md` and user guides
are reviewed source documents.

The SDK generates `PYTHON_API.md` from an explicit list of supported public
symbols using Python's standard inspection APIs. The generator records
signatures and docstrings, supports `--check`, and does not walk private
modules opportunistically. This keeps the supported surface intentional and
avoids a documentation-framework dependency.

`pavedb-site` performs no Python import or reference generation. It transforms
the released Markdown and assets into Hugo content.

## Site input contract

For a production build, `pavedb-site` receives exact PaveDB and SDK source
distribution versions. Local preview may read sibling checkouts.

The site:

1. unpacks both sdists;
2. copies only their declared public documentation inputs;
3. maps them under `/docs/guide/`, `/docs/reference/`, `/docs/python/`, and
   `/docs/examples/`;
4. renders with Hugo; and
5. records both source versions in the output.

It never copies `PLAN-*`, `docs/design/`, ROADMAP, or mutable default-branch
content into a production build.

## CI gates

### PaveDB

- Existing generated-reference drift tests stay in the fast test job.
- Build the sdist and wheel once.
- Assert the sdist documentation manifest and wheel runtime-data manifest.
- Run Twine checks and a clean-wheel smoke.
- Release jobs upload the already-verified artifacts rather than rebuilding.

### SDK

- Run tests and the `PYTHON_API.md --check` drift gate.
- Build and Twine-check the sdist and wheel.
- Assert the distribution manifests.
- Install the wheel in a fresh environment and verify version/import behavior.
- Run every canonical example against the supported PaveDB release or a real
  server as appropriate.

### Site

- Pull the two exact sdists.
- Build Hugo.
- Verify `/docs/` exists, internal links/assets resolve, source versions are
  visible, and no design plan is emitted.
- Deploy only the verified `public/` artifact.

## Acceptance

The P1-46, P1-47, and P1-62 documentation boundary is complete when:

1. both sdists contain their declared documentation;
2. both wheels contain no accidental prose/example payload;
3. all generated references are current; and
4. `https://pavedb.org/docs/` renders those exact released sources.

P1-66 adds the runnable-example boundary when:

1. the canonical Python and Elixir examples pass from a fresh
   checkout/environment;
2. their built distributions contain exactly the declared example payload;
   and
3. the book points substantial examples at tagged client paths.
