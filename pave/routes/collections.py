# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import functools
import os

from fastapi import APIRouter, Body, Depends, File, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from pave.auth import AuthContext, tenant_rate_limit
from pave.config import get_logger
from pave.log import ops_event
from pave.schemas import (
    CollectionDetailResponse,
    ReindexCollectionBody,
    ReindexJobResponse,
    RestoreCollectionArchiveResponse,
    CreateCollectionOptionsBody,
    CreateCollectionResponse,
    DeleteCollectionResponse,
    EmbedderInventoryResponse,
    ListCollectionsResponse,
    MoveCollectionBody,
    MoveCollectionResponse,
    UpdateCollectionBody,
    UpdateCollectionResponse,
)
from pave.service import (
    cancel_reindex_job as svc_cancel_reindex_job,
    create_collection as svc_create_collection,
    dump_collection_archive as svc_dump_collection_archive,
    get_reindex_job as svc_get_reindex_job,
    restore_collection_archive as svc_restore_collection_archive,
    start_collection_reindex as svc_start_collection_reindex,
    delete_collection as svc_delete_collection,
    get_collection_detail as svc_get_collection_detail,
    list_embedders as svc_list_embedders,
    list_collections as svc_list_collections,
    move_collection as svc_move_collection,
    update_collection as svc_update_collection,
)
from pave.routes._deps import (
    archive_restore_error,
    current_store,
    operation_gate,
    reindex_response as _reindex_response,
)
from pave.stores.base import BaseStore

log = get_logger()


