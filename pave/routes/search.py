# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import functools
import json

from fastapi import APIRouter, Depends, Query, Request

from pave.auth import AuthContext, auth_ctx, tenant_rate_limit
from pave.log import ops_event
from pave.metrics import inc
from pave.schemas import (
    GetQueryLogResponse,
    ListQueryLogsResponse,
    QueryReplayResponse,
    SearchBody,
    SearchResponse,
)
from pave.service import (
    get_query_log_entry as svc_get_query_log_entry,
    list_query_logs as svc_list_query_logs,
    replay_query as svc_replay_query,
    search as svc_search,
)
from pave.stores.base import BaseStore


def build_search_router(
    cfg,
    do_search,
    error,
    resp,
    get_rid,
    trace,
) -> APIRouter:
    router = APIRouter()

    def current_store(request: Request) -> BaseStore:
        return request.app.state.store

    @router.get(
        "/collections/{tenant}/{name}/queries",
        response_model=ListQueryLogsResponse,
        responses=resp(401, 403, 429, 500),
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
        inc("requests_total")
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
        response_model=GetQueryLogResponse,
        responses=resp(401, 403, 404, 429, 500),
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
        inc("requests_total")
        result = svc_get_query_log_entry(store, tenant, name, query_id)
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
        response_model=QueryReplayResponse,
        responses=resp(401, 403, 404, 429, 500, 503),
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
        inc("requests_total")
        return await do_search(
            functools.partial(
                svc_replay_query,
                store,
                tenant,
                name,
                query_id,
                request_id=rid,
            ),
            request=request,
            request_id=rid,
        )

    @router.post(
        "/collections/{tenant}/{name}/search",
        response_model=SearchResponse,
        responses=resp(401, 403, 429, 500, 503),
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
        inc("requests_total")
        include_common = bool(cfg.common_enabled)
        return await do_search(
            functools.partial(
                svc_search,
                store,
                tenant,
                name,
                body.q,
                body.k,
                filters=body.filters,
                include_common=include_common,
                common_tenant=cfg.common_tenant,
                common_collection=cfg.common_collection,
                request_id=rid,
            ),
            request=request,
            request_id=rid,
        )

    @router.get(
        "/collections/{tenant}/{name}/search",
        response_model=SearchResponse,
        responses=resp(401, 403, 429, 500, 503),
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
        k: int = Query(5, ge=1),
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(tenant_rate_limit),
        store: BaseStore = Depends(current_store),
    ):
        inc("requests_total")
        include_common = bool(cfg.common_enabled)
        return await do_search(
            functools.partial(
                svc_search,
                store,
                tenant,
                name,
                q,
                k,
                filters=None,
                include_common=include_common,
                common_tenant=cfg.common_tenant,
                common_collection=cfg.common_collection,
                request_id=rid,
            ),
            request=request,
            request_id=rid,
        )

    @router.post(
        "/search",
        response_model=SearchResponse,
        responses=resp(401, 403, 500, 503),
    )
    async def search_common_post(
        request: Request,
        body: SearchBody,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(auth_ctx),
        store: BaseStore = Depends(current_store),
    ):
        inc("requests_total")
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
        return await do_search(
            functools.partial(
                svc_search,
                store,
                cfg.common_tenant,
                cfg.common_collection,
                body.q,
                body.k,
                filters=body.filters,
                request_id=rid,
            ),
            request=request,
            request_id=rid,
        )

    @router.get(
        "/search",
        response_model=SearchResponse,
        responses=resp(401, 403, 500, 503),
    )
    async def search_common_get(
        request: Request,
        q: str = Query(...),
        k: int = Query(5, ge=1),
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(auth_ctx),
        store: BaseStore = Depends(current_store),
    ):
        inc("requests_total")
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
        return await do_search(
            functools.partial(
                svc_search,
                store,
                cfg.common_tenant,
                cfg.common_collection,
                q,
                k,
                filters=None,
                request_id=rid,
            ),
            request=request,
            request_id=rid,
        )

    return router
