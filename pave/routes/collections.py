# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request

from pave.auth import AuthContext, tenant_rate_limit
from pave.log import ops_event
from pave.schemas import (
    CollectionDetailResponse,
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
    create_collection as svc_create_collection,
    delete_collection as svc_delete_collection,
    get_collection_detail as svc_get_collection_detail,
    list_embedders as svc_list_embedders,
    list_collections as svc_list_collections,
    move_collection as svc_move_collection,
    update_collection as svc_update_collection,
)
from pave.routes._deps import current_store
from pave.stores.base import BaseStore


def build_collections_router(error, resp, get_rid, trace) -> APIRouter:
    router = APIRouter()

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

    return router
