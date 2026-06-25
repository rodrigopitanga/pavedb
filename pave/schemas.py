# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pydantic schemas for API request/response validation."""

from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator


class TraceResponse(BaseModel):
    """Shared tracing metadata for JSON responses."""
    request_id: str | None = Field(
        default=None,
        description="Correlation id echoed for this request.",
        examples=["req-5f3a-b812"],
    )
    latency_ms: float | None = Field(
        default=None,
        description="Total server-side time for this request, in ms.",
        examples=[12.4],
    )


class ErrorResponse(TraceResponse):
    """API error envelope."""
    ok: Literal[False] = Field(
        description="Always false on an error response.",
        examples=[False],
    )
    code: str = Field(
        description="Stable, machine-readable error code.",
        examples=["not_found"],
    )
    error: str = Field(
        description="Human-readable error message.",
        examples=["collection 'books' not found"],
    )
    details: dict[str, Any] | None = Field(
        default=None,
        description="Optional structured context about the error.",
        examples=[{"tenant": "demo", "collection": "books"}],
    )


class OkResponse(TraceResponse):
    """Base success envelope."""
    ok: Literal[True] = Field(
        default=True,
        description="Always true on a success response.",
        examples=[True],
    )


class SearchResult(BaseModel):
    """A single search hit."""
    id: str = Field(
        description="Chunk id of the matching passage.",
        examples=["verne-20k::chunk_42"],
    )
    score: float = Field(
        description="Relevance score; higher is closer.",
        examples=[0.82],
    )
    text: str | None = Field(
        description="The matched snippet, when stored.",
        examples=["Captain Nemo conducted me to the central staircase ..."],
    )
    tenant: str = Field(description="Tenant that owns the hit.", examples=["demo"])
    collection: str = Field(
        description="Collection the hit came from.",
        examples=["books"],
    )
    meta: dict[str, Any] = Field(
        description="Provenance and user metadata for the chunk.",
        examples=[{"docid": "verne-20k", "page": 12, "offset": 21000}],
    )
    match_reason: str = Field(
        description="Why this result matched.",
        examples=["semantic"],
    )


class SearchTiming(BaseModel):
    """Per-phase latency breakdown, in ms."""
    embed_ms: float = Field(
        description="Time to turn the query into a vector.",
        examples=[1.2],
    )
    search_ms: float = Field(
        description="Time spent finding nearest matches.",
        examples=[8.4],
    )
    filter_ms: float = Field(
        description="Time spent applying metadata filters.",
        examples=[0.6],
    )
    hydrate_ms: float = Field(
        description="Time spent loading snippet text and metadata.",
        examples=[2.1],
    )


class SearchResponse(OkResponse):
    """Search results with optional timing."""
    matches: list[SearchResult] = Field(
        description="Ranked list of hits, best first.",
    )
    timing: SearchTiming | None = Field(
        default=None,
        description="Per-phase latency breakdown.",
    )
    query_id: str | None = Field(
        default=None,
        description=(
            "Id for this search, usable with the /queries/{query_id} and "
            "/queries/{query_id}/replay endpoints. Omitted when query "
            "logging is disabled."
        ),
        examples=["q-7b2c-19af"],
    )


