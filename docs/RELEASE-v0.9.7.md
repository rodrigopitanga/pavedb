# PaveDB 0.9.7

<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

## English

PaveDB 0.9.7 adds search modes, per-collection chunking and collections
tenants can take with them, and closes nineteen defects. It is a 1.0 preview
release; persisted data stays forward-compatible, as it has been since 0.9.5.

### What is new since 0.9.6

- **Search modes.** Every search runs in one of three modes. `vector` ranks by
  similarity. `boost` nudges vector hits whose text contains the query's
  tokens. `hybrid` fuses vector and full-text rankings. The operator decides
  which modes the instance serves, each collection chooses its default at
  creation, and a search may ask for another mode. The response and the query
  log say which mode ran.
- **Scores you can read.** Each hit's `match_reason` states how its score was
  made: `vector: cosine 0.834`, `boost: cosine 0.712 + 0.067 for exact tokens
  2/3`, or the vector and lexical ranks of a hybrid hit.
- **Priority boosts.** A number in [-1, 1] under a hit's `prio_boost`
  metadata scales its score by `1 + value` in every mode, and `match_reason`
  shows it. A collection can name another field at creation.
- **Text-aware retrieval.** Chunk text lives in SQLite with full-text and
  trigram indexes. Content filters (`exact`, `phrase`, `prefix`, `contains`)
  combine with metadata filters, and selective filters narrow the vector search
  itself instead of screening its results.
- **Per-collection chunking.** A collection chooses `fixed` windows or `none`
  (the caller chunks) at creation, within minimum and maximum sizes and
  overlaps the operator sets.
- **Collections you own.** A tenant can download one collection as a portable
  archive, restore it as a new collection on any same-version instance or roll
  an existing one back to it, and rebuild it into another embedding model as a
  resumable job, all with its own key. Archives carry the PaveDB and schema
  version they were made with.
- **Operator control over embedding.** Configured embedder instances are
  selected by routing policies, with a per-operation preference so bulk ingest
  or a reindex can run on a GPU instance while search stays on the default.
  Every embedder runs in a worker pool with bounded concurrency.
- **Request-rate limits.** Per-tenant budgets per minute and per hour, persisted
  across restarts, alongside the existing concurrency caps and quotas.
- **CLI names collections as `tenant/collection`.** `pavecli search demo/books
  "captain nemo"`, the same path the HTTP API uses.

### Security

This release closes nineteen defects, nearly all from a security review.
Operators running 0.9.6 should upgrade.

- **No internal detail in error bodies.** Unexpected failures answer with a
  fixed message and log the detail; embedder failures no longer reveal internal
  URLs or gateway responses; validation errors no longer echo the rejected
  value.
- **Shared search is text-only and private.** It no longer accepts caller
  vectors, which with exact scores allowed stored vectors to be rebuilt, and it
  no longer writes each caller's query text into the shared tenant's query log.
- **Keys.** Bearer tokens are compared in constant time, and a null key in
  `config.yml` now revokes a key that only the tenants file supplies.
- **Containment.** The container runs as an unprivileged user, and the ops log
  is created readable by owner and group only.
- **Resource bounds.** A collection can only use an embedding model the operator
  configured, so a caller cannot make the instance download one; archive restore
  refuses archives that would fill the disk, embedders are built outside the
  store locks so a slow model cannot stall every tenant, idle collection locks
  are released, and an identity collection's dimension is bounded.
- **Durability.** The FAISS index pair is written in two phases and completed or
  discarded on the next load; a whole-instance dump skips a damaged collection
  and reports it instead of failing for every tenant; new collections are
  written with their index from the start.

### Documentation

The guides gain a production checklist, the difference between an
instance backup and a portable collection, what the query log keeps, and the
inspect-and-replay loop from a hit back to its source.

### Upgrade note

Stored data migrates forward automatically on first open. These changes need
attention:

- **Configuration.** `search.exact_token_boost` and its weight are replaced by
  `search.modes` and `search.boost_weight`; `preprocess.txt_chunk_size` and
  `txt_chunk_overlap` are replaced by the `chunking.*` keys. Existing
  collections keep the 1000/200 chunking they were built with; an instance that
  had customized the old keys should recreate those collections.
- **CLI.** Commands that took `demo books` take `demo/books`.
- **Shared search.** `POST /v1/search` rejects a raw vector (`v`).
- **Embedding models.** Creating a collection or starting a reindex with a model
  no configured instance names is refused; existing collections keep loading.
- **Match reasons.** The `match_reason` wording changed as described above;
  parse it, if at all, by its mode prefix.
- **Containers.** The image runs as UID 1000 with its instance home at `/data`.
  Mount volumes there, and `chown -R 1000:1000` a volume created by an older
  image, which was mounted at `/root/pavedb`.

### Links

- [Project website](https://pavedb.org/)
- [GitLab repository](https://gitlab.com/flowlexi/pavedb)
- [GitHub mirror](https://github.com/rodrigopitanga/pavedb)
- [PyPI project](https://pypi.org/project/pavedb/)
