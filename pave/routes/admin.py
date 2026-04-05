# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import os
import shutil

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from pave.auth import AuthContext, auth_ctx
from pave.metrics import inc, reset as metrics_reset
from pave.schemas import (
    ListTenantsResponse,
    ResetMetricsResponse,
    RestoreArchiveResponse,
)
from pave.service import (
    dump_archive as svc_dump_archive,
    list_tenants as svc_list_tenants,
    restore_archive as svc_restore_archive,
)
from pave.stores.base import BaseStore


def build_admin_router(cfg, error, resp) -> APIRouter:
    router = APIRouter()

    def current_store(request: Request) -> BaseStore:
        return request.app.state.store

    @router.get(
        "/admin/archive",
        response_class=FileResponse,
        responses=resp(401, 403, 404, 500),
    )
    async def dump_archive(
        ctx: AuthContext = Depends(auth_ctx),
        store: BaseStore = Depends(current_store),
    ):
        inc("requests_total")
        if not ctx.is_admin:
            return error(403, "admin_required", "admin access required")

        try:
            archive_path, tmp_dir = await run_in_threadpool(
                svc_dump_archive, store
            )
        except FileNotFoundError:
            return error(404, "data_dir_not_found", "data directory not found")
        except Exception as exc:
            return error(
                500,
                "archive_dump_failed",
                f"failed to dump archive: {exc}",
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
    )
    async def restore_archive(
        ctx: AuthContext = Depends(auth_ctx),
        store: BaseStore = Depends(current_store),
        file: UploadFile = File(...),
    ):
        inc("requests_total")
        if not ctx.is_admin:
            return error(403, "admin_required", "admin access required")

        content = await file.read()
        try:
            out = await run_in_threadpool(
                svc_restore_archive, store, content
            )
            return out
        except ValueError as exc:
            return error(400, "archive_invalid", str(exc))
        except Exception as exc:
            return error(
                500,
                "archive_restore_failed",
                f"failed to restore archive: {exc}",
            )

    @router.delete(
        "/admin/metrics",
        response_model=ResetMetricsResponse,
        responses=resp(401, 403),
    )
    def delete_metrics(
        ctx: AuthContext = Depends(auth_ctx),
    ):
        inc("requests_total")
        if not ctx.is_admin:
            return error(403, "admin_required", "admin access required")
        return metrics_reset()

    @router.get(
        "/admin/tenants",
        response_model=ListTenantsResponse,
        responses=resp(401, 403, 500),
    )
    def list_tenants(
        ctx: AuthContext = Depends(auth_ctx),
        store: BaseStore = Depends(current_store),
    ):
        inc("requests_total")
        if not ctx.is_admin:
            return error(403, "admin_required", "admin access required")
        result = svc_list_tenants(store)
        if not result.get("ok"):
            return error(
                500,
                result.get("code", "list_tenants_failed"),
                result.get("error", "failed to list tenants"),
            )
        return result

    return router
