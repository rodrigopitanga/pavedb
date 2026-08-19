<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Basic operations

The shortest useful path, shown with `pavecli` because it is the least
ceremonious. Every flag is in [`cli.md`](../reference/cli.md); every endpoint is
in `openapi.json`. This page shows the shape, not the surface.

Examples use tenant `demo` and collection `books`.

## Create a collection

```bash
pavecli create-collection demo books
```

The collection is pinned to an embedder at creation. Take the default unless
you have a reason; changing your mind later means a new collection, because
vectors from two models cannot share an index.

## Ingest

```bash
pavecli ingest demo books ./book.txt --docid book-1
```

PaveDB chunks and embeds the file itself. PDF, CSV and TXT are supported;
`ingest-text` takes a string directly, and `ingest-batch` takes several
documents in one call.

The `docid` is yours to choose and is the update key: re-ingesting the same
`docid` replaces that document in place. No restart, no separate reindex.

## Search

```bash
pavecli search demo books "captain nemo" -k 3
```

Each hit carries its score, the matched text, and where it came from — document
id, page, character offset. That provenance is not optional metadata you have
to enable; it is what a hit is.

## Inspect and replay

Every search is logged as it happens.

```bash
pavecli list-queries demo books
pavecli get-query demo books <query_id>
pavecli replay-query demo books <query_id>
```

`get-query` returns the parameters, timing and result ids of a past search.
`replay-query` runs it again against current data — useful for telling
"the index changed" apart from "the query was always wrong".

To go from a hit down to the exact stored text:

```bash
pavecli list-chunks demo books --docid book-1
pavecli get-chunk-content demo books <rid>
```

## Clean up

```bash
pavecli delete-document demo books book-1
pavecli delete-collection demo books
```

## Where this goes next

- Every command, flag and output shape — [`cli.md`](../reference/cli.md)
- The same operations over HTTP — `openapi.json`
- Deploying this for real — [Running it](running-it.md)
