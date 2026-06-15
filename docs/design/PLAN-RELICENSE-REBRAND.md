<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# PLAN-RELICENSE-REBRAND

Plan for relicensing (AGPLv3+), author contact update,
and staged rebranding.

## Decisions

- License: **AGPL-3.0-or-later** (done, verbatim in
  `LICENSE`).
- Author contact email: **rodrigo@flowlexi.com** (done).
- Product name: **PaveDB**.
- Package name stays **`patchvec`** on PyPI, **`pave`**
  internally.
- Rebrand in two phases:
  - **v0.5.9** runtime/internal identifiers +
    env/runtime migration (with backward compat +
    startup log warning for deprecated vars).
  - **v0.6** user-visible (docs, UI, banner, CLI help)
    + remove deprecated `PATCHVEC_` fallback + repo
    migration.

## Relicensing scope (AGPLv3+) — done

- `LICENSE` — verbatim AGPLv3-or-later text.
- SPDX headers — `AGPL-3.0-or-later` across repo.
- `setup.py` — `license="AGPL-3.0-or-later"`.
- Author email — `rodrigo@flowlexi.com` everywhere.

## Rebrand scope (PaveDB)

### Phase 1 — Runtime + Operator Surfaces (v0.5.9)

Goal: rename runtime and operator-facing identifiers
needed for the rebrand and env migration. Existing
deployments keep working via backward-compat fallback
with a startup log warning when legacy vars are used.

Notes:

- The env migration slice is broader than
  `pave/config.py`: it must include all major env
  producers and probes (`runtime_paths.py`, `main.py`,
  `Makefile`, `docker-compose.yml`, test harness).
- The deprecation notice is a startup/app-reload
  `log.warning(...)`, not a Python
  `DeprecationWarning`.
- Phase 1 intentionally includes two externally
  observable compatibility changes:
  - `/metrics` prefix `patchvec_` → `pavedb_`
  - `WWW-Authenticate` realm `patchvec` → `pavedb`
  These require release-note callouts in v0.5.9.

Touchpoints:

- **Env prefix**: `PATCHVEC_` → `PAVEDB_`.
  Fallback: prefer `PAVEDB_`, then fill missing keys
  from `PATCHVEC_`. When a `PATCHVEC_` var is used,
  log a deprecation warning once at startup/app reload.
  Fallback removed in v0.6.
- **Env producers/probes**: `runtime_paths.py`,
  `main.py`, `Makefile`, `docker-compose.yml`,
  tests/conftest.
- **Default paths**: `~/patchvec/` → `~/pavedb/`.
- **Config example**: `config.yml.example` updated
  (ships in the package, not user-facing docs).
- **Metrics prefix**: `patchvec_` → `pavedb_`
  (`pave/metrics.py`, `tests/test_metrics.py`).
  Public scrape surface; no dual-emission planned.
- **Auth realm**: `Bearer realm="patchvec"` →
  `Bearer realm="pavedb"`. Public header surface.
- **Temp/archive prefixes**: `patchvec_export_`,
  `patchvec-data-*.zip`, etc. → `pavedb_*`.
- **setup.py**: description → `"PaveDB — ..."`.
  `name="patchvec"` stays (PyPI name).
- **Makefile**: runtime env producers, `PKG_LONGNAME`,
  temp dir prefixes, comments.
- **Benchmark scripts**: docstrings, argparse help.

Non-goals for this phase:
- README, ABOUT, CHANGELOG, ROADMAP content.
- Public Docker image names / registry paths.
- `docker-compose.yml` service name, image name, and
  container name.
- Startup banner display string.
- FastAPI title default (`Patchvec`).
- Icon asset filename / favicon path references.
- UI display strings (`patchvec __VERSION__`).
- CLI help text beyond path examples.

### Phase 2 — External (v0.6)

Goal: complete the user-visible rebrand and remove
backward compatibility.

Implications from the phase-1 split:

