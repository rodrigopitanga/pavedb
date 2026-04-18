# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, asdict, field
import os
from typing import Any


Record = tuple[str, str, dict[str, Any]]  # (rid, text, meta)


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
    meta: dict[str, Any]
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
        if isinstance(other, list):
            return self.matches == other
        return NotImplemented


class BaseStore(ABC):
    @abstractmethod
    def create_collection(self, tenant: str, name: str) -> None: ...

    @abstractmethod
    def delete_collection(self, tenant: str, collection: str) -> None: ...

    @abstractmethod
    def rename_collection(self, tenant: str, old_name: str, new_name: str) -> None:
        """Rename a collection. Raises ValueError if old/new name invalid."""
        ...

    @abstractmethod
    def list_collections(self, tenant: str) -> list[dict[str, Any]]:
        """List collection summaries for a tenant."""
        ...

    @abstractmethod
    def list_tenants(self) -> list[str]:
        """List all tenants."""
        ...

    @abstractmethod
    def has_doc(self, tenant: str, collection: str, docid: str) -> bool: ...

    @abstractmethod
    def get_document(
        self,
        tenant: str,
        collection: str,
        docid: str,
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def list_documents(
        self,
        tenant: str,
        collection: str,
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    def purge_doc(self, tenant: str, collection: str, docid: str) -> int: ...

    @abstractmethod
    def index_records(self, tenant: str, collection: str, docid: str,
                      records: Iterable[Record],
                      doc_meta: dict[str, Any] | None = None
                      ) -> int: ...

    @abstractmethod
    def search(self, tenant: str, collection: str, query: str, k: int = 5,
               filters: dict[str, Any] | None = None) -> SearchOutput:
        """Search for similar documents."""
        ...

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

    def dump_archive(
        self,
        output_path: str | os.PathLike[str] | None = None,
    ) -> tuple[str, str | None]:
        raise NotImplementedError

    def restore_archive(self, archive_bytes: bytes) -> None:
        raise NotImplementedError
