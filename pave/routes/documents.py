# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import asyncio
import functools
import json

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile

from pave.auth import AuthContext, tenant_rate_limit
from pave.log import ops_event
from pave.metrics import inc
from pave.schemas import (
    DeleteDocumentResponse,
    ListDocumentsResponse,
    GetDocumentResponse,
    IngestDocumentResponse,
    GetChunkResponse,
    ListChunksResponse,
)
from pave.service import (
    ServiceError,
    delete_document as svc_delete_document,
    get_chunk as svc_get_chunk,
    get_chunk_content as svc_get_chunk_content,
    get_document as svc_get_document,
    ingest_document as svc_ingest_document,
    list_chunks as svc_list_chunks,
    list_documents as svc_list_documents,
)
from pave.stores.base import BaseStore


def build_documents_router(cfg, error, resp, get_rid, trace) -> APIRouter:
    router = APIRouter()

    def current_store(request: Request) -> BaseStore:
        return request.app.state.store

    @router.get(
        "/collections/{tenant}/{collection}/documents",
        response_model=ListDocumentsResponse,
        responses=resp(401, 403, 429, 500),
        tags=["Documents"],
    )
    @ops_event(
        "list_docs",
        coll="collection",
        request_id="rid",
    )
    def list_documents(
        request: Request,
        tenant: str,
        collection: str,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(tenant_rate_limit),
        store: BaseStore = Depends(current_store),
    ):
        inc("requests_total")
        result = svc_list_documents(store, tenant, collection)
        if not result.get("ok"):
            return error(
                500,
                result.get("code", "list_documents_failed"),
                result.get("error", "failed to list documents"),
                request=request,
                request_id=rid,
            )
        return trace(result, request, request_id=rid)

    @router.post(
        "/collections/{tenant}/{collection}/documents",
        status_code=201,
        response_model=IngestDocumentResponse,
        responses=resp(400, 401, 403, 413, 429, 500, 503),
        tags=["Documents"],
    )
    @ops_event(
        "ingest",
        coll="collection",
        docid=lambda kw, r: (
            kw.get("docid") or getattr(kw.get("file"), "filename", None)
        ),
        chunks=lambda kw, r: (
            r.get("chunks") if isinstance(r, dict) and r.get("ok") else None
        ),
        request_id="rid",
    )
    async def ingest_document(
        request: Request,
        tenant: str,
        collection: str,
        file: UploadFile = File(...),
        docid: str | None = Form(None),
        metadata: str | None = Form(None),
        csv_has_header: str | None = Query(None, pattern="^(auto|yes|no)$"),
        csv_meta_cols: str | None = Query(None),
        csv_include_cols: str | None = Query(None),
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(tenant_rate_limit),
        store: BaseStore = Depends(current_store),
    ):
        inc("requests_total")
        meta_obj = None
        if metadata:
            try:
                meta_obj = json.loads(metadata)
            except Exception as e:
                return error(
                    400,
                    "invalid_metadata_json",
                    f"invalid metadata json: {e}",
                    request=request,
                    request_id=rid,
                )

        content = await file.read()

        max_mb = float(cfg.get("ingest.max_file_size_mb"))
        max_bytes = int(max_mb * 1024 * 1024)
        if max_bytes > 0 and len(content) > max_bytes:
            return error(
                413,
                "file_too_large",
                f"file exceeds the {int(max_mb)} MB limit",
                request=request,
                request_id=rid,
            )

        csv_opts = None
        if csv_has_header or csv_meta_cols or csv_include_cols:
            csv_opts = {
                "has_header": csv_has_header or "auto",
                "meta_cols": csv_meta_cols or "",
                "include_cols": csv_include_cols or "",
            }

        max_i = request.app.state.max_ingests
        if max_i > 0:
            if request.app.state.active_ingests >= max_i:
                return error(
                    503,
                    "ingest_overloaded",
                    "too many concurrent ingests, try again later",
                    request=request,
                    request_id=rid,
                )
            request.app.state.active_ingests += 1
        try:
            try:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    request.app.state.ingest_executor,
                    functools.partial(
                        svc_ingest_document,
                        store,
                        tenant,
                        collection,
                        file.filename,
                        content,
                        docid,
                        meta_obj,
                        csv_options=csv_opts,
                    ),
                )
                if not result.get("ok"):
                    code = result.get("code", "ingest_failed")
                    status_map = {
                        "no_text_extracted": 400,
                        "ingest_failed": 500,
                    }
                    return error(
                        status_map.get(code, 500),
                        code,
                        result.get("error", "failed to ingest document"),
                        request=request,
                        request_id=rid,
                    )
                return trace(result, request, request_id=rid)
            except ServiceError as exc:
                code = exc.code
                status_map = {
                    "invalid_csv_options": 400,
                    "invalid_metadata_keys": 400,
                    "ingest_failed": 500,
                }
                return error(
                    status_map.get(code, 500),
                    code,
                    exc.message,
                    request=request,
                    request_id=rid,
                )
        finally:
            if max_i > 0:
                request.app.state.active_ingests -= 1

    @router.delete(
        "/collections/{tenant}/{collection}/documents/{docid}",
        response_model=DeleteDocumentResponse,
        responses=resp(401, 403, 429, 500),
        tags=["Documents"],
    )
    @ops_event(
        "delete_doc",
        coll="collection",
        docid="docid",
        request_id="rid",
    )
    def delete_document(
        request: Request,
        tenant: str,
        collection: str,
        docid: str,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(tenant_rate_limit),
        store: BaseStore = Depends(current_store),
    ):
        inc("requests_total")
        result = svc_delete_document(store, tenant, collection, docid)
        if not result.get("ok"):
            return error(
                500,
                result.get("code", "delete_document_failed"),
                result.get("error", "failed to delete document"),
                request=request,
                request_id=rid,
            )
        return trace(result, request, request_id=rid)

    @router.get(
        "/collections/{tenant}/{collection}/documents/{docid}/chunks",
        response_model=ListChunksResponse,
        responses=resp(401, 403, 429, 500),
        tags=["Chunk Inspection"],
    )
    @ops_event(
        "list_chunks",
        coll="collection",
        request_id="rid",
    )
    def list_chunks_handler(
        request: Request,
        tenant: str,
        collection: str,
        docid: str,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(tenant_rate_limit),
        store: BaseStore = Depends(current_store),
    ):
        inc("requests_total")
        result = svc_list_chunks(store, tenant, collection, docid)
        if not result.get("ok"):
            return error(
                500,
                result.get("code", "list_chunks_failed"),
                result.get("error", "failed to list chunks"),
                request=request,
                request_id=rid,
            )
        return trace(result, request, request_id=rid)

    @router.get(
        "/collections/{tenant}/{collection}/documents/{docid}",
        response_model=GetDocumentResponse,
        responses=resp(401, 403, 404, 429, 500),
        tags=["Documents"],
    )
    @ops_event(
        "get_doc",
        coll="collection",
        docid="docid",
        request_id="rid",
    )
    def get_document(
        request: Request,
        tenant: str,
        collection: str,
        docid: str,
        rid: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(tenant_rate_limit),
        store: BaseStore = Depends(current_store),
    ):
        inc("requests_total")
        result = svc_get_document(store, tenant, collection, docid)
        if not result.get("ok"):
            error_type = result.get("error_type", "failed")
            status_map = {
                "not_found": 404,
                "failed": 500,
            }
            return error(
                status_map.get(error_type, 500),
                result.get("code", "get_document_failed"),
                result.get("error", "failed to get document"),
                request=request,
                request_id=rid,
            )
        return trace(result, request, request_id=rid)

    @router.get(
        "/collections/{tenant}/{collection}/chunks/{rid}",
        response_model=GetChunkResponse,
        responses=resp(401, 403, 404, 429, 500),
        tags=["Chunk Inspection"],
    )
    @ops_event(
        "get_chunk",
        coll="collection",
        request_id="rid_header",
    )
    def get_chunk_handler(
        request: Request,
        tenant: str,
        collection: str,
        rid: str,
        rid_header: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(tenant_rate_limit),
        store: BaseStore = Depends(current_store),
    ):
        inc("requests_total")
        result = svc_get_chunk(store, tenant, collection, rid)
        if not result.get("ok"):
            status = 404 if result.get("error_type") == "not_found" else 500
            return error(
                status,
                result.get("code", "get_chunk_failed"),
                result.get("error", "failed to fetch chunk"),
                request=request,
                request_id=rid_header,
            )
        return trace(result, request, request_id=rid_header)

    @router.get(
        "/collections/{tenant}/{collection}/chunks/{rid}/content",
        response_class=Response,
        responses=resp(401, 403, 404, 429, 500),
        tags=["Chunk Inspection"],
    )
    @ops_event(
        "get_chunk_content",
        coll="collection",
        request_id="rid_header",
    )
    def get_chunk_content_handler(
        request: Request,
        tenant: str,
        collection: str,
        rid: str,
        rid_header: str | None = Depends(get_rid),
        ctx: AuthContext = Depends(tenant_rate_limit),
        store: BaseStore = Depends(current_store),
    ):
        inc("requests_total")
        result = svc_get_chunk_content(store, tenant, collection, rid)
        if not result.get("ok"):
            status = 404 if result.get("error_type") == "not_found" else 500
            return error(
                status,
                result.get("code", "get_chunk_content_failed"),
                result.get("error", "failed to fetch chunk content"),
                request=request,
                request_id=rid_header,
            )
        return Response(
            content=result["content"],
            media_type=result["content_type"],
        )

    return router