class SearchBody(BaseModel):
    """Search request body."""
    q: str = Field(description="The search text.", examples=["captain nemo"])
    k: int = Field(default=5, description="Maximum number of hits.", examples=[5])
    filters: dict[str, Any] | None = Field(
        default=None,
        description="Metadata equality filters to narrow results.",
        examples=[{"lang": "en"}],
    )
    include_common: bool = Field(
        default=False,
        description=(
            "Include configured common collection results when server common "
            "search is enabled."
        ),
        examples=[False],
    )

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
    """A recorded search and its results."""
    query_id: str = Field(
        description="Id of the logged search.",
        examples=["q-7b2c-19af"],
    )
    tenant: str = Field(description="Tenant the search ran in.", examples=["demo"])
    collection: str = Field(description="Collection searched.", examples=["books"])
    actor: str = Field(description="Who ran the search.", examples=["demo-key"])
    query_text: str = Field(description="The search text.", examples=["captain nemo"])
    k: int = Field(description="Maximum number of hits requested.", examples=[5])
    filters: dict[str, Any] | None = Field(
        default=None,
        description="Metadata filters applied to the search.",
        examples=[{"lang": "en"}],
    )
    include_common: bool = Field(
        default=False,
        description="Whether common results were included.",
        examples=[False],
    )
    common_tenant: str | None = Field(
        default=None,
        description="Tenant of the included common collection, if any.",
        examples=["demo"],
    )
    common_collection: str | None = Field(
        default=None,
        description="The included common collection, if any.",
        examples=["shared"],
    )
    result_ids: list[str] = Field(
        description="Chunk ids returned, in rank order.",
        examples=[["verne-20k::chunk_42"]],
    )
    result_count: int = Field(description="Number of hits returned.", examples=[3])
    latency_ms: float | None = Field(
        default=None,
        description="Total time for the search, in ms.",
        examples=[12.4],
    )
    timing: dict[str, float] | None = Field(
        default=None,
        description="Per-phase latency breakdown, in ms.",
        examples=[{"embed_ms": 1.2, "search_ms": 8.4}],
    )
    request_id: str | None = Field(
        default=None,
        description="Correlation id of the original request.",
        examples=["req-5f3a-b812"],
    )
    replay_of: str | None = Field(
        default=None,
        description="Original query id when this entry is a replay.",
        examples=["q-1a2b-0001"],
    )
    executed_at: str = Field(
        description="When the search ran (ISO 8601).",
        examples=["2026-03-07T12:00:00Z"],
    )


class QueryLogSummary(BaseModel):
    """Compact entry for query-log listings."""
    query_id: str = Field(
        description="Id of the logged search.",
        examples=["q-7b2c-19af"],
    )
    query_text: str = Field(description="The search text.", examples=["captain nemo"])
    k: int = Field(description="Maximum number of hits requested.", examples=[5])
    result_count: int = Field(description="Number of hits returned.", examples=[3])
    latency_ms: float | None = Field(
        default=None,
        description="Total time for the search, in ms.",
        examples=[12.4],
    )
    request_id: str | None = Field(
        default=None,
        description="Correlation id of the original request.",
        examples=["req-5f3a-b812"],
    )
    replay_of: str | None = Field(
        default=None,
        description="Original query id when this entry is a replay.",
        examples=["q-1a2b-0001"],
    )
    executed_at: str = Field(
        description="When the search ran (ISO 8601).",
        examples=["2026-03-07T12:00:00Z"],
    )


class ListQueryLogsResponse(OkResponse):
    """A page of logged searches."""
    tenant: str = Field(description="Tenant scope of the listing.", examples=["demo"])
    collection: str | None = Field(
        default=None,
        description="Collection scope, if narrowed.",
        examples=["books"],
    )
    queries: list[QueryLogSummary] = Field(description="The logged searches.")
    count: int = Field(description="Number of entries returned.", examples=[3])


class GetQueryLogResponse(OkResponse):
    """A single logged search."""
    query: QueryLogEntry = Field(description="The logged search and its results.")


class QueryReplayResponse(OkResponse):
    """Results of re-running a logged search."""
    original_query_id: str = Field(
        description="Id of the search that was replayed.",
        examples=["q-7b2c-19af"],
    )
    replay_query_id: str | None = Field(
        default=None,
        description=(
            "Id assigned to this replay. Omitted when query logging is "
            "disabled."
        ),
        examples=["q-9f0a-22b1"],
    )
    matches: list[SearchResult] = Field(description="Hits from the replay.")
    timing: SearchTiming | None = Field(
        default=None,
        description="Per-phase latency breakdown.",
    )
    original_result_count: int = Field(
        description="Hit count of the original search.",
        examples=[3],
    )
    original_latency_ms: float | None = Field(
        default=None,
        description="Latency of the original search, in ms.",
        examples=[12.4],
    )


class MoveCollectionBody(BaseModel):
    """Request body for renaming a collection."""
    new_name: str = Field(description="New collection slug.", examples=["library"])


class CreateCollectionOptionsBody(BaseModel):
    """Optional settings when creating a collection."""
    display_name: str | None = Field(
        default=None,
        description="Human-friendly label for the collection.",
        examples=["Books"],
    )
    embedder_type: str | None = Field(
        default=None,
        description="Embedding backend to use for this collection.",
        examples=["sbert"],
    )
    embed_model: str | None = Field(
        default=None,
        description="Embedding model name.",
        examples=["all-MiniLM-L6-v2"],
    )
    embedder_config: dict[str, Any] | None = Field(
        default=None,
        description="Extra embedding options.",
        examples=[{"device": "cpu"}],
    )


