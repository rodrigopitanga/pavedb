<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Docs Site Plan (P1-46 / P1-47)

Ship a static docs site for PaveDB. Two phases: a working
preview alongside the v0.9 release (P1-46), and the full 1.0
docs set (P1-47).

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
- **Docs live in-repo.** Source markdown under `patchvec/docs/`.
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
patchvec/
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

## Phase 1 — P1-46 (v0.9 preview)

**Goal:** publishable site that covers the v0.9 feature set,
especially the inspectability surface. Not comprehensive —
just enough that someone landing on the docs understands what
PaveDB is, can install it, ingest a document, run a search,
and inspect the result.

### Scope

| Page | Status | Source |
|---|---|---|
| `index.md` | NEW | hand-written, ~200 words |
| `user/quickstart.md` | NEW | install → ingest → search in 5 commands |
| `user/concepts.md` | NEW | tenants, collections, docs, chunks, metadata |
| `user/auth.md` | NEW | API keys, admin key, `X-Request-ID` header |
| `user/ingest.md` | NEW | TXT/CSV/PDF, size limits, timeout guidance |
| `user/search.md` | NEW | body + filters + common merge |
| `user/inspect.md` | NEW | **the flagship v0.9 page**: query log, replay, chunk inspector, request_id correlation, timing breakdown. Walks one query end-to-end from request to replay. |
| `reference/api.md` | auto | from `/openapi.json` via `mkdocs-render-swagger-plugin` or equivalent |
| `release-notes.md` | NEW | v0.9 highlights, upgrade notes |

Not in phase 1: dev section, CLI reference, config reference,
detailed operations guide. Those land in P1-47.

### Build and publish

- **Local preview:** `mkdocs serve` → localhost:8000.
- **CI job:** new `docs` stage in `gitlab-ci.yml`:
  - Runs on default branch and tags.
  - `pip install -r docs/site/requirements.txt`.
  - `mkdocs build --strict` (fails on broken links).
  - Publishes to GitLab Pages.
- **URL:** GitLab Pages default
  (`flowlexi-labs.gitlab.io/patchvec`) for v0.9. Custom domain
  deferred to P1-47.

### Deliverables checklist (P1-46)

1. `docs/site/mkdocs.yml` configured with the nav above, the
   Material theme, search, dark/light toggle.
2. Seven new hand-written markdown pages (above table).
3. Auto-API page wired from OpenAPI at build time.
4. `docs/site/requirements.txt` pinning `mkdocs` and
   `mkdocs-material`.
5. `gitlab-ci.yml` `docs` stage with Pages deploy.
6. `README.md` gains a "Docs: <url>" line near the top.
7. `make docs-serve` target for local preview.
8. AGPL header in every new source file (yaml, md).

## Phase 2 — P1-47 (v1.0 full)

**Goal:** complete coverage suitable for a 1.0 public release.

### Additions on top of phase 1

- **Full dev section** — architecture, service layer, store
  layer, embedder, plugin contract, internals. Seed from
  `PLAN-STORE.md`, `PLAN-SQLITE.md`, `PLAN-OPS-LOG.md`,
  `PLAN-OBSERVABILITY.md` — rewrite, do not copy.
- **Reference pages auto-generated:**
  - `reference/config.md` from `_DEFAULTS` in `pave/config.py`.
  - `reference/cli.md` from `pavecli --help` subcommand
    output.
  - `reference/api.md` expanded with code samples per
    endpoint (Python, curl).
- **Versioned docs.** MkDocs Material supports
  `mike`-based versioning. Publish `latest` and `0.9`;
  version selector in the chrome.
- **Custom domain.** e.g. `docs.pavedb.io` via Pages custom
  domain. DNS + TLS config not in this plan.
- **Search improvements.** Client-side search works out of
  the box; consider upgrading to Algolia DocSearch only if
  lunr struggles with the doc size (unlikely at 1.0 scope).
- **Diagrams.** A small number of architecture diagrams,
  Mermaid or SVG. No animated infographics.
- **Translations — infrastructure only.** The site structure
  must support additional language subtrees without a
  rewrite. Actual translations (pt-BR is the likely first
  target given the consumer app's market) happen post-1.0
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

### P1-46

1. `make docs-serve` → localhost:8000 renders the seven
   pages with working nav and search.
2. `mkdocs build --strict` → zero warnings.
3. GitLab Pages pipeline deploys on tag `v0.9.0`.
4. `curl <pages-url>/user/inspect/` returns the inspect page
   HTML.
5. `rg -n 'google-analytics|gtag|cookie' docs/site/` → no
   matches.

### P1-47

1. All P1-46 checks still pass.
2. Dev section covers every seam in `pave/` (one page per
   module boundary).
3. `reference/config.md` matches `_DEFAULTS` (CI drift check
   — overlap with P1-26).
4. Version selector shows `0.9` and `latest` (→ `1.0`).
5. Custom domain resolves over HTTPS.
