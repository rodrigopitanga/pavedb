# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import functools
import json
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response

from pave.auth import (
    AuthContext,
    actor_of,
    auth_ctx,
    tenant_limit_gate,
    tenant_rate_limit,
)
from pave.log import ops_event
from pave.schemas import (
    MAX_SEARCH_K,
    GetQueryLogResponse,
    ListQueryLogsResponse,
    QueryReplayResponse,
    SearchBody,
    SearchResponse,
)
from pave.service import (
    get_query_log_entry_scoped as svc_get_query_log_entry_scoped,
    list_query_logs as svc_list_query_logs,
    replay_query_scoped as svc_replay_query_scoped,
    search as svc_search,
)
from pave.routes._deps import current_store
from pave.stores.base import BaseStore


def _capped_tenant(ctx: AuthContext) -> str | None:
    """Tenant whose cap applies, or None when the caller is uncapped.

    The shared-scope routes carry no ``{tenant}`` path parameter, so they take
    the gate by hand instead of via ``tenant_rate_limit``. That dependency
    bypasses the cap for admin and tenantless contexts, and this must match it
    exactly: under ``auth.mode=none`` the admin context still carries the
    default access tenant, so keying on the tenant alone would start capping
    the admin identity. ``tenant_limit_gate`` treats None as "no cap".
    """
    return None if ctx.is_admin else ctx.tenant


