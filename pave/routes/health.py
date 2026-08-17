# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import os
import tempfile
from contextlib import suppress

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from pave.auth import AuthContext, auth_ctx
from pave.metrics import set_error, snapshot, to_prometheus
from pave.routes._deps import current_store
from pave.stores.base import BaseStore


def build_health_router(cfg, version: str, trace) -> APIRouter:
    router = APIRouter()

    def require_admin(ctx: AuthContext = Depends(auth_ctx)) -> AuthContext:
        if not ctx.is_admin:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "admin_required",
                    "error": "admin access required",
                },
            )
        return ctx

    def readiness_check(request: Request):
        details = {
            "vector_store": cfg.get("vector_store.type"),
            "writable": False,
            "vector_backend_init": False,
        }
        try:
            os.makedirs(cfg.data_dir, exist_ok=True)
            # One probe file per request: on a fixed path, concurrent
            # unauthenticated probes delete each other's file and report a
            # perfectly writable data dir as unwritable.
            fd, testfile = tempfile.mkstemp(
                prefix=".writetest-", dir=cfg.data_dir
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write("ok")
            finally:
                with suppress(FileNotFoundError):
                    os.remove(testfile)
            details["writable"] = True
        except Exception as e:
            details["writable"] = False
            set_error(f"fs: {e}")
        try:
            # Lightweight catalog probe: confirms the store is wired and the
            # SQLite catalog is readable without walking every collection
            # (which `catalog_metrics` does, contending on per-collection locks
            # under load). Vector backend itself is exercised at startup via
            # `store.warm()`; no per-health collection write.
            request.app.state.store.catalog_probe()
            details["vector_backend_init"] = True
        except Exception as e:
            details["vector_backend_init"] = False
            set_error(f"vec: {e}")
        details["ok"] = bool(
            details["writable"] and details["vector_backend_init"]
        )
        details["version"] = version
        return details

    def store_metrics(store: BaseStore) -> dict[str, int | float]:
        try:
            metrics: dict[str, int | float] = dict(store.catalog_metrics())
            age, stale = store.catalog_metrics_freshness()
            metrics["catalog_metrics_age_s"] = (
                -1.0 if age is None else round(age, 3)
            )
            metrics["catalog_metrics_stale"] = int(stale)
            return metrics
        except Exception as e:
            set_error(f"store_metrics: {e}")
            return {
                "catalog_metrics_age_s": -1.0,
                "catalog_metrics_stale": 1,
            }

    @router.get(
        "/health",
        summary="Health check",
        description="Returns ready or degraded, plus the running version.",
        tags=["Health"],
    )
    def health(request: Request):
        d = readiness_check(request)
        status = "ready" if d.get("ok") else "degraded"
        return trace(
            {"ok": d["ok"], "status": status, "version": version},
            request,
        )

    @router.get(
        "/health/live",
        summary="Liveness check",
        description="Always succeeds while the process is running.",
        tags=["Health"],
    )
    def health_live(request: Request):
        return trace(
            {"ok": True, "status": "live", "version": version},
            request,
        )

    @router.get(
        "/health/ready",
        summary="Readiness check",
        description=(
            "Returns 503 until storage and embeddings are usable. "
            "Does not expose local filesystem paths."
        ),
        tags=["Health"],
    )
    def health_ready(request: Request):
        d = readiness_check(request)
        code = 200 if d.get("ok") else 503
        return JSONResponse(trace(d, request), status_code=code)

    @router.get(
        "/health/metrics",
        summary="Metrics snapshot",
        description=(
            "Counters, version, and hardware info as JSON. Requires the "
            "admin key when auth.mode=static."
        ),
        tags=["Health"],
    )
    def health_metrics(
        request: Request,
        _ctx: AuthContext = Depends(require_admin),
        store: BaseStore = Depends(current_store),
    ):
        extra = {
            "version": version,
            "vector_store": cfg.get("vector_store.type"),
            "auth": cfg.get("auth.mode"),
            **request.app.state.hw_info,
        }
        extra.update(store_metrics(store))
        return trace(snapshot(extra, store=store), request)

    @router.get(
        "/metrics",
        summary="Prometheus metrics",
        description=(
            "The same counters in Prometheus exposition format. Requires the "
            "admin key when auth.mode=static."
        ),
        tags=["Health"],
    )
    def metrics_prom(
        _ctx: AuthContext = Depends(require_admin),
        store: BaseStore = Depends(current_store),
    ):
        extra = store_metrics(store)
        txt = to_prometheus(
            build={
                "version": version,
                "vector_store": cfg.get("vector_store.type"),
                "auth": cfg.get("auth.mode"),
            },
            extra=extra,
            store=store,
        )
        return PlainTextResponse(txt, media_type="text/plain; version=0.0.4")

    return router
