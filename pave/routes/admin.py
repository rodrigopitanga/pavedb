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
from pave.metrics import reset as metrics_reset
from pave.schemas import (
    EmbedderInventoryResponse,
    GetQueryLogResponse,
    QueryReplayResponse,
    ListTenantsResponse,
    MoveCollectionBody,
    MoveCollectionResponse,
    ReindexCollectionBody,
    ReindexJobResponse,
    ResetMetricsResponse,
    RestoreArchiveResponse,
    RestoreCollectionArchiveResponse,
)
from pave.service import (
    ServiceError,
    cancel_reindex_job as svc_cancel_reindex_job,
    dump_collection_archive as svc_dump_collection_archive,
    dump_archive as svc_dump_archive,
    get_query_log_entry_scoped as svc_get_query_log_entry_scoped,
    get_reindex_job as svc_get_reindex_job,
    list_embedders as svc_list_embedders,
    list_tenants as svc_list_tenants,
    move_collection as svc_move_collection,
    pause_reindex_job as svc_pause_reindex_job,
    replay_query_scoped as svc_replay_query_scoped,
    resolve_query_home as svc_resolve_query_home,
    restore_collection_archive as svc_restore_collection_archive,
    restore_archive as svc_restore_archive,
    resume_reindex_job as svc_resume_reindex_job,
    start_collection_reindex as svc_start_collection_reindex,
)
from pave.routes._deps import (
    archive_restore_error,
    current_store,
    reindex_response as _reindex_response,
)
from pave.stores.base import BaseStore


def _cleanup_archive_dir(path: str | None) -> None:
    if path:
        shutil.rmtree(path, ignore_errors=True)


