<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Store Protocols — Stability Contract

PaveDB's store stack has two pluggable seams:

- **`Embedder`** — turns text into vectors.
- **`VectorBackend`** — stores vectors and returns ranked record IDs.

This document is the **authoritative contract** for both. Where the
descriptive "Layer contracts" section of
[`PLAN-STORE.md`](../design/PLAN-STORE.md) disagrees with this file
(it predates several refactors), this file wins.

Audience: backend/embedder authors and downstream tooling that depends
on these seams staying stable across minor versions.

## Scope

In scope: the `Embedder` and `VectorBackend` protocols, their method
semantics, and the convention for registering a new embedder type.

Out of scope: every layer above the seams — `BaseStore` / `LocalStore`,
`CollectionDB`, the catalog, `service.py`, and factory internals. Those
are implementation detail and may change at any version (see
[Internal — not the contract](#internal--not-the-contract)).

## Frozen protocols (v1.0)

### `Embedder`

`pave/embedders/base.py`:

```python
class Embedder(Protocol):
    def encode(self, texts: list[str]) -> NDArray[np.float32]:
        """(N, dim) matrix of embedding vectors."""

    @property
    def dim(self) -> int:
        """Embedding dimensionality (e.g. 384)."""
```

Semantics:

- `encode(texts)` returns a `float32` array of shape `(len(texts), dim)`.
  Row `i` is the embedding for `texts[i]`; input order is preserved.
- Embeddings need **not** be pre-normalized. The backend owns its own
  normalization (`FaissBackend` L2-normalizes on `add` and `search`), so
  embedders return raw model output.
- `dim` is a positive int, constant for the embedder's lifetime, and
  equals the second axis of `encode`'s output. It must be known at
  construction (FAISS index creation needs it up front).
- A missing model file, API key, or unreachable model server raises
  `EmbedderUnavailableError` (a `RuntimeError` subclass). The service
  layer maps it to the `embedder_unavailable` error code rather than the
  generic failure envelope. This error type is part of the contract.

### `VectorBackend`

`pave/backends/base.py`:

```python
SearchHit = tuple[str, float]

class VectorBackend(Protocol):
    def initialize(self) -> None: ...
    def add(self, rids: list[str], vectors: NDArray[np.float32]) -> None: ...
    def search(self, vector: NDArray[np.float32], k: int) -> list[SearchHit]: ...
    def delete(self, rids: list[str]) -> None: ...
    def flush(self) -> None: ...
    def close(self) -> None: ...
```

Semantics:

- `initialize()` loads/prepares persisted state from the backend's own
  constructor settings. It is idempotent, must be called before `add` /
  `search`, and is a no-op when nothing is persisted yet.
- `add(rids, vectors)` requires `len(rids) == vectors.shape[0]`; vectors
  are `float32` of shape `(N, dim)`. Re-adding an existing `rid` replaces
  its vector (upsert semantics — delete + reinsert).
- `search(vector, k)` takes a `float32` query of shape `(dim,)` or
  `(1, dim)` and returns **up to** `k` `(rid, score)` pairs in descending
  score order. An empty index returns `[]`. Scores are backend-defined
  similarity; `FaissBackend` returns cosine similarity in `[-1, 1]`.
- `delete(rids)` removes vectors by record ID. Unknown IDs are ignored.
- `flush()` persists pending state for local backends, or is a no-op for
  remote backends.
- `close()` releases resources; local backends flush first.

**Construction is not part of the protocol.** Persistence and connection
details are backend-specific constructor keyword arguments — e.g.
`FaissBackend(dimension, *, storage_dir=...)` — never generic protocol
members. There is no `storage_dir` (or any path) attribute on the
protocol itself.

Implementations:

| Class | Module | Status |
|---|---|---|
| `FaissBackend` | `pave/backends/faiss.py` | Default; persistent `IndexIDMap2` over `IndexFlatIP`. |

## Embedder authoring convention

Adding an embedder type `<type>` means dropping a module into
`pave/embedders/` and listing its name in `factory._EMBEDDER_TYPES`.
The factory imports `pave.embedders.<type>` by convention and reads
module-level attributes; only names in that set are ever imported,
because `embedder_type` is caller-supplied. A conforming module
exports:

- `build_embedder(cfg, *, model=None, embedder_config=None, api_key=None)`
  returning an `Embedder`.
- `CONFIG_KEYS: tuple[str, ...]` — every config key the type accepts.
- `SPACE_KEYS: tuple[str, ...]` — the subset of `CONFIG_KEYS` that changes
  the vector space (e.g. `dim`).
- Optional `CLONE_SAFE: bool` — opt into the factory's bounded thread-worker
  pool. Clone-safe modules accept the operational `workers` config key; it
  must not appear in `SPACE_KEYS`.
- Optional `validate_spec(resolved_config: dict) -> dict` for normalizing
  or rejecting config before construction.

Identity split: `vector_space_key` derives from
`(type, model, SPACE_KEYS subset)`; the runtime cache key uses the full
resolved config. A per-instance `api_key` is carried alongside the spec
and never participates in vector-space identity.

When several configured instances have the same `vector_space_key`,
`embedder.routing.policy` selects the runtime on collection load. Instance
order, routing weight, and the selected runtime are operational state; none is
written into the collection catalog.

`OllamaEmbedder` is the reference example of a thin module added this way.

## Stability tiers

| Tier | Meaning | Examples |
|---|---|---|
| **Frozen** | Signature + semantics locked at v1.0. A breaking change is a v2.0 event. | `Embedder.encode`, `Embedder.dim`, `VectorBackend.search` / `add` / `delete` / `initialize` / `flush` / `close`, `EmbedderUnavailableError` |
| **Stable** | Will not break existing callers, but may gain optional keyword arguments (with defaults) at a minor version. | `build_embedder` overrides; backend constructor kwargs |
| **Internal** | Not part of the contract. Authors must not depend on these. | `BaseStore` / `LocalStore`, `CollectionDB`, `service.py`, factory internals (`_embedder_module`, `resolve_embedder_spec`, …), backend private attrs |

## Additivity rules

PaveDB types these seams structurally (`typing.Protocol`), not by
inheritance — there is no shared ABC that supplies defaults. Therefore:

- Adding a **required** method to a frozen protocol breaks every existing
  implementor and is a **v2.0** change.
- Additive growth happens two ways, both minor-version safe:
  1. New **optional** keyword arguments (with defaults) on a Stable
     surface, never changing existing call sites.
  2. A new **capability protocol** (e.g. a future `BackendCapabilities`)
     that backends opt into and callers probe via duck typing. Introducing
     a brand-new protocol is always additive.
- Renaming a frozen method or changing its signature/semantics is **v2.0**.

## Internal — not the contract

The store orchestrator (`LocalStore`), `CollectionDB`, the catalog, and
`service.py` are deliberately excluded. They compose the seams above but
are free to change shape at any minor version. Backend and embedder
authors should depend only on the two protocols documented here.

## Plugin policy

There is **no third-party plugin entry point** at v1.0 — both seams are
in-tree only. This freeze is forward-preparation: when an entry point
lands (the v1.6 ingest plugin architecture is the precedent), the
stability rules already predate the first external implementor.

## Change policy

Any PR that touches `pave/embedders/base.py` or `pave/backends/base.py`
must add a `CHANGELOG.md` entry under a **`Protocol contract`** header,
kept distinct from internal store/service changes so contract movement is
auditable at a glance.
