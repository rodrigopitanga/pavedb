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
vector-space key; omit it to take `embedder.default`. The collection persists
the selected vector-space identity, not an operational instance pin. When
several instances produce that space, `embedder.routing.policy` chooses among
them, and `embedder.routing.priority.<op>` refines the choice per operation
class (`ingest`, `search`, `maintenance`): an ordered list of instance names
where the first one in the collection's vector space runs that operation — so
a batch ingest or a reindex lands on a GPU instance while interactive search
stays on the default. Unknown or incompatible names are skipped and an
unavailable preferred instance falls back, so one server-wide map never
breaks collections in other spaces. Changing vector space later requires
reindexing because vectors from two models cannot share an index.

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
  tokens matched, reported as `exact tokens m/n`.
- `hybrid` retrieves vector and full-text candidates independently and fuses
  their ranks with reciprocal-rank fusion.

A collection picks its default at creation (`create-collection
--search-mode boost`; `vector` if omitted), and a search that names no
`--mode` uses it. The response's `mode` says which one ran. `boost` and
`hybrid` need a text query; a raw query vector always ranks as `vector`. The
operator decides which modes the instance serves (`search.modes`); asking for
one it does not serve answers `400 search_mode_disabled`.

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

# restore it — same PaveDB version, any instance, your tenant
curl -H "Authorization: Bearer $KEY" -F file=@books.zip \
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