- Phase 2 is now the compatibility-cut release for the
  env migration. `PATCHVEC_*` fallback disappears here,
  so all docs, examples, bootstrap paths, and operator
  guidance must be fully `PAVEDB_*`-only.
- Metrics prefix and auth realm were already renamed in
  v0.5.9. Phase 2 should not "rename them again"; it
  must instead document the already-landed break in the
  upgrade/migration notes.
- FastAPI title default, icon/favicon path rename,
  startup banner, and UI display strings now land
  together. That makes phase 2 the first release where
  all public display surfaces consistently say PaveDB.
- Repo/package migration and public docs changes must be
  coordinated in one release window. If PyPI, GitHub,
  GitLab path, or container image guidance changes, the
  user-facing docs and release notes must change in the
  same cut.

#### Code

- **Remove `PATCHVEC_` env var fallback** (breaking).
- **PyPI shim package**: final `patchvec` release
  becomes a redirecting compatibility package:
  - `install_requires=["pavedb"]`
  - deprecation notice / migration hint in package code
  - package metadata (`homepage`, `project_urls`) points
    to the new `pavedb` repo/docs
- **Startup banner**: `PatchVEC` → `PaveDB`.
- **FastAPI title default**:
  `cfg.get("instance.name","Patchvec")` → `PaveDB`.
- **Public Docker names**: published image path,
  `docker-compose.yml` service/image/container names,
  and README container examples.
- **Icon asset**: `patchvec_icon_192.png` →
  `pavedb_icon_192.png` (+ references in `ui.py`,
  `ui.html`).
- **UI**: `pave/ui.py`, `pave/assets/ui.html` display
  strings.
- **CLI help text**: all user-facing strings.

#### Documentation

- `README.md`, `ABOUT.md`, `CHANGELOG.md`,
  `ROADMAP.md`, `CONTRIBUTING.md`,
  `README-benchmarks.md`, `docs/*.md`.
- Public URLs and install snippets.
- Docker/pip guidance.
- Explicit upgrade notes:
  - `PATCHVEC_*` no longer works
  - default home/config examples use `~/pavedb`
  - metric/auth-realm rename already happened in
    v0.5.9

#### Repository migration

- **GitLab**: rename in-place (Settings → General →
  Advanced → Change path). Preserves CI/CD config,
  container registry, merge history. Old URLs
  redirect automatically.
- **GitHub**: create fresh `pavedb` repo, push clean.
  The current `patchvec` repo has force-push
  artifacts visible in the activity feed that harm
  first impressions for new contributors. A fresh
  repo starts clean. Archive the old repo with a
  pointer to the new one. 6 stars are recoverable.
  The old repo should redirect humans explicitly via:
  - archived README banner / top-of-file notice
  - pinned issue if useful
  - repo description / homepage pointing to `pavedb`
- **PyPI**: publish code under new `pavedb` package.
  Turn `patchvec` into a shim: final release has
  `install_requires=["pavedb"]` + deprecation notice
  in `__init__.py`. Existing `pip install patchvec`
  pulls in `pavedb` automatically, and package metadata
  sends users to the new repo/docs. Yank old versions
  later if needed.

## Notes

- PyPI name stays `patchvec` in v0.5.9. In v0.6,
  code moves to `pavedb` package; `patchvec` becomes
  a shim that depends on `pavedb`.
- Internal rebrand must not break existing deployments
  without a deprecation window (v0.5.9 → v0.6).
- Phase 1 still changes two operator-visible public
  surfaces: Prometheus metric names and auth realm
  headers. Treat those as intentional release-note
  items, not "purely internal" changes.
- Phase 2 carries the migration/documentation burden
  created by that split: it removes env fallback,
  finalizes public naming, and must ship explicit
  upgrade notes covering both v0.5.9 and v0.6 changes.
- By v0.6, both discovery channels must redirect:
  - PyPI users via the `patchvec` shim package
  - GitHub users via the archived old repo pointing to
    `pavedb`
- Test content strings (e.g. `"test of patchvec"`)
  are data, not branding — leave them alone.