class UpdateCollectionBody(BaseModel):
    """Request body for editing collection metadata."""
    display_name: str = Field(
        description="New human-friendly label.",
        examples=["Books"],
    )


class CreateCollectionResponse(OkResponse):
    """A newly created collection."""
    tenant: str = Field(
        description="Tenant that owns the collection.",
        examples=["demo"],
    )
    name: str = Field(description="Collection slug.", examples=["books"])
    collection: str = Field(description="Collection slug.", examples=["books"])
    display_name: str = Field(description="Human-friendly label.", examples=["Books"])
    embedder_type: str = Field(
        description="Embedding backend in use.",
        examples=["sbert"],
    )
    embed_model: str = Field(
        description="Embedding model name.",
        examples=["all-MiniLM-L6-v2"],
    )
    embedder_config: dict[str, Any] = Field(
        default_factory=dict,
        description="Extra embedding options.",
        examples=[{}],
    )


class DeleteCollectionResponse(OkResponse):
    """Confirmation of a deleted collection."""
    tenant: str = Field(
        description="Tenant that owned the collection.",
        examples=["demo"],
    )
    deleted: str = Field(
        description="Slug of the deleted collection.",
        examples=["books"],
    )


class MoveCollectionResponse(OkResponse):
    """Confirmation of a renamed collection."""
    tenant: str = Field(
        description="Tenant that owns the collection.",
        examples=["demo"],
    )
    old_name: str = Field(description="Previous collection slug.", examples=["books"])
    new_name: str = Field(description="New collection slug.", examples=["library"])


class UpdateCollectionResponse(OkResponse):
    """Confirmation of updated collection metadata."""
    tenant: str = Field(
        description="Tenant that owns the collection.",
        examples=["demo"],
    )
    name: str = Field(description="Collection slug.", examples=["books"])
    display_name: str = Field(description="Updated label.", examples=["Books"])


class CollectionSummary(BaseModel):
    """Compact entry for collection listings."""
    name: str = Field(description="Collection slug.", examples=["books"])
    display_name: str | None = Field(
        default=None,
        description="Human-friendly label.",
        examples=["Books"],
    )
    embedder_label: str | None = Field(
        default=None,
        description="Embedding backend and model in one label.",
        examples=["sbert:all-MiniLM-L6-v2"],
    )
    vector_space_key: str | None = Field(
        default=None,
        description="Identifier for the embedding model and size shared by "
        "this collection's vectors.",
        examples=["sbert:all-MiniLM-L6-v2:384"],
    )


class ListCollectionsResponse(OkResponse):
    """All collections in a tenant."""
    tenant: str = Field(description="Tenant scope of the listing.", examples=["demo"])
    collections: list[CollectionSummary] = Field(description="The collections.")
    count: int = Field(description="Number of collections.", examples=[2])


class CollectionDetailResponse(OkResponse):
    """Full detail for one collection."""
    tenant: str = Field(
        description="Tenant that owns the collection.",
        examples=["demo"],
    )
    name: str = Field(description="Collection slug.", examples=["books"])
    display_name: str | None = Field(
        default=None,
        description="Human-friendly label.",
        examples=["Books"],
    )
    embedder_type: str | None = Field(
        default=None,
        description="Embedding backend in use.",
        examples=["sbert"],
    )
    embed_model: str | None = Field(
        default=None,
        description="Embedding model name.",
        examples=["all-MiniLM-L6-v2"],
    )
    embedder_config: dict[str, Any] = Field(
        default_factory=dict,
        description="Extra embedding options.",
        examples=[{}],
    )
    vector_space_key: str | None = Field(
        default=None,
        description="Identifier for the embedding model and size shared by "
        "this collection's vectors.",
        examples=["sbert:all-MiniLM-L6-v2:384"],
    )
    created_at: str | None = Field(
        default=None,
        description="When the collection was created (ISO 8601).",
        examples=["2026-03-07T12:00:00Z"],
    )
    doc_count: int = Field(description="Number of documents.", examples=[3])
    chunk_count: int = Field(description="Number of chunks.", examples=[128])


class ListTenantsResponse(OkResponse):
    """All known tenants."""
    tenants: list[str] = Field(
        description="Tenant slugs.",
        examples=[["demo", "acme"]],
    )
    count: int = Field(description="Number of tenants.", examples=[2])


