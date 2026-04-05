# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import functools
import json

from fastapi import APIRouter, Depends, Header, Query, Request

from pave.auth import AuthContext, auth_ctx, tenant_rate_limit
from pave.log import ops_event
from pave.metrics import inc
from pave.schemas import SearchBody, SearchResponse
from pave.service import search as svc_search
from pave.stores.base import BaseStore


def build_search_router(cfg, do_search, resp) -> APIRouter:
    router = APIRouter()

    def current_store(request: Request) -> BaseStore:
        return request.app.state.store

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
        request_id=lambda kw, r: (
            kw["body"].request_id or kw.get("x_request_id")
        ),
    )
    async def search_post(
        tenant: str,
        name: str,
        body: SearchBody,
        x_request_id: str | None = Header(None, alias="X-Request-ID"),
        ctx: AuthContext = Depends(tenant_rate_limit),
        store: BaseStore = Depends(current_store),
    ):
        inc("requests_total")
        request_id = body.request_id or x_request_id
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
                request_id=request_id,
            )
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
        request_id="x_request_id",
    )
    async def search_get(
        tenant: str,
        name: str,
        q: str = Query(...),
        k: int = Query(5, ge=1),
        x_request_id: str | None = Header(None, alias="X-Request-ID"),
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
                request_id=x_request_id,
            )
        )

    @router.post(
        "/search",
        response_model=SearchResponse,
        responses=resp(401, 403, 500, 503),
    )
    async def search_common_post(
        body: SearchBody,
        x_request_id: str | None = Header(None, alias="X-Request-ID"),
        ctx: AuthContext = Depends(auth_ctx),
        store: BaseStore = Depends(current_store),
    ):
        inc("requests_total")
        request_id = body.request_id or x_request_id
        if not cfg.common_enabled:
            return {
                "ok": True,
                "matches": [],
                "latency_ms": 0.0,
                "timing": {
                    "embed_ms": 0.0,
                    "search_ms": 0.0,
                    "filter_ms": 0.0,
                    "hydrate_ms": 0.0,
                },
                "request_id": request_id,
            }
        return await do_search(
            functools.partial(
                svc_search,
                store,
                cfg.common_tenant,
                cfg.common_collection,
                body.q,
                body.k,
                filters=body.filters,
                request_id=request_id,
            )
        )

    @router.get(
        "/search",
        response_model=SearchResponse,
        responses=resp(401, 403, 500, 503),
    )
    async def search_common_get(
        q: str = Query(...),
        k: int = Query(5, ge=1),
        x_request_id: str | None = Header(None, alias="X-Request-ID"),
        ctx: AuthContext = Depends(auth_ctx),
        store: BaseStore = Depends(current_store),
    ):
        inc("requests_total")
        if not cfg.common_enabled:
            return {
                "ok": True,
                "matches": [],
                "latency_ms": 0.0,
                "timing": {
                    "embed_ms": 0.0,
                    "search_ms": 0.0,
                    "filter_ms": 0.0,
                    "hydrate_ms": 0.0,
                },
                "request_id": x_request_id,
            }
        return await do_search(
            functools.partial(
                svc_search,
                store,
                cfg.common_tenant,
                cfg.common_collection,
                q,
                k,
                filters=None,
                request_id=x_request_id,
            )
        )

    return router
