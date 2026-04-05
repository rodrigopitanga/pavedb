# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from pave.metrics import inc, set_error, snapshot, to_prometheus
from pave.stores.base import BaseStore


def build_health_router(cfg, version: str, trace) -> APIRouter:
    router = APIRouter()

    def current_store(request: Request) -> BaseStore:
        return request.app.state.store

    def readiness_check(request: Request) -> dict[str, Any]:
        details: dict[str, Any] = {
            "data_dir": cfg.get("data_dir"),
            "vector_store": cfg.get("vector_store.type"),
            "writable": False,
            "vector_backend_init": False,
        }
        try:
            os.makedirs(cfg.data_dir, exist_ok=True)
            testfile = os.path.join(cfg.data_dir, ".writetest")
            with open(testfile, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(testfile)
            details["writable"] = True
        except Exception as e:
            details["writable"] = False
            set_error(f"fs: {e}")
        try:
            request_store = request.app.state.store
            request_store.create_collection("_system", "health")
            details["vector_backend_init"] = True
        except Exception as e:
            details["vector_backend_init"] = False
            set_error(f"vec: {e}")
        details["ok"] = bool(
            details["writable"] and details["vector_backend_init"]
        )
        details["version"] = version
        return details

    def store_metrics(store: BaseStore) -> dict[str, int]:
        try:
            return store.catalog_metrics()
        except Exception as e:
            set_error(f"store_metrics: {e}")
            return {}

    @router.get("/health")
    def health(request: Request):
        inc("requests_total")
        d = readiness_check(request)
        status = "ready" if d.get("ok") else "degraded"
        return trace(
            {"ok": d["ok"], "status": status, "version": version},
            request,
        )

    @router.get("/health/live")
    def health_live(request: Request):
        inc("requests_total")
        return trace(
            {"ok": True, "status": "live", "version": version},
            request,
        )

    @router.get("/health/ready")
    def health_ready(request: Request):
        inc("requests_total")
        d = readiness_check(request)
        code = 200 if d.get("ok") else 503
        return JSONResponse(trace(d, request), status_code=code)

    @router.get("/health/metrics")
    def health_metrics(
        request: Request,
        store: BaseStore = Depends(current_store),
    ):
        inc("requests_total")
        extra = {
            "version": version,
            "vector_store": cfg.get("vector_store.type"),
            "auth": cfg.get("auth.mode"),
            **request.app.state.hw_info,
        }
        extra.update(store_metrics(store))
        return trace(snapshot(extra), request)

    @router.get("/metrics")
    def metrics_prom(store: BaseStore = Depends(current_store)):
        inc("requests_total")
        extra = store_metrics(store)
        txt = to_prometheus(
            build={
                "version": version,
                "vector_store": cfg.get("vector_store.type"),
                "auth": cfg.get("auth.mode"),
            },
            extra=extra,
        )
        return PlainTextResponse(txt, media_type="text/plain; version=0.0.4")

    return router
