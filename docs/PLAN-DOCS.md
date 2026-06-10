<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Docs Site Plan (P1-53 / P1-46 / P1-47)

Ship the canonical documentation surface for PaveDB. Three
phases, deliberately split so the docs site itself slips later
than the source-of-truth that feeds it:

- **Phase 0 — Source-of-truth plumbing (v1.0, P1-53a–d).**
  Make the codebase auto-render reference content (OpenAPI,
  CLI, config) without needing a docs site to exist. Most of
  the eventual "reference" pages become committed markdown the
  moment Phase 0 lands.
- **Phase 1 — Docs site preview (v1.1, P1-46).** Stand up
  MkDocs Material on top of the already-correct sources. The
  hand-written content shrinks because Phase 0 carries the
  reference pages.
- **Phase 2 — Docs site 1.0 (v1.2, P1-47).** Full coverage,
  versioned via `mike`, custom domain, dev section seeded
  from `mkdocstrings` against the public-seam docstrings
  landed in P1-53d.

The original plan put the site preview at v0.9 and the full
site at v1.0. That was reordered after the strategic decision
to lead v1.0 with library mode (P1-15) and keep the v1.0
release lean: GitHub README + auto-rendered reference files
in-repo, no static site yet. The site lands once the source it
would render is correct and stable.

## Principles

- **Text-first.** Long-form prose and reference tables, not
  marketing hero sections, testimonial carousels, or splash
  animations. Reference sites: PostgreSQL, SQLite, Caddy,
  Litestream. A developer should land on the page and start
  reading in under a second.
- **Static and fast.** Pre-rendered HTML, no JS framework
  required to read the content, client-side search via a
  prebuilt index.
- **User-facing and developer-facing — in the same site, not
  two.** Two top-level sections share the same chrome, search,
  and theme.
- **No bloat.** No analytics scripts, no cookie banners, no
  newsletter CTAs, no "edit this page" widgets that require
  a GitHub account. One banner for version, one for search,
  done.
- **Docs live in-repo.** Source markdown under `pavedb/docs/`.
  Published via CI. No separate docs repo until there's a
  reason.
- **AGPL headers on every source file.** Same policy as code.

## Toolchain

**MkDocs + Material for MkDocs.**

Rationale:
- Markdown-native. Every `PLAN-*.md` already in `docs/` can
  migrate with zero rewrite.
- Zero build-time runtime: `pip install mkdocs mkdocs-material`
  produces static HTML. No Node, no bundler.
- Built-in client-side search (lunr). Version selector,
  dark/light toggle, admonitions all in the base theme.
- Widely used by infra projects (FastAPI, Pydantic, Litestar,
  Home Assistant) so the ecosystem is stable.

Alternatives considered:
- **Hugo + Docsy / Hextra** — fast builds but Go templating
  is a second language for the project. Already used by the
  marketing site (`/home/rodrigo/devel/flowlexi-labs/website`),
  keeping docs separate is fine.
- **Docusaurus** — React-heavy, violates the "text-first, no
  JS required" principle.
- **Sphinx** — Python-native but RST/mixed toolchain adds
  friction for markdown-first contributors.

## Repository Layout

```
pavedb/
  docs/
    PLAN-*.md              # design plans (existing, untouched)
    site/                  # new — published site source
      mkdocs.yml
      index.md             # landing page
      user/                # user-facing section
        quickstart.md
        concepts.md
        auth.md
        ingest.md
        search.md
        filters.md
        inspect.md         # NEW — showcases observability
        operations.md
      dev/                 # developer-facing section
        architecture.md
        service-layer.md
        store-layer.md
        embedder.md
        plugins.md
        internals.md
      reference/
        api.md             # auto-generated from OpenAPI
        config.md          # auto-generated from _DEFAULTS
        cli.md             # auto-generated from pavecli --help
      release-notes.md
      assets/
        favicon.svg        # reused from pave/assets/
        logo.svg
```

The `site/` subdir keeps the new markdown separated from
`PLAN-*.md` design docs. PLAN docs stay as internal specs,
not shipped. Some PLAN content may be rewritten into the
`dev/` section — but only by hand and only when the feature
is shipped.

## Phase 0 — P1-53a–d (v1.0 source-of-truth plumbing)

**Goal:** make the codebase the single source of truth for
every reference fact (API surface, CLI surface, config keys,
architectural seams). The result is a set of in-repo
`reference/*.md` files that are auto-generated from source and
committed; no MkDocs site is required for these to ship.