def build_collections_router(error, resp, get_rid, trace) -> APIRouter:
    router = APIRouter()
    reindex_response = functools.partial(
        _reindex_response, error=error, trace=trace,
    )

    @router.get(
        "/collections/{tenant}",
        summary="List collections",
        description="One entry per collection, with display name and embedding model.",
        response_model=ListCollectionsResponse,
        responses=resp(401, 403, 429, 500),
        tags=["Collection Catalog"],
    )
    @ops_event("list_collections", coll=None, request_id="rid")
    def list_collections(
        request: Request,
        tenant: str,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(tenant_rate_limit),
        store: BaseStore = Depends(current_store),
    ):
        result = svc_list_collections(store, tenant)
        if not result.get("ok"):
            return error(
                500,
                result.get("code", "list_collections_failed"),
                result.get("error", "failed to list collections"),
                request=request,
                request_id=rid,
            )
        return trace(result, request, request_id=rid)

    @router.get(
        "/collections/{tenant}/{name}/detail",
        summary="Get collection detail",
        description="Settings plus document and chunk counts.",
        response_model=CollectionDetailResponse,
        responses=resp(401, 403, 404, 429, 500),
        tags=["Collection Catalog"],
    )
    @ops_event(
        "get_collection_detail",
        coll="name",
        request_id="rid",
    )
    def get_collection_detail(
        request: Request,
        tenant: str,
        name: str,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(tenant_rate_limit),
        store: BaseStore = Depends(current_store),
    ):
        result = svc_get_collection_detail(store, tenant, name)
        if not result.get("ok"):
            error_type = result.get("error_type", "failed")
            status_map = {
                "not_found": 404,
                "failed": 500,
            }
            return error(
                status_map.get(error_type, 500),
                result.get("code", "get_collection_detail_failed"),
                result.get("error", "failed to get collection detail"),
                request=request,
                request_id=rid,
            )
        return trace(result, request, request_id=rid)

    @router.post(
        "/collections/{tenant}/{name}",
        status_code=201,
        summary="Create a collection",
        description=(
            "Optionally sets the display name and selects an embedder by "
            "shorthand or detailed fields; returns 201."
        ),
        response_model=CreateCollectionResponse,
        responses=resp(400, 401, 403, 429, 500, 503),
        tags=["Collection Catalog"],
    )
    @ops_event("create_collection", request_id="rid")
    def create_collection(
        request: Request,
        tenant: str,
        name: str,
        body: CreateCollectionOptionsBody | None = Body(None),
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(tenant_rate_limit),
        store: BaseStore = Depends(current_store),
    ):
        result = svc_create_collection(
            store,
            tenant,
            name,
            display_name=body.display_name if body else None,
            embedder=body.embedder if body else None,
            embedder_type=body.embedder_type if body else None,
            embed_model=body.embed_model if body else None,
            embedder_config=body.embedder_config if body else None,
        )
        if not result.get("ok"):
            error_type = result.get("error_type", "failed")
            status_map = {
                "invalid": 400,
                "unavailable": 503,
                "limit": 429,
                "failed": 500,
            }
            return error(
                status_map.get(error_type, 500),
                result.get("code", "create_collection_failed"),
                result.get("error", "failed to create collection"),
                request=request,
                request_id=rid,
            )
        return trace(result, request, request_id=rid)

    @router.patch(
        "/collections/{tenant}/{name}",
        summary="Update a collection",
        description="Changes editable metadata only; does not re-index documents.",
        response_model=UpdateCollectionResponse,
        responses=resp(400, 401, 403, 404, 429, 500),
        tags=["Collection Catalog"],
    )
    @ops_event(
        "update_collection",
        coll="name",
        request_id="rid",
    )
    def update_collection(
        request: Request,
        tenant: str,
        name: str,
        body: UpdateCollectionBody,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(tenant_rate_limit),
        store: BaseStore = Depends(current_store),
    ):
        result = svc_update_collection(
            store,
            tenant,
            name,
            display_name=body.display_name,
        )
        if not result.get("ok"):
            error_type = result.get("error_type", "failed")
            status_map = {
                "not_found": 404,
                "invalid": 400,
                "failed": 500,
            }
            return error(
                status_map.get(error_type, 500),
                result.get("code", "update_collection_failed"),
                result.get("error", "failed to update collection"),
                request=request,
                request_id=rid,
            )
        return trace(result, request, request_id=rid)

    @router.post(
        "/collections/{tenant}/{name}/move",
        summary="Rename a collection",
        description="Changes the collection slug and therefore its URL.",
        response_model=MoveCollectionResponse,
        responses=resp(400, 401, 403, 404, 409, 429, 500),
        tags=["Collection Catalog"],
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
        ctx: AuthContext = Depends(tenant_rate_limit),
        store: BaseStore = Depends(current_store),
    ):
        result = svc_move_collection(store, tenant, name, body.new_name)
        if not result.get("ok"):
            status_map = {
                "not_found": 404,
                "conflict": 409,
                "invalid": 400,
                "failed": 500,
            }
            return error(
                status_map.get(result.get("error_type"), 500),
                result.get("code", "move_collection_failed"),
                result.get("error", "failed to move collection"),
                request=request,
                request_id=rid,
            )
        return trace(result, request, request_id=rid)

    @router.delete(
        "/collections/{tenant}/{name}",
        summary="Delete a collection",
        description="Permanently removes the collection and every document in it.",
        response_model=DeleteCollectionResponse,
        responses=resp(400, 401, 403, 404, 429, 500),
        tags=["Collection Catalog"],
    )
    @ops_event("delete_collection", request_id="rid")
    def delete_collection(
        request: Request,
        tenant: str,
        name: str,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(tenant_rate_limit),
        store: BaseStore = Depends(current_store),
    ):
        result = svc_delete_collection(store, tenant, name)
        if not result.get("ok"):
            status_map = {"not_found": 404, "invalid": 400}
            return error(
                status_map.get(result.get("error_type"), 500),
                result.get("code", "delete_collection_failed"),
                result.get("error", "failed to delete collection"),
                request=request,
                request_id=rid,
            )
        return trace(result, request, request_id=rid)

    @router.get(
        "/embedders/{tenant}",
        summary="List embedders available to a tenant",
        description=(
            "Lists configured embedder instances currently available in this "
            "tenant namespace."
        ),
        response_model=EmbedderInventoryResponse,
        responses=resp(401, 403, 429, 500),
        tags=["Collection Catalog"],
    )
    @ops_event("list_embedders", coll=None, request_id="rid")
    def list_embedders(
        request: Request,
        tenant: str,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(tenant_rate_limit),
    ):
        result = svc_list_embedders(tenant=tenant)
        if not result.get("ok"):
            return error(
                500,
                result.get("code", "list_embedders_failed"),
                result.get("error", "failed to list embedders"),
                request=request,
                request_id=rid,
            )
        return trace(result, request, request_id=rid)


    def _op_window(store, request, tenant, op, field, window_seconds, rid):
        """Charge one use of an expensive op; None means admitted."""
        charge = getattr(store, "charge_operation_window", None)
        if charge is None:
            return None
        decision = charge(tenant, op, field, window_seconds)
        if decision is None or decision.allowed:
            return None
        return error(
            429,
            f"{op}_rate_limited",
            f"{op} limit reached: {decision.limit} per "
            f"{decision.window_seconds}s; retry in {decision.retry_after}s",
            request=request,
            request_id=rid,
        )

    def _op_slot(store, request, tenant, op, field, window_seconds, rid):
        """Concurrency slot + windowed charge for an expensive op.

        Order matters: the slot is reversible, the charge is not, so a
        request refused on concurrency never spends quota.
        """
        slot, refused = operation_gate(request, tenant, op)
        if refused is not None:
            source, active, limit = refused
            return None, error(
                503,
                f"{op}_capacity",
                f"{source} reached ({active}/{limit}); retry shortly",
                request=request,
                request_id=rid,
            )
        limited = _op_window(
            store, request, tenant, op, field, window_seconds, rid,
        )
        if limited is not None:
            slot.release()
            return None, limited
        return slot, None

    @router.get(
        "/collections/{tenant}/{name}/archive",
        summary="Download this collection as a portable archive",
        description=(
            "Streams the collection and its manifest as a zip — the same "
            "portable format the admin archive uses, scoped to your own "
            "tenant. Restore it into any PaveDB of the same version."
        ),
        response_class=FileResponse,
        responses=resp(400, 401, 403, 404, 429, 500, 503),
        tags=["Collection Portability"],
    )
    @ops_event("dump_collection_archive", coll="name", request_id="rid")
    async def dump_own_collection_archive(
        request: Request,
        tenant: str,
        name: str,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(tenant_rate_limit),
        store: BaseStore = Depends(current_store),
    ):
        slot, refused = _op_slot(
            store, request, tenant, "archive", "max_archives_per_day",
            86_400, rid,
        )
        if refused is not None:
            return refused
        try:
            archive_path, tmp_dir = await run_in_threadpool(
                svc_dump_collection_archive,
                store,
                tenant,
                name,
            )
        except FileNotFoundError as exc:
            slot.release()
            return error(
                404, "collection_not_found", str(exc),
                request=request, request_id=rid,
            )
        except ValueError as exc:
            slot.release()
            return error(
                400, "collection_archive_invalid", str(exc),
                request=request, request_id=rid,
            )
        except Exception:
            slot.release()
            log.warning(
                "collection archive dump failed tenant=%s coll=%s request_id=%s",
                tenant, name, rid, exc_info=True,
            )
            return error(
                500, "collection_archive_dump_failed",
                "failed to dump collection archive",
                request=request, request_id=rid,
            )

        def _finish() -> None:
            try:
                import shutil

                shutil.rmtree(tmp_dir, ignore_errors=True)
            finally:
                slot.release()

        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=os.path.basename(archive_path),
            background=BackgroundTask(_finish),
        )

    @router.post(
        "/collections/{tenant}/{name}/archive",
        summary="Restore this collection from a portable archive",
        description=(
            "Replaces the collection from a same-version archive after "
            "embedder preflight, scoped to your own tenant."
        ),
        response_model=RestoreCollectionArchiveResponse,
        responses=resp(400, 401, 403, 413, 429, 500, 503),
        tags=["Collection Portability"],
    )
    @ops_event("restore_collection_archive", coll="name", request_id="rid")
    async def restore_own_collection_archive(
        request: Request,
        tenant: str,
        name: str,
        file: UploadFile = File(...),
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(tenant_rate_limit),
        store: BaseStore = Depends(current_store),
    ):
        slot, refused = _op_slot(
            store, request, tenant, "archive", "max_archives_per_day",
            86_400, rid,
        )
        if refused is not None:
            return refused
        try:
            content = await file.read()
            out = await run_in_threadpool(
                svc_restore_collection_archive,
                store,
                tenant,
                name,
                content,
            )
        except Exception as exc:
            return archive_restore_error(exc, request, rid, error=error)
        finally:
            slot.release()
        return trace(out, request, request_id=rid)

    @router.post(
        "/collections/{tenant}/{name}/reindex",
        summary="Rebuild this collection in a new vector space",
        description=(
            "Starts a resumable reindex job that re-embeds the collection "
            "into the requested embedder space (same rules as create). "
            "Operator routing decides which instance executes the embeds."
        ),
        response_model=ReindexJobResponse,
        responses=resp(400, 401, 403, 404, 409, 429, 500, 503),
        tags=["Collection Portability"],
    )
    @ops_event("start_collection_reindex", coll="name", request_id="rid")
    async def start_own_collection_reindex(
        request: Request,
        tenant: str,
        name: str,
        body: ReindexCollectionBody,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(tenant_rate_limit),
        store: BaseStore = Depends(current_store),
    ):
        slot, refused = _op_slot(
            store, request, tenant, "reindex", "max_reindex_per_month",
            2_592_000, rid,
        )
        if refused is not None:
            return refused
        try:
            out = await run_in_threadpool(
                svc_start_collection_reindex,
                store,
                tenant,
                name,
                embedder_type=body.embedder_type,
                embed_model=body.embed_model,
                embedder_config=body.embedder_config,
            )
        finally:
            slot.release()
        return reindex_response(out, request, rid)

    def _own_reindex_job(store, tenant, name, job_id):
        out = svc_get_reindex_job(store, job_id)
        if not out.get("ok"):
            return None
        job = out.get("job") or out
        if job.get("tenant") != tenant or job.get("collection") != name:
            return None
        return out

    @router.get(
        "/collections/{tenant}/{name}/reindex/{job_id}",
        summary="Inspect this collection's reindex job",
        description=(
            "Progress and state of a reindex job started on this "
            "collection; jobs from other tenants or collections answer 404."
        ),
        response_model=ReindexJobResponse,
        responses=resp(401, 403, 404, 429, 500),
        tags=["Collection Portability"],
    )
    @ops_event("get_reindex_job", coll="name", request_id="rid")
    def get_own_reindex_job(
        request: Request,
        tenant: str,
        name: str,
        job_id: str,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(tenant_rate_limit),
        store: BaseStore = Depends(current_store),
    ):
        out = _own_reindex_job(store, tenant, name, job_id)
        if out is None:
            return error(
                404, "reindex_job_not_found", "no such reindex job here",
                request=request, request_id=rid,
            )
        return out

    @router.delete(
        "/collections/{tenant}/{name}/reindex/{job_id}",
        summary="Cancel this collection's reindex job",
        description=(
            "Stops the job and keeps the collection serving from its "
            "current index; jobs from other tenants or collections "
            "answer 404."
        ),
        response_model=ReindexJobResponse,
        responses=resp(400, 401, 403, 404, 409, 429, 500),
        tags=["Collection Portability"],
    )
    @ops_event("cancel_reindex_job", coll="name", request_id="rid")
    def cancel_own_reindex_job(
        request: Request,
        tenant: str,
        name: str,
        job_id: str,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(tenant_rate_limit),
        store: BaseStore = Depends(current_store),
    ):
        if _own_reindex_job(store, tenant, name, job_id) is None:
            return error(
                404, "reindex_job_not_found", "no such reindex job here",
                request=request, request_id=rid,
            )
        return reindex_response(
            svc_cancel_reindex_job(store, job_id), request, rid,
        )

    return router
