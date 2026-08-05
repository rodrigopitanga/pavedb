<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Metric-aware and approximate FAISS indexes

This plan splits the former P3-54 ANN scope into three independently
deliverable P-items:

- P3-54: exact flat cosine, inner-product, and L2 metrics in v1.1.
- P3-55: optional HNSW in v1.2.
- P3-56: optional IVF with optimization lifecycle in v1.8.

This public plan supersedes the private first-cut ANN draft dated 2026-07-14.

PaveDB v1.0 deliberately remains an exact flat cosine database. Non-cosine
metrics and ANN are useful maturity and scale capabilities, but they are not
requirements for a supported 1.0 contract. The v1.0 VectorDBBench adapter uses
standard cosine cases and exact filter semantics.

## Shared collection configuration

All three items reuse the catalog's existing per-collection `backend_config`.
There is no second index-config storage path and no search-time index selector.

The minimal flat configuration is:

```json
{
  "backend_config": {
    "index_type": "flat",
    "metric": "cosine"
  }
}
```

Omitting `backend_config` preserves the current `flat` and `cosine` defaults.
Creation rejects unknown keys, invalid values, and parameters belonging to a
different index type. `index_type` and `metric` are immutable collection
identity; changing either requires a rebuilt index.

The collection create route, service, store, LocalClient, and Python SDK accept
the same configuration. Collection creation and detail responses return the
resolved configuration. Ingest and search request bodies do not change.

## Metric and score contract

Every supported index type implements the same metric matrix:

| Metric | FAISS metric | Vector preprocessing | PaveDB score |
|---|---|---|---|
| `cosine` | inner product | L2-normalize data and query | cosine similarity |
| `ip` | inner product | unchanged | inner product |
| `l2` | squared L2 | unchanged | negative squared distance |

The public score contract remains "higher is closer." Scores are comparable
only within the same metric and vector space. Common-search merging rejects
collections with different metrics. Human-readable match reasons label cosine
similarity, inner product, or positive squared-L2 distance rather than treating
every score as a percentage.

For unit-normalized vectors, cosine and squared L2 are algebraically
convertible. PaveDB does not invent a cross-metric normalization for arbitrary
vectors.

## P3-54: exact flat metrics

P3-54 introduces the real public `backend_config` surface with useful behavior;
there is no pre-1.0 no-op scaffolding step.

`FaissBackend` accepts the resolved metric and builds:

- cosine: `IndexIDMap2(IndexFlatIP(dim))`, with normalization;
- inner product: `IndexIDMap2(IndexFlatIP(dim))`, without normalization;
- L2: `IndexIDMap2(IndexFlatL2(dim))`, without normalization.

Index metadata persists `index_type` and `metric` beside `faiss.index` and the
record-ID map. Legacy indexes without those fields resolve to `flat/cosine`.
Catalog and on-disk configuration conflicts fail fast rather than silently
opening the wrong vector space.

The VectorDBBench adapter forwards each supported case metric at collection
creation. Official cosine and L2 cases provide ranking and recall gates. A
pinned custom IP case supplies the same gate when upstream has no standard IP
dataset.

Acceptance:

- existing collections remain flat cosine without migration;
- all three flat metrics pass add, search, delete, persistence, and reload
  tests;
- invalid or conflicting metric configuration fails at collection creation or
  load;
- common search never merges different metrics;
- the OpenAPI snapshot and client creation surfaces remain aligned;
- representative official cosine/L2 cases and the pinned custom IP case pass.

P3-54 does not add an optimize endpoint or backend lifecycle hook.

## P3-55: HNSW

HNSW extends the same collection configuration:

```json
{
  "backend_config": {
    "index_type": "hnsw",
    "metric": "cosine",
    "hnsw": {
      "M": 32,
      "efConstruction": 200,
      "efSearch": 64
    }
  }
}
```

HNSW builds incrementally during ingest. It therefore needs no public optimize
operation. Flat and HNSW share metric selection, score semantics, persistence
validation, and the existing ingest/search APIs.

Acceptance:

- cosine, IP, and L2 HNSW indexes pass add, search, delete, persistence, and
  reload tests;
- invalid HNSW parameters fail at collection creation;
- VectorDBBench records recall and QPS against the matching flat baseline;
- current flat collections and results remain unchanged.

## P3-56: IVF and optimization

IVF adds training parameters through the same configuration:

```json
{
  "backend_config": {
    "index_type": "ivf",
    "metric": "cosine",
    "ivf": {
      "nlist": 4096,
      "nprobe": 16
    }
  }
}
```

IVF is the first slice that needs an optimization lifecycle. Extend the
backend protocol with an idempotent `optimize()` method, then expose the same
operation through store, service, HTTP, LocalClient, and Python SDK layers.

The public operation is:

```text
POST /v1/collections/{tenant}/{name}/optimize
```

The caller chooses when to request optimization; core decides whether work is
needed and how to perform it. Flat and HNSW return a successful no-op. An
untrained IVF index trains and builds from buffered vectors, then becomes
searchable. Repeating the operation is safe. Search against an untrained IVF
collection returns `409 index_not_ready`.

VectorDBBench calls the operation after bulk load. Operators use it to mark the
same boundary. Long-running implementations should reuse the collection job
machinery available by v1.8 rather than hold an HTTP request indefinitely.

Before IVF merges, BYOV vectors must have a rebuildable source or the
collection must expose a clear reload-required state. Text-only chunk rebuild
cannot reconstruct identity-collection vectors.

Acceptance:

- cosine, IP, and L2 IVF configurations validate and persist;
- trained and untrained states survive restart;
- optimize is idempotent and covered through backend, store, HTTP,
  LocalClient, and SDK paths;
- concurrent ingest/search/optimize behavior has focused race coverage;
- VectorDBBench records training, recall, and QPS against the matching flat
  baseline;
- `bench-stress-full` covers the public state transition.

## Explicitly out of scope

- no no-op public optimize endpoint before P3-56;
- no mutable metric or index type on an existing collection;
- no cross-metric score normalization;
- no GPU index in these three slices;
- no PaveDB runtime dependency on VectorDBBench.
