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
from typing import Any, Protocol

from pave.embedders.base import EmbedderConfig


Metadata = dict[str, Any]
FilterSpec = dict[str, Any]
CollectionInfo = dict[str, Any]
DocumentInfo = dict[str, Any]
ChunkInfo = dict[str, Any]
QueryLogEntry = dict[str, Any]
QueryHomeEntry = dict[str, Any]
Record = tuple[str, str, Metadata]  # (rid, text, meta)


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

    def __iter__(self):
        return iter(self.matches)

    def __len__(self) -> int:
        return len(self.matches)

    def __getitem__(self, index):
        return self.matches[index]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SearchOutput):
            return self.matches == other.matches and self.timing == other.timing
        return NotImplemented


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
        embedder_type: str | None = None,
        embed_model: str | None = None,
        embedder_config: EmbedderConfig | None = None,
        display_name: str | None = None,
    ) -> CollectionInfo | None: ...

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

    def list_collections(self, tenant: str) -> list[CollectionInfo]:
        """List collection summaries for a tenant."""
        ...

    def get_collection_detail(
        self,
        tenant: str,
        name: str,
    ) -> CollectionInfo | None: ...

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

    def index_records(self, tenant: str, collection: str, docid: str,
                      records: Iterable[Record],
                      doc_meta: Metadata | None = None
                      ) -> IndexResult: ...

    def search(self, tenant: str, collection: str, query: str, k: int = 5,
               filters: FilterSpec | None = None) -> SearchOutput:
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
    ) -> tuple[str, str | None]:
        raise NotImplementedError

    def restore_archive(self, archive_bytes: bytes) -> None:
        raise NotImplementedError