class EmbedderInventoryEntry(BaseModel):
    """A configured embedder instance exposed without credentials."""
    key: str = Field(
        description="Configured embedder instance key.",
        examples=["native"],
    )
    vector_key: str = Field(
        description="Vector-space key produced by this instance.",
        examples=["sbert:sentence-transformers/all-MiniLM-L6-v2:"],
    )
    embedder_type: str = Field(
        description="Embedder implementation type.",
        examples=["sbert"],
    )
    embed_model: str = Field(
        description="Embedding model name.",
        examples=["sentence-transformers/all-MiniLM-L6-v2"],
    )


class DefaultEmbedderInventory(BaseModel):
    """Default embedder resolution."""
    selector: str | None = Field(
        default=None,
        description="Configured embedder.default selector, if any.",
        examples=["native"],
    )
    vector_key: str = Field(
        description="Default vector-space key.",
        examples=["sbert:sentence-transformers/all-MiniLM-L6-v2:"],
    )
    instance_keys: list[str] = Field(
        description="Instance keys in the default routing pool.",
        examples=[["native"]],
    )


class EmbedderInventoryResponse(OkResponse):
    """Configured embedder instances available to a caller."""
    tenant: str | None = Field(
        default=None,
        description="Tenant scope for tenant-namespaced requests.",
        examples=["demo"],
    )
    embedders: list[EmbedderInventoryEntry] = Field(
        description="Configured embedder instances."
    )
    default: DefaultEmbedderInventory = Field(
        description="Resolved default embedder."
    )
    count: int = Field(description="Number of configured instances.", examples=[1])


class IngestDocumentResponse(OkResponse):
    """Result of ingesting a document."""
    tenant: str = Field(
        description="Tenant that owns the document.",
        examples=["demo"],
    )
    collection: str = Field(
        description="Collection the document went into.",
        examples=["books"],
    )
    docid: str = Field(
        description="Id of the ingested document.",
        examples=["verne-20k"],
    )
    chunks: int = Field(description="Number of chunks created.", examples=[42])


class IngestTextDocumentBody(BaseModel):
    """Raw-text document ingest request."""
    text: str = Field(
        description="Raw text to index as one document.",
        examples=["Captain Nemo commands the Nautilus."],
    )
    docid: str | None = Field(
        default=None,
        description="Optional stable document id; generated when omitted.",
        examples=["note-1"],
    )
    metadata: dict[str, Any] | None = Field(
        default=None,
        description="Optional user metadata stored with the document.",
        examples=[{"source": "manual"}],
    )


class BatchDocumentInput(BaseModel):
    """One raw-text document in a batch ingest request."""
    text: str = Field(
        description="Raw text to index as one document.",
        examples=["Captain Nemo commands the Nautilus."],
    )
    docid: str | None = Field(
        default=None,
        description="Optional stable document id; generated when omitted.",
        examples=["note-1"],
    )
    metadata: dict[str, Any] | None = Field(
        default=None,
        description="Optional user metadata stored with the document.",
        examples=[{"source": "manual"}],
    )


class BatchIngestDocumentsBody(BaseModel):
    """Batch raw-text document ingest request."""
    documents: list[BatchDocumentInput] = Field(
        description="Documents to ingest.",
        min_length=1,
        examples=[
            [
                {
                    "text": "Captain Nemo commands the Nautilus.",
                    "docid": "note-1",
                    "metadata": {"source": "manual"},
                }
            ]
        ],
    )


class BatchIngestDocumentResult(BaseModel):
    """Per-document result for batch ingest."""
    ok: bool = Field(description="Whether this document was ingested.")
    index: int = Field(description="Zero-based item index.", examples=[0])
    docid: str | None = Field(
        default=None,
        description="Document id, when known.",
        examples=["note-1"],
    )
    chunks: int | None = Field(
        default=None,
        description="Number of chunks indexed on success.",
        examples=[1],
    )
    code: str | None = Field(
        default=None,
        description="Failure code for this item.",
        examples=["no_text_extracted"],
    )
    error: str | None = Field(
        default=None,
        description="Failure message for this item.",
        examples=["no text extracted"],
    )


class BatchIngestDocumentsResponse(OkResponse):
    """Result of ingesting several raw-text documents."""
    tenant: str = Field(description="Tenant scope.", examples=["demo"])
    collection: str = Field(description="Collection scope.", examples=["books"])
    documents: list[BatchIngestDocumentResult] = Field(
        description="Per-document ingest results.",
    )
    count: int = Field(description="Number of items processed.", examples=[2])
    succeeded: int = Field(description="Number of successful items.", examples=[2])
    failed: int = Field(description="Number of failed items.", examples=[0])


