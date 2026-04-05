# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request

from pave.auth import AuthContext, tenant_rate_limit
from pave.log import ops_event
from pave.metrics import inc
from pave.schemas import (
    CreateCollectionBody,
    CreateCollectionResponse,
    DeleteCollectionResponse,
    ListCollectionsResponse,
    RenameCollectionBody,
    RenameCollectionResponse,
)
from pave.service import (
    create_collection as svc_create_collection,
    delete_collection as svc_delete_collection,
    list_collections as svc_list_collections,
    rename_collection as svc_rename_collection,
)
from pave.stores.base import BaseStore


def build_collections_router(error, resp) -> APIRouter:
    router = APIRouter()

    def current_store(request: Request) -> BaseStore:
        return request.app.state.store

    @router.get(
        "/collections/{tenant}",
        response_model=ListCollectionsResponse,
        responses=resp(401, 403, 429, 500),
    )
    @ops_event("list_collections", coll=None)
    def list_collections(
        tenant: str,
        ctx: AuthContext = Depends(tenant_rate_limit),
        store: BaseStore = Depends(current_store),
    ):
        inc("requests_total")
        result = svc_list_collections(store, tenant)
        if not result.get("ok"):
            return error(
                500,
                result.get("code", "list_collections_failed"),
                result.get("error", "failed to list collections"),
            )
        return result

    @router.post(
        "/collections/{tenant}/{name}",
        status_code=201,
        response_model=CreateCollectionResponse,
        responses=resp(400, 401, 403, 429, 500),
    )
    @ops_event("create_collection")
    def create_collection(
        tenant: str,
        name: str,
        body: CreateCollectionBody | None = Body(None),
        ctx: AuthContext = Depends(tenant_rate_limit),
        store: BaseStore = Depends(current_store),
    ):
        inc("requests_total")
        result = svc_create_collection(
            store,
            tenant,
            name,
            embedder_type=body.embedder_type if body else None,
            embed_model=body.embed_model if body else None,
        )
        if not result.get("ok"):
            error_type = result.get("error_type", "failed")
            status_map = {
                "invalid": 400,
                "failed": 500,
            }
            return error(
                status_map.get(error_type, 500),
                result.get("code", "create_collection_failed"),
                result.get("error", "failed to create collection"),
            )
        return result

    @router.delete(
        "/collections/{tenant}/{name}",
        response_model=DeleteCollectionResponse,
        responses=resp(401, 403, 429, 500),
    )
    @ops_event("delete_collection")
    def delete_collection(
        tenant: str,
        name: str,
        ctx: AuthContext = Depends(tenant_rate_limit),
        store: BaseStore = Depends(current_store),
    ):
        inc("requests_total")
        result = svc_delete_collection(store, tenant, name)
        if not result.get("ok"):
            return error(
                500,
                result.get("code", "delete_collection_failed"),
                result.get("error", "failed to delete collection"),
            )
        return result

    @router.put(
        "/collections/{tenant}/{name}",
        response_model=RenameCollectionResponse,
        responses=resp(400, 401, 403, 404, 409, 429, 500),
    )
    @ops_event(
        "rename_collection",
        new_name=lambda kw, r: kw["body"].new_name,
    )
    def rename_collection(
        tenant: str,
        name: str,
        body: RenameCollectionBody,
        ctx: AuthContext = Depends(tenant_rate_limit),
        store: BaseStore = Depends(current_store),
    ):
        inc("requests_total")
        result = svc_rename_collection(store, tenant, name, body.new_name)
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
                result.get("code", "rename_invalid"),
                result.get("error", "failed to rename collection"),
            )
        return result

    return router