This phase is the cheapest insurance against the worst docs
failure mode — content drift between code and the site. If
the site doesn't exist yet, drift is impossible. When it does
exist (Phase 1), it renders from already-correct sources.

### Scope

| Sub-item | Output | Source |
|---|---|---|
| P1-53a | OpenAPI completeness pass on every route + Pydantic model | `summary` / `description` / `tags` / `responses` on routes; `Field(..., description=, examples=)` on models. Endpoint is `/openapi.json`. |
| P1-53b | `reference/cli.md` committed in-repo | Every subcommand gets `help` / `epilog`-with-example. `scripts/cli_to_markdown.py` walks subparsers and renders the page. |
| P1-53c | `reference/config.md` committed in-repo + `config.yml.example` rendered from same source | Single (key, default, description, env_var) list in `pave/config.py`; `_DEFAULTS` derived from it. Closes P1-26 (drift CI check) as a side effect. |
| P1-53d | Module + class docstrings on public seams | `pave/stores`, `pave/backends`, `pave/embedders`, `pave/metadb` (`CatalogDB`, `CollectionDB`), `pave/service.py` public functions. Powers `mkdocstrings` in Phase 2. |

### Deliverables checklist (P1-53a–d)

1. OpenAPI completeness audit — every route + Pydantic model
   passes a "spec has summary/description/examples" lint that
   runs in CI (P1-53a).
2. `scripts/cli_to_markdown.py` + `reference/cli.md` checked
   in; `make docs-refresh` regenerates it; CI fails if
   committed file is stale (P1-53b).
3. Single source for `_DEFAULTS` + `config.yml.example` +
   `reference/config.md`, with a CI drift check (P1-53c,
   closes P1-26).
4. Module-level docstring on every file in `pave/stores`,
   `pave/backends`, `pave/embedders`, `pave/metadb`, and
   public functions in `pave/service.py` (P1-53d).
5. Updated `README.md`: shrink to one screen, point at the
   three `reference/*.md` files + OpenAPI / `--help`. No
   quickstart duplicated from a future docs site.

## Phase 1 — P1-46 (v1.1 docs site preview)

**Goal:** publishable static site on top of Phase 0's
already-correct sources. Hand-written content shrinks because
the reference is already done — only the genuinely-human pages
remain (intro, concepts, inspect walkthrough).

### Scope

| Page | Status | Source |
|---|---|---|
| `index.md` | NEW | hand-written, ~200 words |
| `user/quickstart.md` | NEW | install → ingest → search in 5 commands (library mode + server mode) |
| `user/concepts.md` | NEW | tenants, collections, docs, chunks, metadata |
| `user/inspect.md` | NEW | **the flagship page**: query log, replay, chunk inspector, request_id correlation, timing breakdown. Walks one query end-to-end from request to replay. |
| `user/auth.md` | NEW | API keys, admin key, `X-Request-ID` header |
| `user/ingest.md` | NEW | TXT/CSV/PDF, size limits, timeout guidance |
| `user/search.md` | NEW | body + filters + common merge |
| `reference/api.md` | imported | render Phase 0's OpenAPI via `mkdocs-render-swagger-plugin` or equivalent |
| `reference/cli.md` | imported | committed by Phase 0; site just publishes it |
| `reference/config.md` | imported | committed by Phase 0; site just publishes it |
| `release-notes.md` | NEW | v0.9 + v1.0 + v1.1 highlights, upgrade notes |

Not in phase 1: full dev section, custom domain, versioning.
Those land in P1-47.

### Build and publish

- **Local preview:** `mkdocs serve` → localhost:8000.
- **CI job:** new `docs` stage in `gitlab-ci.yml`:
  - Runs on default branch and tags.
  - `pip install -r docs/site/requirements.txt`.
  - `mkdocs build --strict` (fails on broken links).
  - Publishes to GitLab Pages.
- **URL:** GitLab Pages default
  (`flowlexi-labs.gitlab.io/pavedb`) for v1.1. Custom domain
  deferred to Phase 2 (P1-47).

### Deliverables checklist (P1-46)

1. `docs/site/mkdocs.yml` configured with the nav above, the
   Material theme, search, dark/light toggle.
2. Seven hand-written markdown pages (above table).
3. `reference/{api,cli,config}.md` imported from Phase 0
   (already in-repo, no auto-render required at site build
   time beyond OpenAPI rendering).