class DeleteDocumentResponse(OkResponse):
    """Confirmation of a deleted document."""
    tenant: str = Field(
        description="Tenant that owned the document.",
        examples=["demo"],
    )
    collection: str = Field(description="Collection it was in.", examples=["books"])
    docid: str = Field(
        description="Id of the deleted document.",
        examples=["verne-20k"],
    )
    chunks_deleted: int = Field(description="Number of chunks removed.", examples=[42])


class GetDocumentResponse(OkResponse):
    """Detail for one document."""
    tenant: str = Field(description="Tenant that owns the document.", examples=["demo"])
    collection: str = Field(description="Collection it belongs to.", examples=["books"])
    docid: str = Field(description="Document id.", examples=["verne-20k"])
    version: int = Field(
        description="Revision, incremented on re-ingest.",
        examples=[1],
    )
    ingested_at: str = Field(
        description="When the document was last ingested (ISO 8601).",
        examples=["2026-03-07T12:00:00Z"],
    )
    metadata: dict[str, Any] = Field(
        description="User metadata stored with the document.",
        examples=[{"lang": "en"}],
    )
    chunk_ids: list[str] = Field(
        description="Chunk ids that make up the document.",
        examples=[["verne-20k::chunk_0", "verne-20k::chunk_1"]],
    )
    chunk_count: int = Field(description="Number of chunks.", examples=[42])


class ChunkSummary(BaseModel):
    """Compact entry for chunk listings."""
    rid: str = Field(description="Chunk id.", examples=["verne-20k::chunk_42"])
    chunk_path: str | None = Field(
        default=None,
        description="Position of the chunk within the document.",
        examples=["0/42"],
    )
    meta: dict[str, Any] = Field(
        description="Provenance and user metadata for the chunk.",
        examples=[{"offset": 21000}],
    )
    ingested_at: str = Field(
        description="When the chunk was ingested (ISO 8601).",
        examples=["2026-03-07T12:00:00Z"],
    )


class DocumentSummary(BaseModel):
    """Compact entry for document listings."""
    docid: str = Field(description="Document id.", examples=["verne-20k"])
    version: int = Field(
        description="Revision, incremented on re-ingest.",
        examples=[1],
    )
    ingested_at: str = Field(
        description="When the document was last ingested (ISO 8601).",
        examples=["2026-03-07T12:00:00Z"],
    )
    chunk_count: int = Field(description="Number of chunks.", examples=[42])


class ListDocumentsResponse(OkResponse):
    """All documents in a collection."""
    tenant: str = Field(description="Tenant scope of the listing.", examples=["demo"])
    collection: str = Field(description="Collection scope.", examples=["books"])
    documents: list[DocumentSummary] = Field(description="The documents.")
    count: int = Field(description="Number of documents.", examples=[3])


class ListChunksResponse(OkResponse):
    """All chunks of a document."""
    tenant: str = Field(description="Tenant scope of the listing.", examples=["demo"])
    collection: str = Field(description="Collection scope.", examples=["books"])
    docid: str = Field(
        description="Document the chunks belong to.",
        examples=["verne-20k"],
    )
    chunks: list[ChunkSummary] = Field(description="The chunks.")
    count: int = Field(description="Number of chunks.", examples=[42])


class GetChunkResponse(OkResponse):
    """Detail for one chunk."""
    tenant: str = Field(description="Tenant that owns the chunk.", examples=["demo"])
    collection: str = Field(description="Collection it belongs to.", examples=["books"])
    docid: str = Field(description="Document it belongs to.", examples=["verne-20k"])
    rid: str = Field(description="Chunk id.", examples=["verne-20k::chunk_42"])
    chunk_path: str | None = Field(
        default=None,
        description="Position of the chunk within the document.",
        examples=["0/42"],
    )
    meta: dict[str, Any] = Field(
        description="Provenance and user metadata for the chunk.",
        examples=[{"offset": 21000}],
    )
    ingested_at: str = Field(
        description="When the chunk was ingested (ISO 8601).",
        examples=["2026-03-07T12:00:00Z"],
    )


class RestoreArchiveResponse(OkResponse):
    """Confirmation of a restored archive."""


class ResetMetricsResponse(OkResponse):
    """Confirmation that metrics were reset."""
    reset_at: float = Field(
        description="When metrics were reset (Unix seconds).",
        examples=[1709815200.0],
    )