def build_search_router(
    cfg,
    do_search,
    error,
    resp,
    get_rid,
    trace,
) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/collections/{tenant}/{name}/queries",
        summary="List logged searches",
        description="Most recent first; omits result bodies, which one-by-id returns.",
        response_model=ListQueryLogsResponse,
        responses=resp(401, 403, 429, 500),
        tags=["Query Inspection"],
    )
    @ops_event(
        "list_query_logs",
        coll="name",
        request_id="rid",
    )
    def list_query_logs_handler(
        request: Request,
        tenant: str,
        name: str,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(tenant_rate_limit),
        store: BaseStore = Depends(current_store),
    ):
        result = svc_list_query_logs(
            store,
            tenant,
            name,
            limit,
            offset,
        )
        if not result.get("ok"):
            return error(
                500,
                result.get("code", "list_query_logs_failed"),
                result.get("error", "failed to list queries"),
                request=request,
                request_id=rid,
            )
        return trace(result, request, request_id=rid)

    @router.get(
        "/collections/{tenant}/{name}/queries/{query_id}",
        summary="Get a logged search",
        description="Includes the query text, filters, and the chunk ids it returned.",
        response_model=GetQueryLogResponse,
        responses=resp(401, 403, 404, 429, 500),
        tags=["Query Inspection"],
    )
    @ops_event(
        "get_query_log",
        coll="name",
        request_id="rid",
    )
    def get_query_log_handler(
        request: Request,
        tenant: str,
        name: str,
        query_id: str,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(tenant_rate_limit),
        store: BaseStore = Depends(current_store),
    ):
        result = svc_get_query_log_entry_scoped(
            store,
            tenant,
            name,
            query_id,
        )
        if not result.get("ok"):
            status = (
                404 if result.get("error_type") == "not_found"
                else 500
            )
            return error(
                status,
                result.get("code", "query_log_failed"),
                result.get("error", "failed to fetch query"),
                request=request,
                request_id=rid,
            )
        return trace(result, request, request_id=rid)

    @router.post(
        "/collections/{tenant}/{name}/queries/{query_id}/replay",
        summary="Replay a logged search",
        description=(
            "Re-runs the original query against current data; recorded as a "
            "new entry."
        ),
        response_model=QueryReplayResponse,
        responses=resp(401, 403, 404, 429, 500, 503),
        tags=["Query Inspection"],
    )
    @ops_event(
        "replay_query",
        coll="name",
        request_id="rid",
    )
    async def replay_query_handler(
        request: Request,
        tenant: str,
        name: str,
        query_id: str,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(tenant_rate_limit),
        store: BaseStore = Depends(current_store),
    ):
        return await do_search(
            functools.partial(
                svc_replay_query_scoped,
                store,
                tenant,
                name,
                query_id,
                request_id=rid,
                actor=actor_of(ctx),
            ),
            request=request,
            request_id=rid,
        )

    @router.post(
        "/collections/{tenant}/{name}/search",
        summary="Search a collection",
        description="Ranked retrieval matches with provenance and per-phase timing.",
        response_model=SearchResponse,
        response_model_exclude_none=True,
        responses=resp(401, 403, 429, 500, 503),
        tags=["Scoped Search"],
    )
    @ops_event(
        "search",
        coll="name",
        k=lambda kw, r: kw["body"].k,
        hits=lambda kw, r: (
            len(json.loads(r.body).get("matches", []))
            if getattr(r, "status_code", 400) < 400 else None
        ),
        request_id="rid",
    )
    async def search_post(
        request: Request,
        tenant: str,
        name: str,
        body: SearchBody,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(tenant_rate_limit),
        store: BaseStore = Depends(current_store),
    ):
        include_common = bool(body.include_common and cfg.common_enabled)
        return await do_search(
            functools.partial(
                svc_search,
                store,
                tenant,
                name,
                body.q,
                body.k,
                filters=body.filters,
                content_filter=(
                    body.content_filter.model_dump()
                    if body.content_filter is not None
                    else None
                ),
                mode=body.mode,
                v=body.v,
                include_common=include_common,
                common_tenant=cfg.common_tenant,
                common_collection=cfg.common_collection,
                request_id=rid,
                actor=actor_of(ctx),
            ),
            request=request,
            request_id=rid,
        )

    @router.get(
        "/collections/{tenant}/{name}/search",
        summary="Search a collection",
        description=(
            "Query-string form of the search; metadata filters are not "
            "accepted."
        ),
        response_model=SearchResponse,
        response_model_exclude_none=True,
        responses=resp(401, 403, 429, 500, 503),
        tags=["Scoped Search"],
    )
    @ops_event(
        "search",
        coll="name",
        k="k",
        hits=lambda kw, r: (
            len(json.loads(r.body).get("matches", []))
            if getattr(r, "status_code", 400) < 400 else None
        ),
        request_id="rid",
    )
    async def search_get(
        request: Request,
        tenant: str,
        name: str,
        q: str = Query(...),
        k: int = Query(5, ge=1, le=MAX_SEARCH_K),
        mode: Literal["vector", "hybrid"] = Query("vector"),
        include_common: bool = Query(
            False,
            description=(
                "Include configured common collection results when server "
                "common search is enabled."
            ),
        ),
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(tenant_rate_limit),
        store: BaseStore = Depends(current_store),
    ):
        include_common = bool(include_common and cfg.common_enabled)
        return await do_search(
            functools.partial(
                svc_search,
                store,
                tenant,
                name,
                q,
                k,
                filters=None,
                mode=mode,
                include_common=include_common,
                common_tenant=cfg.common_tenant,
                common_collection=cfg.common_collection,
                request_id=rid,
                actor=actor_of(ctx),
            ),
            request=request,
            request_id=rid,
        )

    @router.post(
        "/search",
        summary="Search shared collections",
        description=(
            "Searches the shared scope by text; empty when that scope is "
            "disabled."
        ),
        response_model=SearchResponse,
        response_model_exclude_none=True,
        responses=resp(400, 401, 403, 429, 500, 503),
        tags=["Global Search"],
    )
    @ops_event(
        "search_common",
        tenant=lambda kw, r: cfg.common_tenant,
        coll=lambda kw, r: cfg.common_collection,
        k=lambda kw, r: kw["body"].k,
        hits=lambda kw, r: (
            len(json.loads(r.body).get("matches", []))
            if getattr(r, "status_code", 400) < 400 else None
        ),
        request_id="rid",
    )
    async def search_common_post(
        request: Request,
        response: Response,
        body: SearchBody,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(auth_ctx),
        store: BaseStore = Depends(current_store),
    ):
        if body.v is not None:
            # A caller-chosen probe vector against the exact index returns
            # exact cosines, so d basis-vector probes rebuild every stored
            # vector. The shared corpus is queried by text only.
            return error(
                400,
                "vector_query_not_allowed",
                "shared search accepts a text query in 'q' only",
                request=request,
                request_id=rid,
            )
        if not cfg.common_enabled:
            return trace(
                {
                    "ok": True,
                    "matches": [],
                    "timing": {
                        "embed_ms": 0.0,
                        "search_ms": 0.0,
                        "filter_ms": 0.0,
                        "hydrate_ms": 0.0,
                    },
                },
                request,
                request_id=rid,
            )
        async with tenant_limit_gate(request, response, _capped_tenant(ctx)):
            return await do_search(
                functools.partial(
                    svc_search,
                    store,
                    cfg.common_tenant,
                    cfg.common_collection,
                    body.q,
                    body.k,
                    filters=body.filters,
                    content_filter=(
                        body.content_filter.model_dump()
                        if body.content_filter is not None
                        else None
                    ),
                    mode=body.mode,
                    request_id=rid,
                    actor=actor_of(ctx),
                    # The query log lives in the searched collection; here that is
                    # the shared tenant's, so logging would hand every caller's query
                    # text to whoever holds that tenant's key.
                    _log=False,
                ),
                request=request,
                request_id=rid,
            )

    @router.get(
        "/search",
        summary="Search shared collections",
        description="Query-string form of the shared search; filters are not accepted.",
        response_model=SearchResponse,
        response_model_exclude_none=True,
        responses=resp(401, 403, 429, 500, 503),
        tags=["Global Search"],
    )
    @ops_event(
        "search_common",
        tenant=lambda kw, r: cfg.common_tenant,
        coll=lambda kw, r: cfg.common_collection,
        k="k",
        hits=lambda kw, r: (
            len(json.loads(r.body).get("matches", []))
            if getattr(r, "status_code", 400) < 400 else None
        ),
        request_id="rid",
    )
    async def search_common_get(
        request: Request,
        response: Response,
        q: str = Query(...),
        k: int = Query(5, ge=1, le=MAX_SEARCH_K),
        mode: Literal["vector", "hybrid"] = Query("vector"),
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(auth_ctx),
        store: BaseStore = Depends(current_store),
    ):
        if not cfg.common_enabled:
            return trace(
                {
                    "ok": True,
                    "matches": [],
                    "timing": {
                        "embed_ms": 0.0,
                        "search_ms": 0.0,
                        "filter_ms": 0.0,
                        "hydrate_ms": 0.0,
                    },
                },
                request,
                request_id=rid,
            )
        async with tenant_limit_gate(request, response, _capped_tenant(ctx)):
            return await do_search(
                functools.partial(
                    svc_search,
                    store,
                    cfg.common_tenant,
                    cfg.common_collection,
                    q,
                    k,
                    filters=None,
                    mode=mode,
                    request_id=rid,
                    actor=actor_of(ctx),
                    # The query log lives in the searched collection; here that is
                    # the shared tenant's, so logging would hand every caller's query
                    # text to whoever holds that tenant's key.
                    _log=False,
                ),
                request=request,
                request_id=rid,
            )

    return router
