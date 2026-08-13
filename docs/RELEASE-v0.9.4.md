# PaveDB 0.9.4

## English

PaveDB 0.9.4 hardens multi-worker operation, archive recovery, and search
correctness.

### Highlights

- **Worker-safe metrics.** Stores counters and bounded latency samples in the
  catalog database so multiple workers expose one consistent metrics view and
  share reset state.
- **Safer archives.** Preflights catalog, collection metadata, FAISS indexes,
  dimensions, and metrics before restore, and rolls back incomplete swaps.
- **Concurrency coverage.** Adds the scheduled chaos harness, controlled ingest
  shedding checks, catalog-to-disk invariants, and automatic failure triage.
- **Search correctness.** Preserves list-valued metadata filters and
  document/chunk override precedence, adds the SQL parity harness, and forwards
  raw-vector queries through shared search.
- **Operational cleanup.** Bounds hot collection caches, improves issue
  reconciliation, selects a supported Python interpreter automatically, and
  documents the Python SDK and Elixir client entry points.

### Links

- [Project website](https://pavedb.org/)
- [GitLab repository](https://gitlab.com/flowlexi/pavedb)
- [GitHub mirror](https://github.com/rodrigopitanga/pavedb)
- [PyPI project](https://pypi.org/project/pavedb/)