def build_admin_router(error, resp, get_rid, trace, do_search) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/admin/archive",
        summary="Download an archive",
        description="Streams the whole dataset as a zip; requires the admin key.",
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

        background = BackgroundTask(_cleanup_archive_dir, tmp_dir)
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=filename,
            background=background,
        )

    @router.put(
        "/admin/archive",
        summary="Restore an archive",
        description=(
            "Overwrites current data with the uploaded zip. The admin key "
            "is checked before body consumption; general request and "
            "document size limits do not apply."
        ),
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

    @router.get(
        "/admin/collections/{tenant}/{name}/archive",
        summary="Download a collection archive",
        description=(
            "Streams one collection and its portable manifest as a zip; "
            "requires the admin key."
        ),
        response_class=FileResponse,
        responses=resp(400, 401, 403, 404, 500),
        tags=["Instance Admin"],
    )
    @ops_event("dump_collection_archive", coll="name", request_id="rid")
    async def dump_collection_archive(
        request: Request,
        tenant: str,
        name: str,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(auth_ctx),
        store: BaseStore = Depends(current_store),
    ):
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
                svc_dump_collection_archive,
                store,
                tenant,
                name,
            )
        except ServiceError as exc:
            return error(
                400,
                exc.code,
                exc.message,
                request=request,
                request_id=rid,
            )
        except FileNotFoundError as exc:
            return error(
                404,
                "collection_not_found",
                str(exc),
                request=request,
                request_id=rid,
            )
        except ValueError as exc:
            return error(
                400,
                "collection_archive_invalid",
                str(exc),
                request=request,
                request_id=rid,
            )
        except Exception as exc:
            return error(
                500,
                "collection_archive_dump_failed",
                f"failed to dump collection archive: {exc}",
                request=request,
                request_id=rid,
            )
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=os.path.basename(archive_path),
            background=BackgroundTask(_cleanup_archive_dir, tmp_dir),
        )

    @router.post(
        "/admin/collections/{tenant}/{name}/archive",
        summary="Restore a collection archive",
        description=(
            "Replaces one collection from a same-version archive after local "
            "embedder preflight; requires the admin key."
        ),
        response_model=RestoreCollectionArchiveResponse,
        responses=resp(400, 401, 403, 500),
        tags=["Instance Admin"],
    )
    @ops_event("restore_collection_archive", coll="name", request_id="rid")
    async def restore_collection_archive(
        request: Request,
        tenant: str,
        name: str,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(auth_ctx),
        store: BaseStore = Depends(current_store),
        file: UploadFile = File(...),
    ):
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
                svc_restore_collection_archive,
                store,
                tenant,
                name,
                content,
            )
            return trace(out, request, request_id=rid)
        except Exception as exc:
            return archive_restore_error(exc, request, rid, error=error)

    @router.delete(
        "/admin/metrics",
        summary="Reset metrics",
        description="Zeroes request counters; requires the admin key.",
        response_model=ResetMetricsResponse,
        responses=resp(401, 403),
        tags=["Instance Admin"],
    )
    @ops_event("delete_metrics", coll=None, request_id="rid")
    def delete_metrics(
        request: Request,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(auth_ctx),
        store: BaseStore = Depends(current_store),
    ):
        if not ctx.is_admin:
            return error(
                403,
                "admin_required",
                "admin access required",
                request=request,
                request_id=rid,
            )
        return trace(metrics_reset(store), request, request_id=rid)

    @router.get(
        "/admin/tenants",
        summary="List tenants",
        description="Names every tenant on the instance; requires the admin key.",
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
        "/admin/embedders",
        summary="List embedder instances",
        description=(
            "Lists configured embedder instances and the resolved default; "
            "requires the admin key."
        ),
        response_model=EmbedderInventoryResponse,
        responses=resp(401, 403, 500),
        tags=["Instance Admin"],
    )
    @ops_event("list_embedders", coll=None, request_id="rid")
    def list_embedders(
        request: Request,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(auth_ctx),
    ):
        if not ctx.is_admin:
            return error(
                403,
                "admin_required",
                "admin access required",
                request=request,
                request_id=rid,
            )
        result = svc_list_embedders()
        if not result.get("ok"):
            return error(
                500,
                result.get("code", "list_embedders_failed"),
                result.get("error", "failed to list embedders"),
                request=request,
                request_id=rid,
            )
        return trace(result, request, request_id=rid)

    @router.post(
        "/admin/collections/{tenant}/{name}/move",
        summary="Rename a collection",
        description="Changes the slug, and therefore the URL; requires the admin key.",
        response_model=MoveCollectionResponse,
        responses=resp(400, 401, 403, 404, 409, 500),
        tags=["Instance Admin"],
    )
    @ops_event(
        "move_collection",
        coll="name",
        new_name=lambda kw, r: kw["body"].new_name,
        request_id="rid",
    )
    def move_collection(
        request: Request,
        tenant: str,
        name: str,
        body: MoveCollectionBody,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(auth_ctx),
        store: BaseStore = Depends(current_store),
    ):
        if not ctx.is_admin:
            return error(
                403,
                "admin_required",
                "admin access required",
                request=request,
                request_id=rid,
            )

        result = svc_move_collection(store, tenant, name, body.new_name)
        if not result.get("ok"):
            error_type = result.get("error_type", "invalid")
            status_map = {
                "not_found": 404,
                "conflict": 409,
                "invalid": 400,
                "failed": 500,
            }
            status_code = status_map.get(error_type, 500)
            return error(
                status_code,
                result.get("code", "move_invalid"),
                result.get("error", "failed to move collection"),
                request=request,
                request_id=rid,
            )
        return trace(result, request, request_id=rid)

    reindex_response = functools.partial(
        _reindex_response, error=error, trace=trace,
    )

    @router.post(
        "/admin/collections/{tenant}/{name}/reindex",
        status_code=202,
        summary="Reindex a collection",
        description=(
            "Starts an interruptible, resumable vector rebuild against a new "
            "embedder specification; requires the admin key."
        ),
        response_model=ReindexJobResponse,
        responses=resp(400, 401, 403, 404, 409, 500, 503),
        tags=["Instance Admin"],
    )
    @ops_event("start_collection_reindex", coll="name", request_id="rid")
    def start_collection_reindex(
        request: Request,
        tenant: str,
        name: str,
        body: ReindexCollectionBody,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(auth_ctx),
        store: BaseStore = Depends(current_store),
    ):
        if not ctx.is_admin:
            return error(
                403,
                "admin_required",
                "admin access required",
                request=request,
                request_id=rid,
            )
        result = svc_start_collection_reindex(
            store,
            tenant,
            name,
            embedder_type=body.embedder_type,
            embed_model=body.embed_model,
            embedder_config=body.embedder_config,
        )
        return reindex_response(result, request, rid)

    @router.get(
        "/admin/reindex/{job_id}",
        summary="Get reindex job status",
        description="Returns durable reindex progress; requires the admin key.",
        response_model=ReindexJobResponse,
        responses=resp(401, 403, 404, 500),
        tags=["Instance Admin"],
    )
    @ops_event("get_reindex_job", coll=None, request_id="rid")
    def get_reindex_job(
        request: Request,
        job_id: str,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(auth_ctx),
        store: BaseStore = Depends(current_store),
    ):
        if not ctx.is_admin:
            return error(
                403,
                "admin_required",
                "admin access required",
                request=request,
                request_id=rid,
            )
        return reindex_response(svc_get_reindex_job(store, job_id), request, rid)

    @router.post(
        "/admin/reindex/{job_id}/pause",
        summary="Pause a reindex job",
        description=(
            "Requests a pause after the current persisted batch; requires the "
            "admin key."
        ),
        response_model=ReindexJobResponse,
        responses=resp(401, 403, 404, 409, 500),
        tags=["Instance Admin"],
    )
    @ops_event("pause_reindex_job", coll=None, request_id="rid")
    def pause_reindex_job(
        request: Request,
        job_id: str,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(auth_ctx),
        store: BaseStore = Depends(current_store),
    ):
        if not ctx.is_admin:
            return error(
                403,
                "admin_required",
                "admin access required",
                request=request,
                request_id=rid,
            )
        return reindex_response(
            svc_pause_reindex_job(store, job_id),
            request,
            rid,
        )

    @router.post(
        "/admin/reindex/{job_id}/resume",
        summary="Resume a reindex job",
        description="Resumes from persisted batches; requires the admin key.",
        response_model=ReindexJobResponse,
        responses=resp(401, 403, 404, 409, 500),
        tags=["Instance Admin"],
    )
    @ops_event("resume_reindex_job", coll=None, request_id="rid")
    def resume_reindex_job(
        request: Request,
        job_id: str,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(auth_ctx),
        store: BaseStore = Depends(current_store),
    ):
        if not ctx.is_admin:
            return error(
                403,
                "admin_required",
                "admin access required",
                request=request,
                request_id=rid,
            )
        return reindex_response(
            svc_resume_reindex_job(store, job_id),
            request,
            rid,
        )

    @router.delete(
        "/admin/reindex/{job_id}",
        summary="Cancel a reindex job",
        description=(
            "Discards staged vectors without changing the live index; requires "
            "the admin key."
        ),
        response_model=ReindexJobResponse,
        responses=resp(401, 403, 404, 409, 500),
        tags=["Instance Admin"],
    )
    @ops_event("cancel_reindex_job", coll=None, request_id="rid")
    def cancel_reindex_job(
        request: Request,
        job_id: str,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(auth_ctx),
        store: BaseStore = Depends(current_store),
    ):
        if not ctx.is_admin:
            return error(
                403,
                "admin_required",
                "admin access required",
                request=request,
                request_id=rid,
            )
        return reindex_response(
            svc_cancel_reindex_job(store, job_id),
            request,
            rid,
        )

    @router.get(
        "/admin/queries/{query_id}",
        summary="Get any logged search",
        description=(
            "Finds a search by id without knowing its tenant; requires the "
            "admin key."
        ),
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
        summary="Replay any logged search",
        description=(
            "Re-runs a search found by id in any tenant; requires the "
            "admin key."
        ),
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
        # The replay really runs on the owner tenant's search capacity, so it
        # still takes that tenant's concurrency slot. Its moving-window budget
        # is a plan quota instead, and admin credentials bypass that
        # everywhere else, so an operator diagnosing a throttled tenant is
        # neither locked out by nor charged against it.
        async with tenant_limit_gate(
            request,
            response,
            tenant,
            charge_request_rate=False,
        ):
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
