<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Basic operations

The shortest useful path, shown with `pavecli` because it is the least
ceremonious. Every flag is in [`cli.md`](../reference/cli.md); every endpoint is
in [`openapi.json`](../../openapi.json). This page shows the shape, not the
surface.

Examples use tenant `demo` and collection `books`. Commands name a collection
as `tenant/collection`, the same path the HTTP API uses: `demo/books`.
They open the local data directory directly, so stop any `pavesrv` or persistent
local Python client using that directory first.

## Create a collection

```bash
pavecli create-collection demo/books --embedder native
```

`--embedder` accepts a configured instance key, type, `type:model`, or full
vector-space key; omit it to take `embedder.default`. A model no configured
instance names is refused, so a caller cannot make the instance fetch one. The
collection persists the selected vector-space identity, not an operational
instance pin. When several instances produce that space,
`embedder.routing.policy` chooses among them, and
`embedder.routing.priority.<op>` refines the choice per operation class
(`ingest`, `search`, `maintenance`): an ordered list of instance names where
the first one in the collection's vector space runs that operation — so a
batch ingest or a reindex lands on a GPU instance while interactive search
stays on the default. Unknown or incompatible names are skipped and an
unavailable preferred instance falls back, so one server-wide map never breaks
collections in other spaces. Changing vector space later requires reindexing
because vectors from two models cannot share an index.

Chunking is also fixed at creation. `--chunking
'{"strategy":"fixed","size":800,"overlap":100}'` cuts free text into
800-character windows that overlap by 100; `{"strategy":"none"}` keeps each
text as one chunk, for callers that chunk before ingesting. Omitted, the
instance default applies. PDF pages and CSV rows stay one chunk each either
way. Another chunking means another collection.

## Ingest

```bash
pavecli ingest demo/books ./book.txt --docid book-1
```

PaveDB chunks and embeds the file itself. PDF, CSV and TXT are supported;
`ingest-text` takes a string directly, and `ingest-batch` takes several
documents in one call.

The `docid` is yours to choose and is the update key: re-ingesting the same
`docid` replaces that document in place. No restart, no separate reindex.

## Search

```bash
pavecli search demo/books "captain nemo" -k 3
pavecli search demo/books "captain nemo" -k 3 --mode hybrid
pavecli search demo/books "captain" \
  --content-filter '{"op":"phrase","value":"captain nemo"}'
```

Each hit carries its score, the matched text, and where it came from — document
id plus the source-specific page, row, or character offset. That provenance is
not optional metadata you have to enable; it is what a text-backed hit is.
Content filters are additive to semantic search and metadata filters. `exact`,
`phrase`, and token `prefix` use SQLite text indexes; `contains` uses the
trigram index and requires at least three characters.

Three ranking modes:

- `vector` ranks by vector similarity alone.
- `boost` adds a nudge inside the vector candidates for hits whose text
  contains the query's tokens: `search.boost_weight` times the fraction of
  tokens matched.
- `hybrid` retrieves vector and full-text candidates independently and fuses
  their ranks with reciprocal-rank fusion.

A collection picks its default at creation (`create-collection
--search-mode boost`; `vector` if omitted), and a search that names no
`--mode` uses it. The response's `mode` says which one ran. `boost` and
`hybrid` need a text query; a raw query vector always ranks as `vector`. The
operator decides which modes the instance serves (`search.modes`); asking for
one it does not serve answers `400 search_mode_disabled`.

Each hit's `match_reason` shows how its score was made, so the number is
never opaque: `vector: cosine 0.834`, `boost: cosine 0.712 + 0.067 for exact
tokens 2/3`, or `hybrid: vector rank 2 (cosine 0.812) + lexical rank 5, each
1/(60+rank)`. Cosine scores compare across collections sharing a vector
space; hybrid scores only rank hits within one response.

A hit whose `prio_boost` metadata is a number in [-1, 1] ranks by
`score * (1 + value)`: `1` doubles it, `-1` zeroes it. A collection that
already uses that field for something else names another at creation
(`create-collection --priority-key rank`). Values outside the range are
clamped; booleans and anything non-numeric are ignored. The boost re-sorts
the candidates each mode already fetched, and the hit's `match_reason` ends
with it: `vector: cosine 0.500; priority: prio_boost +0.50, 0.500 -> 0.750`.

## Inspect and replay

Text searches are logged by default and can be replayed. Query logging can be
disabled; raw-vector searches cannot be replayed because no query vector is
stored.

```bash
pavecli list-queries --tenant demo --collection books
pavecli get-query <query_id>
pavecli replay-query <query_id>
```

A `query_id` is unique on its own, so `get-query` and `replay-query` take just
the id. `get-query` returns the parameters, timing and result ids of a past
search; `replay-query` runs it again against current data — useful for telling
"the index changed" apart from "the query was always wrong".

To go from a hit down to the exact stored text:

```bash
pavecli list-chunks demo/books book-1
pavecli get-chunk-content demo/books book-1::chunk_0
```

Put together, this is the inspection loop:

1. **Search**, and note the ranked hits: ids, scores and provenance.
2. **Open the source** of a hit: its chunk text and the page, row or offset
   it came from.
3. **Read the stored record** with `get-query`: parameters, timing, and the
   ranked result ids as they were at search time.
4. **Change the corpus**: re-ingest, add or delete documents.
5. **Replay** the query and **compare** with the stored record: which ids
   entered or left, and how ranks moved. Scores compare meaningfully only
   within one vector space, so after a reindex compare ids and ranks.

Changed results are the point: they make retrieval drift visible, attributed
to a concrete corpus change. PaveDB does not aim to reproduce historical
results, and it keeps no snapshot of the old corpus. The stored record keeps
its query text, parameters, timing and result ids after the source changes;
the text of chunks that were replaced or deleted is gone with them. See
[the query log](running-it.md#the-query-log) for retention.

The Python SDK runs the whole loop, from search to replay to chunk content,
in one program: `python -m pavesdk.examples.observability`
([source][sdk-observability]).

[sdk-observability]: https://gitlab.com/flowlexi/pavedb-sdk-python/-/blob/main/pavesdk/examples/observability.py

## Clean up

```bash
pavecli delete-document demo/books book-1
pavecli delete-collection demo/books
```

## Archive and reindex your collection

Your collection is yours to take or rebuild — with your tenant key, no admin
needed:

```bash
# download a portable zip of one collection (manifest + data)
curl -H "Authorization: Bearer $KEY" -o books.zip \
  "$PAVEDB_URL/v1/collections/demo/books/archive"

# roll it back to that snapshot (PUT); POST restores into a new collection
curl -X PUT -H "Authorization: Bearer $KEY" -F file=@books.zip \
  "$PAVEDB_URL/v1/collections/demo/books/archive"

# rebuild into another embedder space (resumable job; poll or cancel by id)
curl -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"embedder_type":"sbert","embed_model":"all-MiniLM-L6-v2"}' \
  "$PAVEDB_URL/v1/collections/demo/books/reindex"
```

The embedder rules are the same as create, and which configured instance
executes the re-embedding is the operator's routing decision. Both operations
are bounded per tenant on shared instances; see the running-it guide.

## Where this goes next

- Every command, flag and output shape — [`cli.md`](../reference/cli.md)
- The same operations over HTTP — [`openapi.json`](../../openapi.json)
- Deploying this for real — [Running it](running-it.md)
