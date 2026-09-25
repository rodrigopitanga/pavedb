# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Store contract.

Defines `BaseStore` — the typed surface every concrete store
implementation honors — together with the dataclasses that flow
across the store/service boundary (`SearchResult`, `SearchOutput`,
`IndexResult`) and the typed-alias shorthands used in their
signatures.
"""

from __future__ import annotations
from collections.abc import Iterable
from dataclasses import dataclass, asdict, field
import os
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from pave.embedders.base import EmbedderConfig


Metadata = dict[str, Any]
FilterSpec = dict[str, Any]
ContentFilterSpec = dict[str, str]
SearchMode = Literal["vector", "boost", "hybrid"]
CollectionInfo = dict[str, Any]
DocumentInfo = dict[str, Any]
ChunkInfo = dict[str, Any]
QueryLogEntry = dict[str, Any]
QueryHomeEntry = dict[str, Any]
ReindexJobInfo = dict[str, Any]
Record = tuple[str, str, Metadata]  # (rid, text, meta)


class SearchModeError(ValueError):
    """A search mode the instance does not serve, or that the query cannot
    use. `code` is the client-facing error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# Metadata field a collection reads priority boosts from unless it names
# another at creation (P2-11).
DEFAULT_PRIORITY_KEY = "prio_boost"