4. `docs/site/requirements.txt` pinning `mkdocs` and
   `mkdocs-material`.
5. `gitlab-ci.yml` `docs` stage with Pages deploy.
6. `README.md` gains a "Docs: <url>" line near the top
   (currently it points at the in-repo `reference/*.md`).
7. `make docs-serve` target for local preview.
8. AGPL header in every new source file (yaml, md).

## Phase 2 — P1-47 (v1.2 docs site 1.0)

**Goal:** complete coverage suitable for a 1.x public release.

### Additions on top of phase 1

- **Full dev section** — architecture, service layer, store
  layer, embedder, plugin contract, internals. Rendered via
  `mkdocstrings` against the docstrings landed in P1-53d;
  hand-written narrative seeded from `PLAN-STORE.md`,
  `PLAN-SQLITE.md`, `PLAN-OPS-LOG.md`, `PLAN-OBSERVABILITY.md`
  where helpful (rewrite, do not copy).
- **API reference with examples.** Phase 1 renders OpenAPI
  as-is; Phase 2 adds per-endpoint code samples (Python via
  the client package P1-24, curl).
- **Versioned docs.** MkDocs Material supports `mike`-based
  versioning. Publish `latest` plus prior minor lines; version
  selector in the chrome.
- **Custom domain.** e.g. `docs.pavedb.io` via Pages custom
  domain. DNS + TLS config not in this plan.
- **Search improvements.** Client-side search works out of
  the box; consider upgrading to Algolia DocSearch only if
  lunr struggles with the doc size (unlikely at 1.x scope).
- **Diagrams.** A small number of architecture diagrams,
  Mermaid or SVG. No animated infographics.
- **Translations — infrastructure only.** The site structure
  must support additional language subtrees without a
  rewrite. Actual translations (pt-BR is the likely first
  target given the consumer app's market) happen post-1.x
  and do not block P1-47.

### Out of scope (post-1.0)

- Tutorials / cookbook / recipes.
- Marketing site (stays separate at
  `/home/rodrigo/devel/flowlexi-labs/website`). See P3-23
  for the marketing-site ambition; P1-46/P1-47 are functional
  docs only.
- Interactive API playground.
- Docs-driven SDKs (code samples stay hand-written until SDK
  tooling justifies more).

## Not in scope for either phase

| Concern | Why out |
|---|---|
| Marketing copy, feature hero sections | Text-first principle |
| Video or animation | Static principle |
| Analytics / telemetry / cookie banner | Bloat principle |
| CMS or authoring UI | Markdown-in-repo principle |
| Forum / community / comments | Out of project scope |
| Paid-tier docs gating | PaveDB is AGPL; no gated content |

## Verification

### P1-53a–d (Phase 0)

1. `make docs-refresh` regenerates `reference/cli.md` and
   `reference/config.md`; CI fails on a stale committed copy.
2. `reference/config.md` and `config.yml.example` both derive
   from the same source list in `pave/config.py`; a unit test
   asserts they cannot diverge.
3. OpenAPI lint: every route has `summary` + `description`;
   every Pydantic model field has `description`; at least one
   example per request/response model.
4. `pavecli <subcommand> --help` exit-code zero for every
   subcommand; help text is non-empty and includes an example
   in `epilog`.
5. Module docstrings present on every `.py` file in
   `pave/stores`, `pave/backends`, `pave/embedders`,
   `pave/metadb`, and public functions in `pave/service.py`.

### P1-46 (Phase 1)

1. `make docs-serve` → localhost:8000 renders the seven hand-
   written pages plus the three imported `reference/*.md`
   files with working nav and search.
2. `mkdocs build --strict` → zero warnings.
3. GitLab Pages pipeline deploys on tag `v1.1.0`.
4. `curl <pages-url>/user/inspect/` returns the inspect page
   HTML.
5. `rg -n 'google-analytics|gtag|cookie' docs/site/` → no
   matches.

### P1-47 (Phase 2)

1. All P1-46 checks still pass.
2. Dev section covers every seam in `pave/` (one page per
   module boundary), driven by `mkdocstrings` against the
   docstrings landed in P1-53d.
3. `reference/config.md` matches `_DEFAULTS` (CI drift check
   already enforced from Phase 0).
4. Version selector shows at least one prior minor and
   `latest`.
5. Custom domain resolves over HTTPS.
