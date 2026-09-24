# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pydantic schemas for API request/response validation."""

from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator

# Upper bound on requested hits. Every caller-facing path that accepts k must
# use this, including replay of a query logged before the bound existed.
MAX_SEARCH_K = 1000


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
        description=(
            "How the score was made, prefixed by the mode that ranked the "
            "hit. vector: the cosine. boost: cosine plus the boost for the "
            "exact tokens matched. hybrid: the vector and lexical ranks, "
            "each contributing 1/(60+rank). Matched metadata filters and "
            "content filters follow after '; '."
        ),
        examples=["boost: cosine 0.712 + 0.067 for exact tokens 2/3"],
    )


class SearchTiming(BaseModel):
    """Per-phase latency breakdown, in ms."""
    embed_ms: float = Field(
        description="Time to turn the query into a vector.",
        examples=[1.2],
    )
    search_ms: float = Field(
        description="Time spent generating ranked candidates.",
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
    vector_search_ms: float | None = Field(
        default=None,
        description="Vector candidate-generation time for hybrid search.",
        examples=[1.4],
    )
    lexical_search_ms: float | None = Field(
        default=None,
        description="FTS5 candidate-generation time for hybrid search.",
        examples=[0.7],
    )


SearchModeName = Literal["vector", "boost", "hybrid"]


def mode_enum(schema: dict[str, Any]) -> None:
    """Document the modes on a request field typed as a plain string.

    The service checks the value, so an unknown mode answers
    invalid_search_mode rather than a generic validation error.
    """
    for branch in schema.get("anyOf", [schema]):
        if branch.get("type") == "string":
            branch["enum"] = ["vector", "boost", "hybrid"]


class SearchResponse(OkResponse):
    """Search results with optional timing."""
    matches: list[SearchResult] = Field(
        description="Ranked list of hits, best first.",
    )
    mode: SearchModeName | None = Field(
        default=None,
        description=(
            "Mode that ranked the matches: the requested one, or the "
            "collection's default. Scores are cosine similarity for vector "
            "and boost, reciprocal-rank fusion scores for hybrid."
        ),
        examples=["vector"],
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


class ContentFilter(BaseModel):
    """One additive chunk-text predicate for a search."""
    op: Literal["exact", "phrase", "prefix", "contains"] = Field(
        description="Text matching operation.",
        examples=["phrase"],
    )
    value: str = Field(
        min_length=1,
        description="Non-empty text value to match against stored chunks.",
        examples=["captain nemo"],
    )

    @model_validator(mode="after")
    def require_indexable_contains(self) -> "ContentFilter":
        if self.op == "contains" and len(self.value) < 3:
            raise ValueError("contains requires at least three characters")
        return self


class SearchBody(BaseModel):
    """Search request body."""
    q: str | None = Field(
        default=None,
        description="The search text.",
        examples=["captain nemo"],
    )
    v: list[float] | None = Field(
        default=None,
        description="Raw query vector (BYOV). Provide exactly one of q or v.",
        examples=[[0.1, 0.2, 0.3]],
    )
    k: int = Field(
        default=5,
        ge=1,
        le=MAX_SEARCH_K,
        description="Maximum number of hits.",
        examples=[5],
    )
    filters: dict[str, Any] | None = Field(
        default=None,
        description="Metadata equality filters to narrow results.",
        examples=[{"lang": "en"}],
    )
    content_filter: ContentFilter | None = Field(
        default=None,
        description=(
            "Additive exact, phrase, token-prefix, or substring predicate "
            "over stored chunk text."
        ),
        examples=[{"op": "phrase", "value": "captain nemo"}],
    )
    mode: str | None = Field(
        default=None,
        json_schema_extra=mode_enum,
        description=(
            "Ranking mode; omit for the collection's default. vector ranks by "
            "similarity; boost adds an exact-token nudge within the vector "
            "candidates; hybrid fuses vector and full-text rankings. boost "
            "and hybrid require a text query."
        ),
        examples=["hybrid"],
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

    @model_validator(mode="after")
    def require_q_or_v(self) -> "SearchBody":
        if (self.q is None) == (self.v is None):
            raise ValueError(
                "Provide exactly one of 'q' (text query) or 'v' (vector query)."
            )
        return self


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
    content_filter: ContentFilter | None = Field(
        default=None,
        description="Chunk-text predicate applied to the search.",
        examples=[{"op": "prefix", "value": "captain ne"}],
    )
    mode: SearchModeName = Field(
        default="vector",
        description="Ranking mode the search ran with.",
        examples=["hybrid"],
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
    mode: SearchModeName | None = Field(
        default=None,
        description="Mode the replay ran with: the one the original logged.",
        examples=["vector"],
    )
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


class ChunkingSpec(BaseModel):
    """How a collection cuts free text into chunks; fixed at creation."""
    model_config = {"extra": "forbid"}
    strategy: Literal["fixed", "none"] | None = Field(
        default=None,
        description=(
            "fixed: overlapping windows of characters. none: each text is one "
            "chunk. Omitted: the instance default."
        ),
        examples=["fixed"],
    )
    size: int | None = Field(
        default=None,
        description="fixed only: characters per chunk; omitted, the default.",
        examples=[1000],
    )
    overlap: int | None = Field(
        default=None,
        description=(
            "fixed only: characters shared with the previous chunk; at most "
            "a quarter of size. Omitted, the default."
        ),
        examples=[200],
    )


class CreateCollectionOptionsBody(BaseModel):
    """Optional settings when creating a collection."""
    display_name: str | None = Field(
        default=None,
        description="Human-friendly label for the collection.",
        examples=["Books"],
    )
    embedder: str | None = Field(
        default=None,
        description=(
            "Configured embedder selector shorthand: instance key, type, "
            "type:model, or full vector-space key. Cannot be combined with "
            "the detailed embedder fields."
        ),
        examples=["native"],
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
    search_mode: str | None = Field(
        default=None,
        json_schema_extra=mode_enum,
        description=(
            "Default ranking mode for searches that name none; fixed at "
            "creation. Must be a mode the instance serves. Defaults to vector."
        ),
        examples=["boost"],
    )
    chunking: ChunkingSpec | None = Field(
        default=None,
        description=(
            "Chunking for free text, fixed at creation and checked against "
            "the instance's bounds. Omitted: the instance default."
        ),
    )

    @model_validator(mode="after")
    def keep_embedder_selector_exclusive(self) -> "CreateCollectionOptionsBody":
        if self.embedder is not None and (
            self.embedder_type is not None
            or self.embed_model is not None
            or self.embedder_config is not None
        ):
            raise ValueError(
                "embedder cannot be combined with embedder_type, "
                "embed_model, or embedder_config"
            )
        return self


class UpdateCollectionBody(BaseModel):
    """Request body for editing collection metadata."""
    display_name: str = Field(
        description="New human-friendly label.",
        examples=["Books"],
    )


class ReindexCollectionBody(BaseModel):
    """New embedder vector space for a collection reindex job."""
    embedder_type: str | None = Field(
        default=None,
        description="Embedding backend to use for the rebuilt index.",
        examples=["sbert"],
    )
    embed_model: str | None = Field(
        default=None,
        description="Embedding model to use for the rebuilt index.",
        examples=["sentence-transformers/all-MiniLM-L6-v2"],
    )
    embedder_config: dict[str, Any] | None = Field(
        default=None,
        description="Space-affecting options for the target embedder.",
        examples=[{"passage_prefix": "passage: "}],
    )

    @model_validator(mode="after")
    def require_target(self) -> "ReindexCollectionBody":
        if (
            self.embedder_type is None
            and self.embed_model is None
            and self.embedder_config is None
        ):
            raise ValueError("a target embedder setting is required")
        return self


class ReindexTarget(BaseModel):
    """Persistable target vector-space identity for a reindex job."""
    embedder_type: str = Field(
        description="Embedding backend the job rebuilds the index with.",
        examples=["sbert"],
    )
    embed_model: str = Field(
        description="Embedding model the rebuilt index uses.",
        examples=["sentence-transformers/all-MiniLM-L6-v2"],
    )
    embedder_config: dict[str, Any] = Field(
        default_factory=dict,
        description="Space-affecting options for the target embedder.",
        examples=[{"passage_prefix": "passage: "}],
    )


class ReindexJobResponse(OkResponse):
    """Durable status and progress for one collection reindex job."""
    job_id: str = Field(
        description="Stable job identifier.",
        examples=["9f1c2b3a"],
    )
    tenant: str = Field(
        description="Tenant that owns the collection.",
        examples=["demo"],
    )
    collection: str = Field(
        description="Collection slug being reindexed.",
        examples=["books"],
    )
    status: Literal[
        "queued",
        "running",
        "pausing",
        "paused",
        "committing",
        "cancelling",
        "cancelled",
        "failed",
        "succeeded",
    ] = Field(
        description="Current lifecycle state of the reindex job.",
        examples=["running"],
    )
    target: ReindexTarget = Field(
        description="Vector space the job rebuilds the collection into.",
    )
    total_chunks: int = Field(
        ge=0,
        description="Total chunks the job will reindex.",
        examples=[128],
    )
    completed_chunks: int = Field(
        ge=0,
        description="Chunks reindexed so far.",
        examples=[64],
    )
    error: str | None = Field(
        default=None,
        description="Failure detail when the job status is failed.",
        examples=["embedder unavailable"],
    )
    created_at: str = Field(
        description="When the job was created (ISO 8601).",
        examples=["2026-09-22T07:40:00Z"],
    )
    updated_at: str = Field(
        description="When the job last advanced (ISO 8601).",
        examples=["2026-09-22T07:41:00Z"],
    )
    started_at: str | None = Field(
        default=None,
        description="When the job began running (ISO 8601), or null.",
        examples=["2026-09-22T07:40:05Z"],
    )
    finished_at: str | None = Field(
        default=None,
        description="When the job reached a terminal state (ISO 8601), or null.",
        examples=["2026-09-22T07:45:00Z"],
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
    search_mode: SearchModeName = Field(
        default="vector",
        description="Default ranking mode for searches that name none.",
        examples=["vector"],
    )
    chunking: dict[str, Any] | None = Field(
        default=None,
        description="Chunking the collection applies to free text.",
        examples=[{"strategy": "fixed", "size": 1000, "overlap": 200}],
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
        examples=["sbert:sentence-transformers/all-MiniLM-L6-v2"],
    )
    vector_space_key: str | None = Field(
        default=None,
        description="Identifier for the embedding model and any space-affecting "
        "config shared by this collection's vectors.",
        examples=["sbert:sentence-transformers/all-MiniLM-L6-v2:"],
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
        examples=["sentence-transformers/all-MiniLM-L6-v2"],
    )
    embedder_config: dict[str, Any] = Field(
        default_factory=dict,
        description="Extra embedding options.",
        examples=[{}],
    )
    vector_space_key: str | None = Field(
        default=None,
        description="Identifier for the embedding model and any space-affecting "
        "config shared by this collection's vectors.",
        examples=["sbert:sentence-transformers/all-MiniLM-L6-v2:"],
    )
    search_mode: SearchModeName = Field(
        default="vector",
        description="Default ranking mode for searches that name none.",
        examples=["vector"],
    )
    chunking: dict[str, Any] | None = Field(
        default=None,
        description="Chunking the collection applies to free text.",
        examples=[{"strategy": "fixed", "size": 1000, "overlap": 200}],
    )
    created_at: str | None = Field(
        default=None,
        description="When the collection was created (ISO 8601).",
        examples=["2026-03-07T12:00:00Z"],
    )
    pavedb_version: str | None = Field(
        default=None,
        description="PaveDB version that created the collection; unset for "
        "collections that predate this tag (P2-25).",
        examples=["0.9.6"],
    )
    schema_version: int | None = Field(
        default=None,
        description="Per-collection metadata schema version at creation time; "
        "unset for collections that predate this tag (P2-25).",
        examples=[8],
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
    """Raw-text or raw-vector document ingest request."""
    text: str | None = Field(
        default=None,
        description="Raw text to index as one document.",
        examples=["Captain Nemo commands the Nautilus."],
    )
    vector: list[float] | None = Field(
        default=None,
        description=(
            "Raw embedding vector (BYOV). Provide exactly one of text or vector."
        ),
        examples=[[0.1, 0.2, 0.3]],
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

    @model_validator(mode="after")
    def require_text_or_vector(self) -> "IngestTextDocumentBody":
        if (self.text is None) == (self.vector is None):
            raise ValueError("Provide exactly one of 'text' or 'vector'.")
        return self


class BatchDocumentInput(BaseModel):
    """One raw-text or raw-vector document in a batch ingest request."""
    text: str | None = Field(
        default=None,
        description="Raw text to index as one document.",
        examples=["Captain Nemo commands the Nautilus."],
    )
    vector: list[float] | None = Field(
        default=None,
        description=(
            "Raw embedding vector (BYOV). Provide exactly one of text or vector."
        ),
        examples=[[0.1, 0.2, 0.3]],
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

    @model_validator(mode="after")
    def require_text_or_vector(self) -> "BatchDocumentInput":
        if (self.text is None) == (self.vector is None):
            raise ValueError("Provide exactly one of 'text' or 'vector'.")
        return self


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


class RestoreCollectionArchiveResponse(OkResponse):
    """Confirmation of a restored collection archive."""
    tenant: str = Field(description="Destination tenant.", examples=["demo"])
    collection: str = Field(
        description="Destination collection slug.",
        examples=["books"],
    )


class ResetMetricsResponse(OkResponse):
    """Confirmation that metrics were reset."""
    reset_at: float = Field(
        description="When metrics were reset (Unix seconds).",
        examples=[1709815200.0],
    )
