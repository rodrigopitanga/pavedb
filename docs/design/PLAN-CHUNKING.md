<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# PLAN-CHUNKING — Per-collection chunking (P1-78)

Chunking is two instance-wide settings, `preprocess.txt_chunk_size` and
`preprocess.txt_chunk_overlap`, read once at import. A config reload does not
apply them, no collection records the values its chunks were made with, and
changing them leaves collections mixed without saying so. P1-78 gives
chunking the same shape as search modes (P1-77): the operator sets what is
allowed, the collection fixes its chunking at creation.

## Strategies

| Strategy | Chunks |
|---|---|
| `fixed` | windows of `size` characters, each overlapping the previous by `overlap` |
| `none` | the whole text is one chunk; the caller chunks before ingesting |

Sizes are characters, as today. A strategy applies to free text: TXT uploads,
`ingest-text` and batch text documents. PDF pages and CSV rows stay one chunk
per page or row, as today.

## Who decides

1. **Operator**, in config:

   | Key | Default | Meaning |
   |---|---|---|
   | `chunking.strategies` | `[fixed, none]` | strategies collections may choose |
   | `chunking.min_size` | `200` | smallest `size` a collection may choose |
   | `chunking.max_size` | `8000` | largest `size`; also the largest `none` chunk |
   | `chunking.min_overlap` | `0` | smallest `overlap` |
   | `chunking.max_overlap` | `2000` | largest `overlap` |
   | `chunking.default_strategy` | `fixed` | used when a collection names none |
   | `chunking.default_size` | `1000` | `fixed` size when a collection omits it |
   | `chunking.default_overlap` | `200` | `fixed` overlap when a collection omits it |

   Startup refuses config that is not sane, naming the key: `1 <= min_size <=
   max_size`, `0 <= min_overlap <= max_overlap`, a non-empty strategy list
   drawn from `fixed` and `none`, a default strategy in that list, and a
   default size and overlap that are themselves a valid pair. A pair is valid
   when both values are within bounds and `size >= 4 * overlap`: an overlap
   above a quarter of the chunk mostly re-embeds the same text. The
   `preprocess.txt_chunk_*` keys are removed.
2. **Collection** — `chunking` at creation: `{"strategy": "fixed", "size":
   N, "overlap": M}` or `{"strategy": "none"}`. Omitted, the operator default
   applies; a `fixed` spec missing `size` or `overlap` takes the default for
   it. An invalid spec answers `400 invalid_chunking` naming the broken bound.
   It never changes afterwards: another chunking means another collection.
3. **Caller** — none. Mixed chunking inside one collection would make
   documents and replays incomparable.

With `none`, a text longer than `chunking.max_size` answers `400
chunk_too_large` instead of being cut.

## Storage

The collection's chunking lives beside its search mode in the `settings`
table of its `meta.db` (P1-77), so it travels with both archive kinds. It is
recorded when the collection is created, or on its first load: a collection
that already holds chunks predates P1-78 and records the shipped `fixed`
1000/200; an empty one, including one that ingest auto-creates, records the
operator default. No schema migration is needed. An instance that customized
`preprocess.txt_chunk_*` gets records that do not match its old chunks; this
is a pre-1.0 release, and the release notes say to recreate those
collections.

The create response and collection detail report the effective chunking.
`pavecli create-collection` takes `--chunking '<json>'`, like
`--embedder-config`.

## Simplest-path review (ponytail)

Cut: sentence, token or semantic strategies (add a strategy when a user
needs one); a per-ingest override; changing chunking after creation, which
would need a re-chunk job, not a reindex; splitting long PDF pages; a
restore-time check against destination limits (limits govern creation; an
archived collection keeps the chunking its data was made with); a
configurable overlap ratio. Reused: the existing window function, now
parameterized instead of reading module constants.

## Verification

Config sanity failures, create-time validation and defaults, `fixed` and
`none` ingest (text and TXT), `chunk_too_large`, detail output, and the
recorded chunking of auto-created and pre-P1-78 collections.
