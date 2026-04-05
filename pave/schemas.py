# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pydantic schemas for API request/response validation."""

from typing import Any, Literal
from pydantic import BaseModel


class ErrorResponse(BaseModel):
    """API error envelope."""
    ok: Literal[False]
    code: str
    error: str
    details: dict[str, Any] | None = None
    request_id: str | None = None
    latency_ms: float | None = None


class OkResponse(BaseModel):
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


class SearchResponse(OkResponse):
    """API response for search endpoints."""
    matches: list[SearchResult]
    latency_ms: float | None = None
    request_id: str | None = None


class SearchBody(BaseModel):
    """API request body for search endpoints."""
    q: str
    k: int = 5
    filters: dict[str, Any] | None = None
    request_id: str | None = None


class RenameCollectionBody(BaseModel):
    """API request body for collection rename."""
    new_name: str


class CreateCollectionBody(BaseModel):
    """Optional API request body for collection creation."""
    embedder_type: str | None = None
    embed_model: str | None = None
