<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# 🛣️ PaveDB CLI Reference

Command reference for `pavecli`, the local PaveDB administration and
data-loading tool.

Generated from `pave.cli.build_parser`. Regenerate with `make docs-refresh`.

## Common Flow

```bash
pavecli init
pavecli create-collection demo books
pavecli ingest demo books ./book.txt --docid book-1
pavecli search demo books "captain nemo" -k 3
```

## Usage

```bash
pavecli [-h] [--compact] [--version] [--home HOME]
        [--config CONFIG]
        [--tenants TENANTS] [--data-dir DATA_DIR] <command> ...
```

## Pre-1.0 Note

Runtime selectors (--home, --config, --tenants, --data-dir) are global options.
Pass them before the command, e.g. pavecli --home ./instance list-tenants.

## Legal Notice

PaveDB 0.9.6 (pavecli)
Copyright (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
There is NO WARRANTY, to the extent permitted by law.

## Global Options

- `--compact` (optional): Emit compact JSON for scripting
- `--version` (optional): Print version, copyright, and license information
- `--home` (optional): Instance home directory; omitted siblings default under it;
  defaults to ~/pavedb
- `--config` (optional): Explicit config.yml path; default comes from
  PAVEDB_CONFIG/config search
- `--tenants` (optional): Explicit tenants.yml path; default comes from
  PAVEDB_AUTH__TENANTS_FILE/config
- `--data-dir` (optional): Explicit data directory; default comes from
  PAVEDB_DATA_DIR/config

## Commands

| Command | Description |
|---|---|
| `init` | Create a PaveDB instance config and data directory. |
| `admin` | Manage the local admin key. |
| `create-collection` | Create a collection. |
| `ingest` | Index a file into a collection. |
| `ingest-batch` | Index raw-text documents into a collection. |
| `ingest-text` | Index raw text into a collection. |
| `search` | Search a collection. |
| `list-queries` | List logged query IDs. |
| `get-query` | Fetch a logged query by ID. |
| `replay-query` | Replay a logged query by ID. |
| `list-documents` | List documents in a collection. |
| `get-document` | Fetch one document by ID. |
| `list-chunks` | List chunks for a document. |
| `get-chunk` | Fetch chunk metadata. |
| `get-chunk-content` | Print raw chunk text. |
| `delete-collection` | Delete a collection. |
| `move-collection` | Move a collection to a new name. |
| `update-collection` | Update collection metadata. |
| `delete-document` | Delete a document by ID. |
| `dump-archive` | Export the instance data archive. |
| `restore-archive` | Restore an instance data archive. |
| `reset-metrics` | Reset persisted metrics. |
| `list-tenants` | List tenants with cataloged collections. |
| `list-embedders` | List configured embedder instances. |
| `list-collections` | List collections for a tenant. |
| `get-collection` | Fetch collection details. |

## `init`

Create config.yml, tenants.yml, and the data directory for an instance.

### Usage

```bash
pavecli init [-h] [--force] [root]
```

### Examples

```bash
pavecli init
pavecli init ~/pavedb-staging
pavecli --config /etc/pavedb/config.yml \
  --tenants /var/pavedb/tenants.yml \
  --data-dir /var/pavedb/data init
```

### Arguments And Options

- `root` (optional): Instance home directory
- `--force` (optional): Overwrite config and tenants files if they already exist

## `admin`

Show or rotate the local instance admin key.

### Usage

```bash
pavecli admin [-h] {show-key,rotate-key} ...
```

### Examples

```bash
pavecli admin show-key
pavecli admin rotate-key
```

### Arguments And Options

- `admin_cmd` (required): (choices: show-key, rotate-key)

## `create-collection`

Create a collection, optionally with display and embedder configuration.

### Usage

```bash
pavecli create-collection [-h] [--display-name DISPLAY_NAME]
                                 [--embedder-type EMBEDDER_TYPE]
                                 [--embed-model EMBED_MODEL]
                                 [--embedder-config EMBEDDER_CONFIG]
                                 tenant collection
```

### Example

```bash
pavecli create-collection demo books --display-name "Books"
```

### Arguments And Options

- `tenant` (required): Tenant namespace
- `collection` (required): Collection slug: lowercase letters, digits, and hyphens; must
  start with a letter or digit; max 63 chars
- `--display-name` (optional): Human-readable collection label
- `--embedder-type` (optional): Embedder type, such as sbert, openai, or ollama
- `--embed-model` (optional): Embedding model name
- `--embedder-config` (optional): JSON object with embedder-specific config

## `ingest`

Preprocess and index a TXT, CSV, or PDF file into a collection.

### Usage

```bash
pavecli ingest [-h] [--docid DOCID] [--metadata METADATA]
                      [--csv-has-header {auto,yes,no}]
                      [--csv-meta-cols CSV_META_COLS]
                      [--csv-include-cols CSV_INCLUDE_COLS]
                      tenant collection file
```

### Example

```bash
pavecli ingest demo books ./book.txt --docid book-1 --metadata '{"lang":"en"}'
```

### Arguments And Options

- `tenant` (required): Tenant namespace
- `collection` (required): Collection name
- `file` (required): Path to TXT, CSV, or PDF input
- `--docid` (optional): Stable document ID; defaults from filename when omitted
- `--metadata` (optional): JSON object attached to the document
- `--csv-has-header` (optional): CSV header handling: auto, yes, or no (choices: auto,
  yes, no)
- `--csv-meta-cols` (optional): CSV columns for metadata only (not indexed). Names or
  1-based indices, comma-separated
- `--csv-include-cols` (optional): CSV columns to index. Names or 1-based indices,
  comma-separated. Defaults to all non-meta columns

## `ingest-batch`

Index a JSON batch of raw-text documents.

### Usage

```bash
pavecli ingest-batch [-h] tenant collection file
```

### Example

```bash
pavecli ingest-batch demo books ./documents.json
```

### Arguments And Options

- `tenant` (required): Tenant namespace
- `collection` (required): Collection name
- `file` (required): JSON batch input file

## `ingest-text`

Index one raw-text document.

### Usage

```bash
pavecli ingest-text [-h] [--docid DOCID] [--metadata METADATA]
                           tenant collection text
```

### Example

```bash
pavecli ingest-text demo books "Captain Nemo" --docid note-1
```

### Arguments And Options

- `tenant` (required): Tenant namespace
- `collection` (required): Collection name
- `text` (required): Raw text to index
- `--docid` (optional): Stable document ID; generated when omitted
- `--metadata` (optional): JSON object attached to the document

## `search`

Run semantic search over a collection and print the response envelope.

### Usage

```bash
pavecli search [-h] [-k K] [--include-common] [--filters FILTERS]
                      tenant collection query
```

### Example

```bash
pavecli search demo books "captain nemo" -k 3
```

### Arguments And Options

- `tenant` (required): Tenant namespace
- `collection` (required): Collection name
- `query` (required): Natural-language search query
- `-k` (optional): Number of matches to return (default: 5)
- `--include-common` (optional): Include configured common collection results
- `--filters` (optional): JSON object, e.g. {"docid":"DOC-1"}

## `list-queries`

List logged query IDs, optionally scoped to a tenant or collection.

### Usage

```bash
pavecli list-queries [-h] [--tenant TENANT] [--collection COLLECTION]
                            [--limit LIMIT] [--offset OFFSET]
```

### Example

```bash
pavecli list-queries --tenant demo --collection books --limit 20
```

### Arguments And Options

- `--tenant` (optional): Filter by tenant
- `--collection` (optional): Filter by collection
- `--limit` (optional): Maximum entries to return (default: 50)
- `--offset` (optional): Entries to skip

## `get-query`

Resolve a query_id through the catalog and print the stored query log entry.

### Usage

```bash
pavecli get-query [-h] query_id
```

### Example

```bash
pavecli get-query 6f7cfd26-7d7f-4e5d-942b-f2a516f515a6
```

### Arguments And Options

- `query_id` (required): Query ID to fetch

## `replay-query`

Resolve a query_id through the catalog and run the stored query again.

### Usage

```bash
pavecli replay-query [-h] query_id
```

### Example

```bash
pavecli replay-query 6f7cfd26-7d7f-4e5d-942b-f2a516f515a6
```

### Arguments And Options

- `query_id` (required): Query ID to replay

## `list-documents`

Print indexed documents and their collection metadata.

### Usage

```bash
pavecli list-documents [-h] tenant collection
```

### Example

```bash
pavecli list-documents demo books
```

### Arguments And Options

- `tenant` (required): Tenant namespace
- `collection` (required): Collection name

## `get-document`

Print document metadata and chunk summary for one document.

### Usage

```bash
pavecli get-document [-h] tenant collection docid
```

### Example

```bash
pavecli get-document demo books book-1
```

### Arguments And Options

- `tenant` (required): Tenant namespace
- `collection` (required): Collection name
- `docid` (required): Document ID

## `list-chunks`

Print chunk metadata for a document without chunk text content.

### Usage

```bash
pavecli list-chunks [-h] tenant collection docid
```

### Example

```bash
pavecli list-chunks demo books book-1
```

### Arguments And Options

- `tenant` (required): Tenant namespace
- `collection` (required): Collection name
- `docid` (required): Document ID

## `get-chunk`

Print metadata for one chunk by record ID.

### Usage

```bash
pavecli get-chunk [-h] tenant collection rid
```

### Example

```bash
pavecli get-chunk demo books book-1::chunk_0
```

### Arguments And Options

- `tenant` (required): Tenant namespace
- `collection` (required): Collection name
- `rid` (required): Chunk record ID

## `get-chunk-content`

Write one chunk's text content to stdout.

### Usage

```bash
pavecli get-chunk-content [-h] tenant collection rid
```

### Example

```bash
pavecli get-chunk-content demo books book-1::chunk_0
```

### Arguments And Options

- `tenant` (required): Tenant namespace
- `collection` (required): Collection name
- `rid` (required): Chunk record ID

## `delete-collection`

Remove a collection, including its local metadata and vector index.

### Usage

```bash
pavecli delete-collection [-h] tenant collection
```

### Example

```bash
pavecli delete-collection demo books
```

### Arguments And Options

- `tenant` (required): Tenant namespace
- `collection` (required): Collection name

## `move-collection`

Rename the collection slug and move its catalog/query-home records.

### Usage

```bash
pavecli move-collection [-h] tenant old_name new_name
```

### Example

```bash
pavecli move-collection demo old-books books
```

### Arguments And Options

- `tenant` (required): Tenant namespace
- `old_name` (required): Current collection name
- `new_name` (required): New collection slug: lowercase letters, digits, and hyphens;
  must start with a letter or digit; max 63 chars

## `update-collection`

Update collection metadata such as the display name, without re-indexing.

### Usage

```bash
pavecli update-collection [-h] --display-name DISPLAY_NAME
                                 tenant collection
```

### Example

```bash
pavecli update-collection demo books --display-name "Books"
```

### Arguments And Options

- `tenant` (required): Tenant namespace
- `collection` (required): Collection name
- `--display-name` (required): New human-readable collection label

## `delete-document`

Remove one document and its chunks from a collection.

### Usage

```bash
pavecli delete-document [-h] tenant collection docid
```

### Example

```bash
pavecli delete-document demo books book-1
```

### Arguments And Options

- `tenant` (required): Tenant namespace
- `collection` (required): Collection name
- `docid` (required): Document ID

## `dump-archive`

Write a zip archive containing the current instance data directory.

### Usage

```bash
pavecli dump-archive [-h] [--output OUTPUT]
```

### Example

```bash
pavecli dump-archive --output pavedb-data.zip
```

### Arguments And Options

- `--output` (optional): Destination ZIP file path

## `restore-archive`

Replace the instance data directory from a zip archive.

### Usage

```bash
pavecli restore-archive [-h] file
```

### Example

```bash
pavecli restore-archive pavedb-data.zip
```

### Arguments And Options

- `file` (required): Archive zip file path

## `reset-metrics`

Clear the shared runtime metrics in the configured catalog database.

### Usage

```bash
pavecli reset-metrics [-h]
```

### Example

```bash
pavecli reset-metrics
```

## `list-tenants`

Print tenants currently present in the collection catalog.

### Usage

```bash
pavecli list-tenants [-h]
```

### Example

```bash
pavecli list-tenants
```

## `list-embedders`

Print configured embedder instances and the resolved default routing pool.

### Usage

```bash
pavecli list-embedders [-h] [--tenant TENANT]
```

### Examples

```bash
pavecli list-embedders
pavecli list-embedders --tenant demo
```

### Arguments And Options

- `--tenant` (optional): Tenant namespace to echo in the response

## `list-collections`

Print collection names, display labels, and embedder labels for a tenant.

### Usage

```bash
pavecli list-collections [-h] tenant
```

### Example

```bash
pavecli list-collections demo
```

### Arguments And Options

- `tenant` (required): Tenant namespace

## `get-collection`

Print catalog, embedder, vector-space, document, and chunk details for a collection.

### Usage

```bash
pavecli get-collection [-h] tenant collection
```

### Example

```bash
pavecli get-collection demo books
```

### Arguments And Options

- `tenant` (required): Tenant namespace
- `collection` (required): Collection name
