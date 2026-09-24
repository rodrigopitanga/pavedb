<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# PLAN-HYBRID-SEARCH — Metadata parity and lexical retrieval

PaveDB searches vectors first, screens those candidates with SQLite metadata
and content predicates, applies canonical Python metadata-filter semantics,
and hydrates canonical chunk text from SQLite. This plan separates
correctness, content search, hybrid ranking, and true vector prefiltering so
none is mistaken for another.

Roadmap ownership:

- `P1-35` — CI-only metadata-filter SQL parity, v1.2
- `P1-70` — SQLite chunk text and content filters, v1.2
- `P1-71` — backend-aware filtered vector search, v1.3
- `P1-72` — hybrid lexical/vector retrieval, v1.2
- `P1-77` — search modes: who enables, defaults, and picks them, v1.0

## Current flow

```text
query -> embed -> FAISS top-N -> SQLite metadata/content screen
      -> canonical Python filter -> expand N if needed
      -> hydrate result text from SQLite
```

SQLite currently reduces only the candidates already returned by FAISS. It
can save metadata hydration and Python work, but it does not make vector
search itself smaller.

## P1-35 — metadata-filter SQL parity

P1-35 is a deterministic test harness, never a production request-time
comparison. For the same candidate IDs:

- every canonical match must survive SQL (`canonical ⊆ SQL survivors`);
- when SQL claims complete support, both result sets must be equal;
- unsupported operators may remain SQL no-ops and are decided by the
  canonical Python post-filter.

Operators move into SQLite one capability at a time. Exact and list-valued
metadata come first. Wildcard matching needs explicit escaping and benchmark
proof. Numeric and datetime comparisons require typed SQL values rather than
lexical comparison of the current text column. Negation must preserve the
rule that chunk metadata overrides document metadata.

Parity proves correctness. Separate benchmarks prove whether an operator is
worth pushing into SQLite.

## P1-70 — SQLite text and content filters

Chunk text becomes nullable canonical data in the per-collection `chunks`
table. Existing text collections migrate from sidecars; BYOV-only records keep
`NULL` text. Sidecars are removed only after successful copy and verification,
so a failed migration leaves the old collection recoverable.

An FTS5 index over chunk text provides token, phrase, prefix, and BM25 queries.
Use the built-in tokenizer/index surface; add trigram indexing only for the
selected substring/wildcard contract. Do not add Chroma or another search
dependency.

Content filters are additive and separate from metadata `filters` and semantic
`q`. `SearchBody.content_filter` starts as one object with `op` (`exact`,
`phrase`, `prefix`, or `contains`) and a non-empty `value`; it is not a second
boolean filter language. Exact matching uses `chunks.text = ?`. Phrase and
prefix use escaped FTS5 queries. `contains` ships only with a proven trigram
index and requires at least three Unicode characters. Regex can post-filter
indexed candidates later; it is not a reason to scan every sidecar or add a
regex indexing subsystem.

Metadata and content filters are ANDed: every returned row satisfies both.
`q` remains the search query, while `mode` controls candidate generation; a
content filter does not replace `q`.

Before implementation, CI must prove FTS5 and the selected tokenizer are
present in every supported Python/Docker runtime. There is no silent
linear-scan fallback for a public indexed-search contract.

The ingest, replace, purge, archive, restore, and migration paths update or
verify chunk text and its FTS index under the existing collection write lock.
Search keeps the collection read-lock boundary. FTS is rebuildable from
non-null `chunks.text`. This work does not claim that BYOV records with null
text make the FAISS index rebuildable; that existing limitation stays explicit.

## P1-72 — hybrid lexical/vector retrieval

P1-07 remains the narrower exact-token boost inside the vector candidate
window. P1-72 consumes two independent candidate lists:

- FAISS vector similarity;
- FTS5 BM25/token ranking from P1-70.

Each source uses the existing candidate window `max(50, 5 * result_k)`, capped
by its available rows. Fuse ranks with equal-weight reciprocal-rank fusion
using `k=60`, then break ties by vector rank and `rid`; raw FAISS and BM25
scores never share a scale. `SearchBody.mode` adds `vector` (the default) and
`hybrid`; hybrid requires a text `q`, not a BYOV query vector. Report
vector/lexical contribution in timing and match reason. No learned reranker or
external search service is required.

Query logging persists both `content_filter` and `mode`; replay restores them
verbatim so a logged content-filtered or hybrid search cannot become a plain
vector search.

## P1-77 — search modes

P1-07 shipped the exact-token boost as an instance-wide switch: one operator
setting re-ranked every search of every tenant, the caller could not see or
choose it, the query log did not record it, and hybrid mode silently dropped
it. P1-77 makes ranking one per-search choice with three levels of control.

### Modes