class PriorityKeyError(ValueError):
    """A collection's priority key conflicts with the one it was created
    with. `code` is the client-facing error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class MetadataValidationError(ValueError):
    """Raised when metadata keys become invalid after sanitization."""


@dataclass(frozen=True)
class SearchResult:
    """Store-layer search result."""
    id: str
    score: float
    text: str | None
    tenant: str
    collection: str
    meta: Metadata
    match_reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SearchOutput:
    """Store-layer search output with matches and optional timing detail."""
    matches: list[SearchResult]
    timing: dict[str, float] = field(default_factory=dict)
    # The mode that ranked these matches: the request's, or the collection's
    # default when the request named none.
    mode: SearchMode = "vector"

    def __iter__(self):
        return iter(self.matches)

    def __len__(self) -> int:
        return len(self.matches)

    def __getitem__(self, index):
        return self.matches[index]


@dataclass(frozen=True)
class IndexResult:
    """Store-layer indexing result."""
    indexed_chunks: int
    purged_chunks: int = 0


class BaseStore(Protocol):
    """Store surface for a single PaveDB instance.

    Composes embedder + vector backend + metadata store behind one
    tenant/collection-scoped interface. The service layer depends
    only on this surface; transport (HTTP, CLI, library) never
    talks to embedders, backends, or SQLite directly.
    """
    def warm(self) -> None:
        """Warm runtime caches without persisting any collection state.

        Default no-op. Concrete stores override to load default embedder
        weights, trigger backend-library one-time process init, etc., so
        the first user request doesn't pay those costs.
        """
        return None

    def create_collection(
        self,
        tenant: str,
        name: str,
        *,
        embedder: str | None = None,
        embedder_type: str | None = None,
        embed_model: str | None = None,
        embedder_config: EmbedderConfig | None = None,
        display_name: str | None = None,
        search_mode: SearchMode | None = None,
        chunking: dict[str, Any] | None = None,
        priority_key: str | None = None,
    ) -> CollectionInfo | None: ...

    def collection_chunking(
        self, tenant: str, collection: str
    ) -> dict[str, Any]: ...

    def delete_collection(self, tenant: str, collection: str) -> None: ...

    def update_collection(
        self,
        tenant: str,
        name: str,
        *,
        display_name: str | None = None,
    ) -> CollectionInfo | None: ...

    def move_collection(self, tenant: str, old_name: str, new_name: str) -> None:
        """Move a collection slug/path. Raises ValueError if invalid."""
        ...

    def start_reindex(
        self,
        tenant: str,
        collection: str,
        *,
        embedder_type: str | None = None,
        embed_model: str | None = None,
        embedder_config: EmbedderConfig | None = None,
    ) -> ReindexJobInfo: ...

    def get_reindex_job(self, job_id: str) -> ReindexJobInfo: ...

    def pause_reindex(self, job_id: str) -> ReindexJobInfo: ...

    def resume_reindex(self, job_id: str) -> ReindexJobInfo: ...

    def cancel_reindex(self, job_id: str) -> ReindexJobInfo: ...

    def list_collections(self, tenant: str) -> list[CollectionInfo]:
        """List collection summaries for a tenant."""
        ...

    def get_collection_detail(
        self,
        tenant: str,
        name: str,
    ) -> CollectionInfo | None: ...

    def get_collection_vector_dim(self, tenant: str, collection: str) -> int:
        """Return the collection's resolved runtime vector dimension."""
        ...

    def list_tenants(self) -> list[str]:
        """List all tenants."""
        ...

    def get_document(
        self,
        tenant: str,
        collection: str,
        docid: str,
    ) -> DocumentInfo | None: ...

    def list_documents(
        self,
        tenant: str,
        collection: str,
    ) -> list[DocumentInfo]: ...

    def list_chunks(
        self,
        tenant: str,
        collection: str,
        docid: str,
    ) -> list[ChunkInfo]: ...

    def get_chunk(
        self,
        tenant: str,
        collection: str,
        rid: str,
    ) -> ChunkInfo | None: ...

    def get_chunk_content(
        self,
        tenant: str,
        collection: str,
        rid: str,
    ) -> ChunkInfo | None: ...

    def purge_doc(self, tenant: str, collection: str, docid: str) -> int: ...

    def index_records(
        self,
        tenant: str,
        collection: str,
        docid: str,
        records: Iterable[Record],
        doc_meta: Metadata | None = None,
        *,
        vectors=None,
    ) -> IndexResult: ...

    def search(self, tenant: str, collection: str, query: str, k: int = 5,
               filters: FilterSpec | None = None,
               *, content_filter: ContentFilterSpec | None = None,
               mode: SearchMode | None = None,
               query_vector: list[float] | None = None) -> SearchOutput:
        """Search for similar documents."""
        ...

    def log_query(
        self,
        *,
        query_id: str,
        tenant: str,
        collection: str,
        actor: str,
        query_text: str,
        k: int,
        filters: FilterSpec | None = None,
        content_filter: ContentFilterSpec | None = None,
        mode: SearchMode = "vector",
        include_common: bool = False,
        common_tenant: str | None = None,
        common_collection: str | None = None,
        result_ids: list[str] | None = None,
        result_count: int = 0,
        latency_ms: float | None = None,
        timing: dict[str, float] | None = None,
        request_id: str | None = None,
        replay_of: str | None = None,
    ) -> None: ...

    def get_query_log_entry(
        self,
        tenant: str,
        collection: str,
        query_id: str,
    ) -> QueryLogEntry | None: ...

    def list_query_logs(
        self,
        tenant: str,
        collection: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[QueryLogEntry]: ...

    def query_log_gauge_stats(self) -> tuple[int, float | None]: ...

    def chunk_drift_stats(self) -> dict[str, int]: ...

    def put_query_home(
        self,
        query_id: str,
        tenant: str,
        collection: str,
    ) -> None: ...

    def resolve_query_home(
        self,
        query_id: str,
    ) -> tuple[str, str] | None: ...

    def purge_query_homes_for_collection(
        self,
        tenant: str,
        collection: str,
    ) -> None: ...

    def list_query_homes(
        self,
        tenant: str | None = None,
        collection: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[QueryHomeEntry]: ...

    def catalog_probe(self) -> None:
        """Lightweight readiness probe: confirm the catalog is reachable.

        Unlike `catalog_metrics`, this does no per-collection work, so
        liveness/readiness checks stay cheap and lock-free. Backends should
        override with the cheapest catalog read available. Raises on failure.
        """
        self.list_tenants()

    def catalog_metrics(self) -> dict[str, int]:
        """Return store-level catalog counters for admin/metrics endpoints.

        Default implementation provides tenant/collection counts only via the
        existing listing APIs. Backends with richer metadata stores should
        override this to include document/chunk counts.
        """
        tenants = self.list_tenants()
        collection_count = 0
        for tenant in tenants:
            collection_count += len(self.list_collections(tenant))
        return {
            "tenant_count": len(tenants),
            "collection_count": collection_count,
            "doc_count": 0,
            "chunk_count": 0,
        }

    def metrics_generation(self) -> str: ...

    def read_metrics(self) -> dict[str, Any]: ...

    def flush_metrics_delta(
        self,
        generation: str,
        counters: list[tuple[str, str, float]],
        latencies: dict[str, list[float]],
        last_error: tuple[str, float] | None,
        sample_limit: int,
    ) -> tuple[str, bool]: ...

    def reset_metrics(self) -> str: ...

    def refresh_catalog_metrics(self) -> dict[str, int]:
        """Refresh store-level catalog counters.

        Backends with expensive catalog metrics should override this so
        `catalog_metrics()` can stay cheap on request paths.
        """
        return self.catalog_metrics()

    def catalog_metrics_freshness(self) -> tuple[float | None, bool]:
        """Return (age_seconds, stale) for cached catalog metrics."""
        return 0.0, False

    def dump_archive(
        self,
        output_path: str | os.PathLike[str] | None = None,
        *,
        skipped: list[str] | None = None,
    ) -> tuple[str, str | None]:
        raise NotImplementedError

    def restore_archive(self, archive_bytes: bytes) -> None:
        raise NotImplementedError

    def dump_collection_archive(
        self,
        tenant: str,
        collection: str,
        output_path: str | os.PathLike[str] | None = None,
    ) -> tuple[str, str | None]:
        raise NotImplementedError

    def restore_collection_archive(
        self,
        tenant: str,
        collection: str,
        archive_bytes: bytes,
        *,
        replace: bool = False,
    ) -> None:
        raise NotImplementedError
