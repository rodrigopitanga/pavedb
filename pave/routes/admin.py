# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import os
import shutil
import functools

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from pave.auth import AuthContext, actor_of, auth_ctx, tenant_limit_gate
from pave.log import ops_event
from pave.metrics import inc, reset as metrics_reset
from pave.schemas import (
    GetQueryLogResponse,
    QueryReplayResponse,
    ListTenantsResponse,
    ResetMetricsResponse,
    RestoreArchiveResponse,
)
from pave.service import (
    dump_archive as svc_dump_archive,
    get_query_log_entry_scoped as svc_get_query_log_entry_scoped,
    list_tenants as svc_list_tenants,
    replay_query_scoped as svc_replay_query_scoped,
    resolve_query_home as svc_resolve_query_home,
    restore_archive as svc_restore_archive,
)
from pave.stores.base import BaseStore


def build_admin_router(error, resp, get_rid, trace, do_search) -> APIRouter:
    router = APIRouter()

    def current_store(request: Request) -> BaseStore:
        return request.app.state.store

    @router.get(
        "/admin/archive",
        response_class=FileResponse,
        responses=resp(401, 403, 404, 500),
        tags=["Instance Admin"],
    )
    @ops_event("dump_archive", coll=None, request_id="rid")
    async def dump_archive(
        request: Request,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(auth_ctx),
        store: BaseStore = Depends(current_store),
    ):
        inc("requests_total")
        if not ctx.is_admin:
            return error(
                403,
                "admin_required",
                "admin access required",
                request=request,
                request_id=rid,
            )

        try:
            archive_path, tmp_dir = await run_in_threadpool(
                svc_dump_archive, store
            )
        except FileNotFoundError:
            return error(
                404,
                "data_dir_not_found",
                "data directory not found",
                request=request,
                request_id=rid,
            )
        except Exception as exc:
            return error(
                500,
                "archive_dump_failed",
                f"failed to dump archive: {exc}",
                request=request,
                request_id=rid,
            )

        filename = os.path.basename(archive_path)

        def cleanup(path: str | None) -> None:
            if not path:
                return
            shutil.rmtree(path, ignore_errors=True)

        background = BackgroundTask(cleanup, tmp_dir)
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=filename,
            background=background,
        )

    @router.put(
        "/admin/archive",
        response_model=RestoreArchiveResponse,
        responses=resp(400, 401, 403, 500),
        tags=["Instance Admin"],
    )
    @ops_event("restore_archive", coll=None, request_id="rid")
    async def restore_archive(
        request: Request,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(auth_ctx),
        store: BaseStore = Depends(current_store),
        file: UploadFile = File(...),
    ):
        inc("requests_total")
        if not ctx.is_admin:
            return error(
                403,
                "admin_required",
                "admin access required",
                request=request,
                request_id=rid,
            )

        content = await file.read()
        try:
            out = await run_in_threadpool(
                svc_restore_archive, store, content
            )
            return trace(out, request, request_id=rid)
        except ValueError as exc:
            return error(
                400,
                "archive_invalid",
                str(exc),
                request=request,
                request_id=rid,
            )
        except Exception as exc:
            return error(
                500,
                "archive_restore_failed",
                f"failed to restore archive: {exc}",
                request=request,
                request_id=rid,
            )

    @router.delete(
        "/admin/metrics",
        response_model=ResetMetricsResponse,
        responses=resp(401, 403),
        tags=["Instance Admin"],
    )
    @ops_event("delete_metrics", coll=None, request_id="rid")
    def delete_metrics(
        request: Request,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(auth_ctx),
    ):
        inc("requests_total")
        if not ctx.is_admin:
            return error(
                403,
                "admin_required",
                "admin access required",
                request=request,
                request_id=rid,
            )
        return trace(metrics_reset(), request, request_id=rid)

    @router.get(
        "/admin/tenants",
        response_model=ListTenantsResponse,
        responses=resp(401, 403, 500),
        tags=["Instance Admin"],
    )
    @ops_event("list_tenants", coll=None, request_id="rid")
    def list_tenants(
        request: Request,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(auth_ctx),
        store: BaseStore = Depends(current_store),
    ):
        inc("requests_total")
        if not ctx.is_admin:
            return error(
                403,
                "admin_required",
                "admin access required",
                request=request,
                request_id=rid,
            )
        result = svc_list_tenants(store)
        if not result.get("ok"):
            return error(
                500,
                result.get("code", "list_tenants_failed"),
                result.get("error", "failed to list tenants"),
                request=request,
                request_id=rid,
            )
        return trace(result, request, request_id=rid)

    @router.get(
        "/admin/queries/{query_id}",
        response_model=GetQueryLogResponse,
        responses=resp(401, 403, 404, 500),
        tags=["Query Admin"],
    )
    @ops_event("get_query_log", coll=None, request_id="rid")
    def get_query_log(
        request: Request,
        query_id: str,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(auth_ctx),
        store: BaseStore = Depends(current_store),
    ):
        inc("requests_total")
        if not ctx.is_admin:
            return error(
                403,
                "admin_required",
                "admin access required",
                request=request,
                request_id=rid,
            )

        home = svc_resolve_query_home(store, query_id)
        if home is None:
            return error(
                404,
                "query_not_found",
                f"query '{query_id}' not found",
                request=request,
                request_id=rid,
            )
        tenant, collection = home
        result = svc_get_query_log_entry_scoped(
            store,
            tenant,
            collection,
            query_id,
        )
        if not result.get("ok"):
            status = 404 if result.get("error_type") == "not_found" else 500
            return error(
                status,
                result.get("code", "query_log_failed"),
                result.get("error", "failed to fetch query"),
                request=request,
                request_id=rid,
            )
        return trace(result, request, request_id=rid)

    @router.post(
        "/admin/queries/{query_id}/replay",
        response_model=QueryReplayResponse,
        responses=resp(401, 403, 404, 429, 500, 503),
        tags=["Query Admin"],
    )
    @ops_event("replay_query", coll=None, request_id="rid")
    async def replay_query(
        request: Request,
        response: Response,
        query_id: str,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(auth_ctx),
        store: BaseStore = Depends(current_store),
    ):
        inc("requests_total")
        if not ctx.is_admin:
            return error(
                403,
                "admin_required",
                "admin access required",
                request=request,
                request_id=rid,
            )

        home = svc_resolve_query_home(store, query_id)
        if home is None:
            return error(
                404,
                "query_not_found",
                f"query '{query_id}' not found",
                request=request,
                request_id=rid,
            )
        tenant, collection = home
        async with tenant_limit_gate(request, response, tenant):
            return await do_search(
                functools.partial(
                    svc_replay_query_scoped,
                    store,
                    tenant,
                    collection,
                    query_id,
                    request_id=rid,
                    actor=actor_of(ctx),
                ),
                request=request,
                request_id=rid,
            )

    return router
