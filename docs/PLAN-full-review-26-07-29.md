# PaveDB v0.9.3 final hardening review — 2026-07-29

## Review base

- Reviewed tree:
  `24e9639783c188e69093e15d51e39933fd8a005c`.
- Protected GitLab `main` and merged MR !37 resolve to that exact tree.
- MR pipeline
  [2709096968](https://gitlab.com/flowlexi/pavedb/-/pipelines/2709096968)
  passed.
- The review covers only the merged v0.9.3 runtime and release candidate.
  Parked MRs !30, !34, !36, and !38 and P1-46, P1-47, P1-62, and P1-63
  were excluded.
- The named prior public plan, `docs/PLAN-full-review-26-07-24.md`, is not
  present in the reviewed tree. Existing GitLab review issues were used as
  the finding baseline.

## Release verdict

**Blocked.** Two surviving findings meet the constrained v0.9.3 blocker
rubric:

1. [B15](https://gitlab.com/flowlexi/pavedb/-/work_items/224) can silently
   rebuild an upgraded custom-model collection with the global default after
   `catalog.db` recovery.
2. [B20](https://gitlab.com/flowlexi/pavedb/-/work_items/231) leaves the
   documented Docker deployment without a usable, operator-known auth key.

Both existing issues were reopened with current evidence. No duplicate blocker
was created.

## Technical review

| Key | Verdict | Verification |
|-----|---------|--------------|
| [B11](https://gitlab.com/flowlexi/pavedb/-/work_items/214) | Resolved | Fresh-interpreter `pave.filters` import regression passes; the import path uses standard-library logging. |
| [B13](https://gitlab.com/flowlexi/pavedb/-/work_items/222) | Resolved | Lifespan shutdown closes the store, BYOV writes persist synchronously, and dirty-marker reopen coverage passes. |
| [B14](https://gitlab.com/flowlexi/pavedb/-/work_items/223) | Resolved | Negated predicates defer to the canonical postfilter; exact/or and chunk-override regressions pass. |
| [B15](https://gitlab.com/flowlexi/pavedb/-/work_items/224) | **Blocker** | New collections get `collection.json`, but existing v0.9.2 collections are never backfilled. Removing `catalog.db` from a modeled dim-4 legacy collection reseeded it without its model, treated the index as corrupt, and rebuilt it at the dim-8 default. |
| [B16](https://gitlab.com/flowlexi/pavedb/-/work_items/226) | Resolved | The completion future retains the search slot through timeout or cancellation; cancellation and unlimited-search regressions pass. |
| [B17](https://gitlab.com/flowlexi/pavedb/-/work_items/227) | Resolved | Worker applications bootstrap and reuse the persisted generated admin key; the multi-worker regression passes. |
| [B18](https://gitlab.com/flowlexi/pavedb/-/work_items/228) | Resolved | Fresh production `pavedb-sdk 0.1.2` exposes public vector parameters; the LocalClient BYOV round-trip passes. |
| [B19](https://gitlab.com/flowlexi/pavedb/-/work_items/229) | Resolved | CLI JSON options use the shared object parser; malformed embedder configuration returns the structured error. |
| [R3-77](https://gitlab.com/flowlexi/pavedb/-/work_items/225) | Resolved | Cached vectors retain and verify the embedder object; the reused-identity regression passes. |
| [R3-78](https://gitlab.com/flowlexi/pavedb/-/work_items/230) | Resolved | The final Docker copy set includes `LICENSE` at `/app/LICENSE`. |

## Product and release review

### Release blockers

- **B15 — upgrade recovery corruption.** PaveDB v0.9.2 recorded
  per-collection embedder configuration only in `catalog.db`. The merged
  sidecar change protects future collections but does not migrate existing
  directories. The documented catalog-recovery path can therefore substitute
  a different model and vector space.
- **B20 — Docker auth onboarding.** The named volume now preserves data, but
  the documented `docker run` command passes no
  `PAVEDB_AUTH__GLOBAL_KEY`. Docker does not inherit the earlier host-shell
  export. The container generates a different key in
  `/root/pavedb/admin.key` and prints only its prefix, so the documented host
  key is rejected.

### Post-v0.9.3 non-blockers

- [R2-79](https://gitlab.com/flowlexi/pavedb/-/work_items/232) — the README
  examples are fixed, but `CONTRIBUTING.md` still places `--home` after the
  `pavecli list-tenants` subcommand. Reopened as one-line documentation
  cleanup.
- [R3-80](https://gitlab.com/flowlexi/pavedb/-/work_items/233) — protected-main
  pipeline
  [2714068712](https://gitlab.com/flowlexi/pavedb/-/pipelines/2714068712)
  exhausted file descriptors after 509 passing tests. The exact suite passes
  locally, including with a 1024-descriptor limit, so the evidence does not
  establish a production runtime leak. This is post-v0.9.3 reliability work,
  not a blocker under the constrained rubric; a publication pipeline still
  must pass its required test stage.

### Packaging evidence

- A staged 0.9.3 wheel and sdist built from the reviewed tree.
- Wheel metadata reported version 0.9.3 and
  `pavedb-sdk>=0.1.2,<0.2.0`.
- The wheel and sdist contained the PaveDB license; the sdist contained
  `setup.py` and the runtime store implementation.
- The normal release target bumps `pave/version.py` before tests and artifact
  checks. No tag, upload, or Docker publication was attempted during this
  review.
- No Docker daemon smoke was run. The B20 result follows directly from the
  documented command and the reviewed auth bootstrap path.

## Verification evidence

- Exact-tree non-slow suite with fresh production `pavedb-sdk 0.1.2`:
  **520 passed, 1 skipped, 29 deselected**.
- The same suite under a local soft `RLIMIT_NOFILE` of 1024:
  **520 passed, 1 skipped, 29 deselected**.
- Focused merged-fix regressions passed, including the B18 public SDK round
  trip after installing version 0.1.2.
- `git diff --check` against the MR base passed.
- `python -m compileall -q pave` passed.
- Protected `main` was revalidated live at the reviewed SHA. Its current
  pipeline is red only in required pytest job
  [15590196338](https://gitlab.com/flowlexi/pavedb/-/jobs/15590196338);
  issue-key sync, secret detection, and Semgrep completed successfully.

## Ponytail closeout

Ponytail retained only the smallest actionable set:

- reopen B15 for the missing legacy-sidecar recovery boundary;
- reopen B20 for the missing Docker environment pass-through;
- reopen R2-79 for one stale command, explicitly post-v0.9.3;
- create R3-80 for the deduplicated protected-main resource failure,
  explicitly post-v0.9.3.

All other reviewed fixes remain resolved. No parked roadmap work, speculative
abstraction, or duplicate issue was promoted into this cycle.