| Mode | Ranking |
|---|---|
| `vector` | FAISS similarity only (the default) |
| `boost` | `vector`, plus the exact-token nudge inside the vector window |
| `hybrid` | reciprocal-rank fusion of FAISS and FTS5 candidates (P1-72) |

`vector` stays the industry name for vector-only ranking. `flat` is rejected
because it already means the brute-force FAISS index, `plain` because it
describes nothing, and `exact` because it collides with the `exact`
content-filter op and with exact nearest-neighbour search. `boost` names
what the mode does. Boost and hybrid are alternatives, one mode each, so the
case where a hybrid search silently drops the boost cannot arise.

### Who decides

1. **Operator** — `search.modes` lists the modes this instance serves
   (default all three; must include `vector`). `search.boost_weight` (default
   `0.1`, `0 < w <= 1`) is the operator's tuning of the boost. The
   instance-wide `search.exact_token_boost` switch and its weight key are
   removed.
2. **Collection** — `search_mode` is chosen at creation (default `vector`) and
   must be a mode the instance serves. It does not change afterwards.
3. **Caller** — `mode` on a search is optional; omitted, the collection's
   `search_mode` applies. A mode the operator does not serve, requested or
   defaulted, answers `400 search_mode_disabled`; it is never downgraded
   silently.

A raw-vector query has no text, so it always ranks as `vector`: asking for
`boost` or `hybrid` with `v` is a `400`, and a collection defaulting to either
runs a raw-vector query as `vector`. The response carries the effective
`mode`, so a caller always knows which score scale it got (cosine for
`vector`/`boost`, fusion score for `hybrid`). Shared-scope search (`/v1/search`)
uses the shared collection's default; a merged `include_common` search ranks
both collections with the caller's effective mode.

### Boost semantics

A query token is a `\w+` run of the query, deduplicated case-insensitively:
the same split hybrid's lexical side already uses. A token matches a
candidate when FTS5 (`unicode61`) matches it in that chunk, so "exact token"
means the same thing in `boost`, in `hybrid`, and in the `exact`/`phrase`
content filters. A candidate matching `m` of `n` tokens gains
`boost_weight * m / n`; the match reason reads `exact tokens m/n`. The boost
still re-ranks only the vector candidate window and never adds candidates.

### Query log and replay

The log records the effective mode; replay runs the recorded mode and answers
`400 search_mode_disabled` if the operator has since stopped serving it.
`boost_weight` is not recorded: it is operator tuning, like embedder routing,
and changing it is an operator action outside the corpus-drift story.

### Storage

A collection's settings live in its own `meta.db`, in a `settings` table
(collection schema v9), so they travel with collection and instance archives
with no new catalog column, sidecar field or manifest key. v9 seeds existing
collections with `search_mode = vector` and rebuilds `query_log` so its mode
check accepts `boost`.

### Simplest-path review (ponytail)

Cut: an "always" level that stops callers overriding the default (no
competitor has one); changing `search_mode` after creation; weighted fusion
(`alpha`); recording `boost_weight`; a restore-time check of the default mode
(the first search already answers `search_mode_disabled`). Reused instead of
written: FTS5 decides token matches, which deletes the Python tokenizer and
the per-candidate chunk-text reads the boost did before. SDK and client
`mode` parameters follow in their own repos.

## P1-71 — true filtered vector search

P1-71 changes execution order for selective filters:

```text
SQLite metadata/content filter -> eligible record IDs
                               -> backend selector/mask -> vector search
```

Do not change the frozen `VectorBackend` protocol. Add an optional capability
protocol with `search_filtered(vector, k, allowed_rids)` only after the active
backend can consume the allowed-ID set without rebuilding an index per request.
The store probes that capability and otherwise keeps the existing vector-first
candidate screen as a correct fallback; the fallback is not prefilter
acceleration. Benchmark selectivity, overfetch count, latency, and memory before
enabling a new backend path.

## Verification

- `P1-35`: differential corpus covering every claimed SQL capability and
  document/chunk override semantics.
- `P1-70`: schema migration, BYOV-null behavior, FTS rebuild, archive/restore,
  and content-filter API/CLI/client parity.
- `P1-72`: fixed-corpus vector, lexical, and hybrid ranking expectations plus
  before/after latency evidence; query-log replay preserves `mode` and
  `content_filter`.
- `P1-71`: identical result semantics with and without backend filtering, plus
  selectivity benchmarks showing reduced vector work.
- `P1-77`: create/search/replay/restore across the operator, collection and
  caller levels; boost counts match FTS5 for mixed-case tokens; v9 migration
  of an existing collection.

## Not in scope

- request-time parity execution;
- a Chroma runtime or test dependency;
- a learned reranker;
- scanning every sidecar as the permanent content-search implementation;
- claiming SQL candidate screening already constrains FAISS.
