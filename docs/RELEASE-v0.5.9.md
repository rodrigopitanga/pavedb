# PatchVec 0.5.9 — Relevance

## English

PatchVec 0.5.9 improves relevance, metadata filtering, and store reliability.
PatchVec is now PaveDB.

### Highlights

- Introduces `LocalStore` as the orchestrator and activates the FAISS vector
  path while retiring the runtime `txtai` dependency.
- Improves metadata filtering with chunk-level storage, exact-negation
  prefiltering, validation, sanitization, and a mixed-OR correctness fix.
- Hardens index loading, chunk and archive I/O, and cache lifecycle behavior
  against race and TOCTOU conditions.
- Adds benchmark SLO exit gates and a dedicated benchmark CI job.

### Links

- [PaveDB](https://pavedb.org/)
- [GitLab repository](https://gitlab.com/flowlexi/pavedb)
- [GitHub mirror](https://github.com/rodrigopitanga/pavedb)
- [Historical PatchVec package](https://pypi.org/project/patchvec/)
