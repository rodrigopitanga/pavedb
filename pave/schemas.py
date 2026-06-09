# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pydantic schemas for API request/response validation."""

from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator


class TraceResponse(BaseModel):
    """Shared tracing metadata for JSON responses."""
    request_id: str | None = None
    latency_ms: float | None = None


class ErrorResponse(TraceResponse):
    """API error envelope."""
    ok: Literal[False]
    code: str
    error: str
    details: dict[str, Any] | None = None


class OkResponse(TraceResponse):
    """Base success envelope."""
    ok: Literal[True] = True


class SearchResult(BaseModel):
    """API response item for search results."""
    id: str
    score: float
    text: str | None
    tenant: str
    collection: str
    meta: dict[str, Any]
    match_reason: str


class SearchTiming(BaseModel):
    """Per-phase latency breakdown."""
    embed_ms: float = Field(description="Query embedding time")
    search_ms: float = Field(description="Vector search time")
    filter_ms: float = Field(
        description="Metadata filter time (pushdown + post-filter)"
    )
    hydrate_ms: float = Field(
        description="Metadata and chunk text load time"
    )


class SearchResponse(OkResponse):
    """API response for search endpoints."""
    matches: list[SearchResult]
    timing: SearchTiming | None = Field(
        default=None,
        description="Per-phase latency breakdown",
    )
    query_id: str | None = Field(
        default=None,
        description=(
            "Server-assigned id for this search, usable with the "
            "/queries/{query_id} and /queries/{query_id}/replay endpoints. "
            "Omitted when query logging is disabled."
        ),
    )


class SearchBody(BaseModel):
    """API request body for search endpoints."""
    q: str
    k: int = 5
    filters: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_request_id(cls, data: Any) -> Any:
        if isinstance(data, dict) and "request_id" in data:
            raise ValueError(
                "request_id in search body is not supported; use the "
                "X-Request-ID header instead"
            )
        return data


class QueryLogEntry(BaseModel):
    """API entry for a persisted query log row."""
    query_id: str
    tenant: str
    collection: str
    actor: str
    query_text: str
    k: int
    filters: dict[str, Any] | None = None
    include_common: bool = False
    common_tenant: str | None = None
    common_collection: str | None = None
    result_ids: list[str]
    result_count: int
    latency_ms: float | None = None
    timing: dict[str, float] | None = None
    request_id: str | None = None
    replay_of: str | None = None
    executed_at: str


class QueryLogSummary(BaseModel):
    """API summary item for query log listings."""
    query_id: str
    query_text: str
    k: int
    result_count: int
    latency_ms: float | None = None
    request_id: str | None = None
    replay_of: str | None = None
    executed_at: str


class ListQueryLogsResponse(OkResponse):
    """API response for query log listing."""
    tenant: str
    collection: str | None = None
    queries: list[QueryLogSummary]
    count: int


class GetQueryLogResponse(OkResponse):
    """API response for query log lookup."""
    query: QueryLogEntry


class QueryReplayResponse(OkResponse):
    """API response for query replay."""
    original_query_id: str
    replay_query_id: str
    matches: list[SearchResult]
    timing: SearchTiming | None = Field(
        default=None,
        description="Per-phase latency breakdown",
    )
    original_result_count: int
    original_latency_ms: float | None = None


class RenameCollectionBody(BaseModel):
    """API request body for collection rename."""
    new_name: str


class CreateCollectionBody(BaseModel):
    """Optional API request body for collection creation."""
    embedder_type: str | None = None
    embed_model: str | None = None


class CreateCollectionResponse(OkResponse):
    """API response for collection creation."""
    tenant: str
    collection: str
    embedder_type: str
    embed_model: str


class DeleteCollectionResponse(OkResponse):
    """API response for collection deletion."""
    tenant: str
    deleted: str


class RenameCollectionResponse(OkResponse):
    """API response for collection rename."""
    tenant: str
    old_name: str
    new_name: str


class CollectionSummary(BaseModel):
    """API summary item for collection listings."""
    name: str
    display_name: str | None = None
    embedder_label: str | None = None


class ListCollectionsResponse(OkResponse):
    """API response for collection listing."""
    tenant: str
    collections: list[CollectionSummary]
    count: int


class CollectionDetailResponse(OkResponse):
    """API response for collection detail."""
    tenant: str
    name: str
    display_name: str | None = None
    embedder_type: str | None = None
    embed_model: str | None = None
    created_at: str | None = None
    doc_count: int
    chunk_count: int


class ListTenantsResponse(OkResponse):
    """API response for tenant listing."""
    tenants: list[str]
    count: int


class IngestDocumentResponse(OkResponse):
    """API response for document ingest."""
    tenant: str
    collection: str
    docid: str
    chunks: int


class DeleteDocumentResponse(OkResponse):
    """API response for document deletion."""
    tenant: str
    collection: str
    docid: str
    chunks_deleted: int


class GetDocumentResponse(OkResponse):
    """API response for document lookup."""
    tenant: str
    collection: str
    docid: str
    version: int
    ingested_at: str
    metadata: dict[str, Any]
    chunk_ids: list[str]
    chunk_count: int


class ChunkSummary(BaseModel):
    """API summary item for document chunk listings."""
    rid: str
    chunk_path: str | None = None
    meta: dict[str, Any]
    ingested_at: str


class DocumentSummary(BaseModel):
    """API summary item for document listings."""
    docid: str
    version: int
    ingested_at: str
    chunk_count: int


class ListDocumentsResponse(OkResponse):
    """API response for document listing."""
    tenant: str
    collection: str
    documents: list[DocumentSummary]
    count: int


class ListChunksResponse(OkResponse):
    """API response for document chunk listing."""
    tenant: str
    collection: str
    docid: str
    chunks: list[ChunkSummary]
    count: int


class GetChunkResponse(OkResponse):
    """API response for chunk lookup."""
    tenant: str
    collection: str
    docid: str
    rid: str
    chunk_path: str | None = None
    meta: dict[str, Any]
    ingested_at: str


class RestoreArchiveResponse(OkResponse):
    """API response for archive restore."""


class ResetMetricsResponse(OkResponse):
    """API response for metrics reset."""
    reset_at: float
