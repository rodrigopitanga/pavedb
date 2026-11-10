# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import argparse
import asyncio
import copy
import logging
import os
import platform
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager, suppress

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import JSONResponse
from uvicorn.config import Config
from uvicorn.main import STARTUP_FAILURE
from uvicorn.server import HANDLED_SIGNALS, Server
from uvicorn.supervisors import ChangeReload, Multiprocess

from pave.auth import (
    ConcurrencySlot,
    auth_ctx,
    bearer,
    bootstrap_admin_key,
    enforce_policy,
    resolve_bind,
)
from pave.config import (
    cfg_bool,
    get_cfg,
    get_logger,
    reload_cfg,
    should_enable_ui,
    should_warm_default_embedder,
)
from pave.data_dir_lease import DataDirInUseError, DataDirLease
from pave.embedders import LazyEmbedder, get_embedder
from pave.metadb import SchemaMigrationError, UnsupportedSchemaVersionError
from pave.metrics import (
    flush as metrics_flush,
    inc as metrics_inc,
    initialize as metrics_initialize,
    record_http_request,
    refresh_product_gauges as metrics_refresh_product_gauges,
    set_error as metrics_set_error,
)
from pave.routes import (
    build_admin_router,
    build_collections_router,
    build_documents_router,
    build_health_router,
    build_search_router,
)
from pave.runtime_paths import (
    DEFAULT_HOME,
    HOME_ENV,
    apply_runtime_env,
    resolve_runtime_paths,
)
from pave.schemas import ErrorResponse
from pave.service import ServiceError, ingest_upload_limit_bytes
from pave.stores.local import LocalStore
from pave.ui import attach_ui
from pave.version import (
    COPYRIGHT,
    VERSION,
    help_notice,
    product_display_name,
    software_title,
    version_banner,
)
import pave.log as ops_log

_KNOWN_HTTP_METHODS = frozenset(
    ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE")
)

METRICS_FLUSH_INTERVAL_SECONDS = 10.5
CATALOG_METRICS_REFRESH_INTERVAL_SECONDS = 10.0
GRACEFUL_SHUTDOWN_MESSAGE = (
    "Finishing current work, shutting down soon. Press Ctrl+C again to "
    "shut down immediately (potential data loss)."
)
FORCE_SHUTDOWN_MESSAGE = (
    "Immediate shutdown requested; exiting now (potential data loss)."
)
# Boundary lines, part headers, and the small form fields (docid, metadata)
# that ride along with an uploaded file. Added to the ingest limit so a file
# at exactly the configured size still fits inside its multipart envelope.
MULTIPART_FRAMING_BYTES = 1024 * 1024
_ASGI_CONTEXT_ENV = "_PAVEDB_ASGI_CONTEXT"


class _BodyTooLarge(Exception):
    """Raised from the receive chain when a streamed body passes the cap."""


_LOOPBACK_HOST = re.compile(r"(?:localhost|127\.0\.0\.1)(?::([0-9]{1,5}))?", re.I)


class DevHostOriginMiddleware:
    """Keep unauthenticated development requests on the loopback origin."""

    def __init__(self, app, cfg) -> None:
        self.app = app
        self.cfg = cfg

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or str(self.cfg.get("auth.mode")).strip().lower() != "none"
        ):
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers") or ()
        hosts = [v.decode("latin-1") for k, v in headers if k.lower() == b"host"]
        origins = [v.decode("latin-1") for k, v in headers if k.lower() == b"origin"]
        valid = len(hosts) == 1 and _LOOPBACK_HOST.fullmatch(hosts[0])
        if valid:
            host_port = hosts[0].rsplit(":", 1)
            port = int(host_port[1]) if len(host_port) == 2 else None
            valid = port is None or 0 < port <= 65535
        if valid and origins:
            valid = (
                len(origins) == 1
                and origins[0].lower()
                == f"{scope.get('scheme', 'http')}://{hosts[0]}".lower()
            )
        if not valid:
            response = JSONResponse(
                {
                    "ok": False,
                    "code": "invalid_origin",
                    "error": "invalid Host or Origin",
                },
                status_code=403,
            )
            await response(scope, _closed_receive, send)
            return
        await self.app(scope, receive, send)


async def _closed_receive() -> dict:
    """Receive channel for a response sent without draining the body."""
    return {"type": "http.disconnect"}


class BodySizeLimitMiddleware:
    """Reject oversized request bodies before routing and authentication.

    FastAPI reads and validates the whole body before it resolves route
    dependencies, so `tenant_rate_limit` / `auth_ctx` cannot stop an
    unauthenticated caller from making the process buffer and parse an
    arbitrary payload. The guard therefore has to sit ahead of routing, which
    rules out a dependency and any route-level check.

    Multipart document uploads and tenant collection-archive restores spool
    to a temporary file, so they get the ingest ceiling. Admin archive
    restore has its own authenticated, exempt path; every other body gets
    the tighter `server.max_request_body_mb` ceiling.
    """

    def __init__(self, app) -> None:
        self.app = app

    @staticmethod
    def _accepts_upload(path: str) -> bool:
        # Keyed on the path, never on Content-Type: the header is the
        # caller's, so granting the larger ceiling from it let any request
        # claim multipart and buy the ingest limit on a JSON endpoint, where
        # the body is parsed into memory rather than spooled to disk.
        if path.endswith("/documents"):
            return True
        # POST /v1/collections/{tenant}/{name}/archive: a tenant restore is
        # unauthenticated at this layer, so it is bounded, but by the upload
        # ceiling rather than the in-memory JSON one.
        return (
            path.startswith("/v1/collections/")
            and path.endswith("/archive")
            and len(path.split("/")) == 6
        )

    @staticmethod
    def _restores_archive(scope) -> bool:
        method = scope.get("method")
        path = str(scope.get("path") or "")
        if method == "PUT" and path == "/v1/admin/archive":
            return True
        return (
            method == "POST"
            and path.startswith("/v1/admin/collections/")
            and path.endswith("/archive")
            and len(path.split("/")) == 7
        )

    @classmethod
    def _limit_for(cls, scope) -> int:
        state = getattr(scope.get("app"), "state", None)
        if state is None:
            return -1
        path = str(scope.get("path") or "")
        # Archives are unrelated to the largest accepted document. Starlette
        # spools these multipart uploads to disk; do not reject a valid backup
        # under either in-memory request ceiling.
        if cls._restores_archive(scope):
            return -1
        if cls._accepts_upload(path):
            return getattr(state, "max_upload_body_bytes", -1)
        return getattr(state, "max_body_bytes", -1)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self._restores_archive(scope):
            if not await self._authorize_archive(scope, send):
                return
        limit = -1 if scope["type"] != "http" else self._limit_for(scope)
        if limit == -1:
            await self.app(scope, receive, send)
            return

        declared = None
        for name, value in scope.get("headers") or ():
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = None
                break
        if declared is not None and declared > limit:
            await self._reject(scope, send, declared, limit)
            return

        received = 0
        oversize = False
        started = False
        answered = False

        async def guarded_receive():
            nonlocal received, oversize
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                # Chunked requests carry no Content-Length. Rejecting them all
                # would break legitimate streaming clients, so the body is
                # counted as it arrives and cut off at the same ceiling.
                if received > limit:
                    oversize = True
                    raise _BodyTooLarge()
            return message

        async def guarded_send(message):
            nonlocal started, answered
            if answered:
                return
            if oversize and not started:
                # FastAPI turns anything raised while it reads the body into
                # its own "error parsing the body" 400; replace that with the
                # documented 413 envelope.
                answered = True
                await self._reject(scope, send, received, limit)
                return
            if message.get("type") == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, guarded_receive, guarded_send)
        except _BodyTooLarge:
            pass
        if oversize and not started and not answered:
            await self._reject(scope, send, received, limit)

    @staticmethod
    async def _authorize_archive(scope, send) -> bool:
        credentials = await bearer(Request(scope))
        try:
            ctx = auth_ctx(credentials)
            if not ctx.is_admin:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "admin_required",
                        "error": "admin access required",
                    },
                )
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            body = {
                "ok": False,
                "code": detail.get("code", "http_error"),
                "error": detail.get("error") or str(exc.detail),
            }
            request_id = (scope.get("state") or {}).get("request_id")
            if request_id is not None:
                body["request_id"] = request_id
            response = JSONResponse(
                body,
                status_code=exc.status_code,
                headers=exc.headers,
            )
            await response(scope, _closed_receive, send)
            return False
        return True

    @staticmethod
    async def _reject(scope, send, size: int, limit: int) -> None:
        request_id = (scope.get("state") or {}).get("request_id")
        get_logger().warning(
            "Request body rejected before routing: it exceeds the "
            f"{limit} byte limit (at least {size} bytes) "
            f"path={scope.get('path')} request_id={request_id}"
        )
        body = {
            "ok": False,
            "code": "request_too_large",
            "error": (
                "request body exceeds the "
                f"{max(int(limit / (1024 * 1024)), 1)} MB limit"
            ),
        }
        if request_id is not None:
            body["request_id"] = request_id
        response = JSONResponse(body, status_code=413)
        await response(scope, _closed_receive, send)


def _hw_info() -> dict:
    """Collect server hardware info once at startup (stdlib-only, multiplatform)."""
    info: dict = {
        "hw_cpu": platform.processor() or platform.machine(),
        "hw_cores": os.cpu_count(),
        "hw_os": f"{platform.system()} {platform.release()}",
    }
    try:
        if sys.platform == "linux":
            with open("/proc/meminfo", encoding="ascii") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        info["hw_ram_gb"] = round(
                            int(line.split()[1]) / 1_000_000,
                            1,
                        )
                        break
        elif sys.platform == "darwin":
            out = subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"], text=True, timeout=2
            )
            info["hw_ram_gb"] = round(int(out.strip()) / 1_000_000_000, 1)
    except Exception:
        pass
    return info


async def _flush_metrics_periodically(store) -> None:
    while True:
        await asyncio.sleep(METRICS_FLUSH_INTERVAL_SECONDS)
        metrics_flush(store)


async def _refresh_catalog_metrics_periodically(store) -> None:
    loop = asyncio.get_running_loop()
    while True:
        try:
            await loop.run_in_executor(None, store.refresh_catalog_metrics)
            await loop.run_in_executor(None, metrics_refresh_product_gauges, store)
        except Exception as exc:
            metrics_set_error(f"catalog_metrics_refresh: {exc}")
        await asyncio.sleep(CATALOG_METRICS_REFRESH_INTERVAL_SECONDS)


class PaveServer(Server):
    @contextmanager
    def capture_signals(self):
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        original_handlers = {
            sig: signal.signal(sig, self.handle_exit)
            for sig in HANDLED_SIGNALS
        }
        try:
            yield
        finally:
            for sig, handler in original_handlers.items():
                signal.signal(sig, handler)

    def handle_exit(self, sig: int, frame) -> None:
        self._captured_signals.append(sig)
        if self.should_exit:
            # SIGTERM during an existing graceful shutdown is the
            # reloader/multiproc parent winding us up after we already
            # accepted the user's SIGINT; only escalate on a real second
            # user-issued SIGINT.
            if sig == signal.SIGINT:
                get_logger().warning(FORCE_SHUTDOWN_MESSAGE)
                self.force_exit = True
            return
        get_logger().info(GRACEFUL_SHUTDOWN_MESSAGE)
        self.should_exit = True


class PaveMultiprocess(Multiprocess):
    def _request_shutdown(self) -> None:
        if self.should_exit.is_set():
            get_logger().warning(FORCE_SHUTDOWN_MESSAGE)
            for process in self.processes:
                process.kill()
            return
        # Each worker prints GRACEFUL on its own terminal SIGINT; parent
        # stays quiet so a single Ctrl+C produces a single message.
        self.should_exit.set()

    def handle_int(self) -> None:
        self._request_shutdown()

    def handle_term(self) -> None:
        self._request_shutdown()

    def handle_break(self) -> None:
        self._request_shutdown()


class PaveReload(ChangeReload):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._pave_shutdown_requested = False

    def signal_handler(self, sig: int, frame) -> None:
        if self._pave_shutdown_requested:
            get_logger().warning(FORCE_SHUTDOWN_MESSAGE)
            self.process.kill()
            self.should_exit.set()
            return
        self._pave_shutdown_requested = True
        # The reload child prints GRACEFUL when it receives the terminal
        # SIGINT; parent stays quiet to avoid duplicating the message.
        if sys.platform == "win32" and self.is_restarting:
            self.is_restarting = False
        else:
            self.should_exit.set()


def _run_pave_server(app: str, **kwargs) -> None:
    config = Config(app, **kwargs)
    config.load_app()
    pirex = PaveServer(config=config)
    try:
        if config.should_reload:
            sock = config.bind_socket()
            PaveReload(config, target=pirex.run, sockets=[sock]).run()
        elif (config.workers or 1) > 1:
            sock = config.bind_socket()
            PaveMultiprocess(config, target=pirex.run, sockets=[sock]).run()
        else:
            pirex.run()
    except KeyboardInterrupt:
        pass
    finally:
        if config.uds and os.path.exists(config.uds):
            os.remove(config.uds)
    if not pirex.started and not config.should_reload and (config.workers or 1) == 1:
        sys.exit(STARTUP_FAILURE)


_LOC_PART_MAX = 64


def _client_validation_errors(errors) -> list[dict]:
    """Validation errors without the rejected value.

    Echoing the input reflects whatever the caller sent back at full size,
    up to the body limit; the location and message say what was wrong.
    Location parts are caller-chosen too (an unknown key), so long ones are
    cut.
    """
    cleaned = []
    for error in errors:
        error = {key: value for key, value in error.items() if key != "input"}
        error["loc"] = [
            part[:_LOC_PART_MAX] + "..."
            if isinstance(part, str) and len(part) > _LOC_PART_MAX
            else part
            for part in error.get("loc", ())
        ]
        cleaned.append(error)
    return cleaned


def build_app(cfg) -> FastAPI:
    bootstrap_admin_key(cfg, resolve_runtime_paths().home)
    # Workers launched by pavesrv and callers using build_app(cfg) both enter
    # here. Keep policy enforcement at the construction boundary even though
    # the lazy module-level app now rejects raw ASGI startup first. Ordered
    # after bootstrap: that sets the key file the static-mode check sees.
    enforce_policy(cfg)
    log = get_logger()

    def _resp(*codes: int) -> dict[int, dict[str, object]]:
        # Python 3.13 renamed the 413 phrase ("Request Entity Too Large" ->
        # "Content Too Large"); pin it so openapi.json is interpreter-stable.
        return {
            code: (
                {"model": ErrorResponse, "description": "Content Too Large"}
                if code == 413
                else {"model": ErrorResponse}
            )
            for code in codes
        }

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        metrics_initialize(app.state.store)
        metrics_flush_task = asyncio.create_task(
            _flush_metrics_periodically(app.state.store)
        )
        catalog_metrics_task = asyncio.create_task(
            _refresh_catalog_metrics_periodically(app.state.store)
        )
        if should_warm_default_embedder(cfg):
            started = time.perf_counter()
            try:
                app.state.store.warm()
                elapsed_ms = (time.perf_counter() - started) * 1000
                log.info(f"Default store warm-up complete in {elapsed_ms:.1f} ms")
            except Exception as exc:
                elapsed_ms = (time.perf_counter() - started) * 1000
                log.warning(
                    f"Default store warm-up failed after {elapsed_ms:.1f} ms: "
                    f"{exc}"
                )
        try:
            yield
        finally:
            metrics_flush_task.cancel()
            catalog_metrics_task.cancel()
            with suppress(asyncio.CancelledError):
                await metrics_flush_task
            with suppress(asyncio.CancelledError):
                await catalog_metrics_task
            # Drain in-flight search/ingest work before flushing metrics and
            # closing the ops log, so their accounting is not lost.
            for _exec in (app.state.search_executor, app.state.ingest_executor):
                if _exec is not None:
                    _exec.shutdown(wait=True)
            metrics_flush(app.state.store)
            app.state.store.close()
            ops_log.close()

    # UI switch also gates FastAPI's built-in /docs, /redoc, /openapi.json so
    # "off" leaves no interactive OpenAPI explorer, not just no /ui bundle.
    _ui_on = should_enable_ui(cfg)
    app = FastAPI(
        title=product_display_name(cfg.get("instance.name")),
        description=cfg.get("instance.desc"),
        version=VERSION,
        lifespan=lifespan,
        docs_url="/docs" if _ui_on else None,
        redoc_url="/redoc" if _ui_on else None,
        openapi_url="/openapi.json" if _ui_on else None,
    )
    app.state.ui_enabled = _ui_on

    # Registered first, so it is the innermost user middleware: the request id
    # is already on the scope when a body is rejected, and the rejection is
    # still counted by the metrics middleware.
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(DevHostOriginMiddleware, cfg=cfg)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        rid = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = rid
        request.state.started_at = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = _get_request_id(request) or rid
        return response

    app.state.store = LocalStore(
        data_dir=str(cfg.get("data_dir")),
        embedder=LazyEmbedder(get_embedder),
    )
    app.state.tenant_rate_limiter = app.state.store.charge_rate_limit
    app.state.tenant_rate_clock = time.time
    # The shared corpus used to be conjured by the first include_common
    # search. Read paths no longer create collections (B33), so the operator's
    # configured corpus is brought into existence here instead.
    # create_collection is idempotent, so concurrent workers and restarts
    # converge on the same row instead of failing or duplicating.
    if cfg.get("common_enabled"):
        _ct = str(cfg.get("common_tenant"))
        _cc = str(cfg.get("common_collection"))
        try:
            # Only pay for the create when the corpus is genuinely missing:
            # create_collection sizes the index from the embedder, so calling
            # it on every start would load a model that
            # embedder.warmup_default=false asked us not to load. Listing the
            # catalog costs one query and no embedder.
            if not any(
                c["name"] == _cc
                for c in app.state.store.list_collections(_ct)
            ):
                app.state.store.create_collection(_ct, _cc)
        except Exception as exc:
            # Never block startup on it: shared search degrades to 404, every
            # other route keeps working.
            log.warning(f"Common collection bootstrap failed: {exc}")
    app.state.cfg = cfg
    app.state.version = VERSION
    app.state.hw_info = _hw_info()

    # Request body limits, read once here like every other instance-scoped
    # cap. Uploads are allowed the configured ingest ceiling; an unlimited
    # ingest limit (-1) leaves them unbounded, as documented.
    _body_mb = cfg.get("server.max_request_body_mb")
    app.state.max_body_bytes = (
        -1 if _body_mb == -1 else int(_body_mb * 1024 * 1024)
    )
    _ingest_bytes = ingest_upload_limit_bytes()
    app.state.max_upload_body_bytes = (
        _ingest_bytes + MULTIPART_FRAMING_BYTES if _ingest_bytes >= 0 else -1
    )

    # Search limits
    _max_conc = int(cfg.get("search.max_concurrent"))
    _to_ms = int(cfg.get("search.timeout_ms"))
    # Dedicated executor: threads == max_concurrent so work starts immediately.
    app.state.search_executor = (
        ThreadPoolExecutor(max_workers=_max_conc) if _max_conc > 0 else None
    )
    # Background workers release slots after timeouts or cancellation.
    app.state.max_searches = _max_conc
    app.state.active_searches = 0
    # Per-tenant share of the shared search pool, off by default for the same
    # reason as the ingest share: on a single-tenant instance a share is pure
    # throughput loss. Turn it on for a shared instance, where one tenant can
    # otherwise hold every slot -- tenants.default_max_concurrent (42) is not
    # smaller than this pool (42), so that cap only binds once the tenant has
    # already taken all of it.
    _raw_sper = cfg.get("search.max_concurrent_per_tenant")
    app.state.max_searches_per_tenant = int(_raw_sper)
    app.state.tenant_active_searches = {}
    app.state.search_lock = threading.Lock()
    app.state.search_timeout_s = _to_ms / 1000.0 if _to_ms > 0 else 0.0

    _max_iconc = int(cfg.get("ingest.max_concurrent"))
    app.state.ingest_executor = (
        ThreadPoolExecutor(max_workers=_max_iconc) if _max_iconc > 0 else None
    )
    app.state.max_ingests = _max_iconc
    app.state.active_ingests = 0
    # Per-tenant share of the shared ingest pool, off by default: on a
    # single-tenant instance a share is pure throughput loss. Turn it on for a
    # shared instance, where one tenant can otherwise hold every slot while
    # staying inside its own request cap -- tenants.default_max_concurrent (42)
    # is larger than this pool (7), so that cap cannot bind on ingest.
    _raw_iper = cfg.get("ingest.max_concurrent_per_tenant")
    app.state.max_ingests_per_tenant = int(_raw_iper)
    app.state.tenant_active_ingests = {}
    # Ingest slots are now released from pool threads (detached work), so the
    # read-modify-write genuinely races without this.
    app.state.ingest_lock = threading.Lock()

    # Per-tenant concurrency and moving-window request-rate limits.
    _tenants_cfg = cfg.get("tenants") or {}
    _raw_def = (
        _tenants_cfg.get("default_max_concurrent")
        if isinstance(_tenants_cfg, dict)
        else None
    )
    app.state.tenant_default_limit = int(_raw_def) if _raw_def is not None else -1
    app.state.tenant_limits = {}
    app.state.tenant_active = {}
    app.state.tenant_lock = threading.Lock()
    app.state.tenant_default_rate_limits = {
        60: int(
            _tenants_cfg.get("default_max_rpm", -1)
            if isinstance(_tenants_cfg, dict)
            else -1
        ),
        3600: int(
            _tenants_cfg.get("default_max_rph", -1)
            if isinstance(_tenants_cfg, dict)
            else -1
        ),
    }
    app.state.tenant_rate_limits = {}
    for _t, _tcfg in (_tenants_cfg.items() if isinstance(_tenants_cfg, dict) else []):
        if not isinstance(_tcfg, dict):
            continue
        _lim = _tcfg.get("max_concurrent")
        if _lim is not None:
            app.state.tenant_limits[_t] = int(_lim)
            app.state.tenant_active[_t] = 0
        _rpm = _tcfg.get("max_rpm")
        _rph = _tcfg.get("max_rph")
        app.state.tenant_rate_limits[_t] = {
            60: (
                int(_rpm)
                if _rpm is not None
                else app.state.tenant_default_rate_limits[60]
            ),
            3600: (
                int(_rph)
                if _rph is not None
                else app.state.tenant_default_rate_limits[3600]
            ),
        }

    ops_log.configure(cfg.get("log.ops_log"))

    def _get_request_id(request: Request) -> str | None:
        return getattr(request.state, "request_id", None)

    def _get_latency_ms(request: Request) -> float | None:
        started = getattr(request.state, "started_at", None)
        if started is None:
            return None
        return round((time.perf_counter() - started) * 1000, 2)

    def _apply_trace_fields(
        body: dict,
        *,
        request_id: str | None = None,
        latency_ms: float | None = None,
    ) -> dict:
        if request_id is not None:
            body["request_id"] = request_id
        if latency_ms is not None:
            body["latency_ms"] = latency_ms
        return body

    def _trace_body(
        body: dict,
        request: Request,
        *,
        request_id: str | None = None,
    ) -> dict:
        rid = request_id if request_id is not None else _get_request_id(request)
        latency_ms = _get_latency_ms(request)
        return _apply_trace_fields(
            body,
            request_id=rid,
            latency_ms=latency_ms,
        )

    def _error(
        status_code: int,
        code: str,
        message: str,
        *,
        request: Request | None = None,
        request_id: str | None = None,
        latency_ms: float | None = None,
    ) -> JSONResponse:
        if request is not None:
            if request_id is None:
                request_id = _get_request_id(request)
            if latency_ms is None:
                latency_ms = _get_latency_ms(request)
        body = _apply_trace_fields(
            {"ok": False, "code": code, "error": message},
            request_id=request_id,
            latency_ms=latency_ms,
        )
        return JSONResponse(body, status_code=status_code)

    def _search_gate(request, tenant, request_id):
        """Take one instance and one per-tenant search slot.

        Returns ``(slot, None)`` when admitted, ``(None, response)`` with the
        503 when either bound is reached. The per-tenant share is what stops
        one tenant from holding every slot in the shared pool; the general
        per-tenant request cap cannot, because it is not smaller than the
        pool. Both counters move together under the one lock, because a
        detached search thread releases them off the event loop.
        """
        state = app.state
        max_s = state.max_searches
        # No charged tenant (admin, or no per-tenant cap in force) means no
        # share to apply, exactly as with the request cap it follows.
        max_t = state.max_searches_per_tenant if tenant else -1
        per = state.tenant_active_searches
        source = None
        with state.search_lock:
            active_s = state.active_searches
            active_t = per.get(tenant, 0)
            if max_s >= 0 and active_s >= max_s:
                source, active, limit = (
                    "instance-scoped search cap", active_s, max_s
                )
            elif max_t >= 0 and active_t >= max_t:
                source, active, limit = (
                    "tenant-scoped search share", active_t, max_t
                )
            else:
                if max_s >= 0:
                    state.active_searches = active_s + 1
                if max_t >= 0:
                    per[tenant] = active_t + 1

        if source is None:
            def _decrement() -> None:
                if max_s >= 0:
                    state.active_searches -= 1
                if max_t >= 0:
                    per[tenant] = max(0, per.get(tenant, 0) - 1)

            return ConcurrencySlot(state.search_lock, _decrement), None

        log.warning(
            f"search capped by {source} tenant={tenant} "
            f"active={active} limit={limit} path={request.url.path} "
            f"request_id={request_id}"
        )
        return None, _error(
            503,
            "search_overloaded",
            f"search capped by {source} "
            f"(active={active}, limit={limit})",
            request=request,
            request_id=request_id,
        )

    async def _do_search(fn, *, request: Request, request_id: str | None = None):
        """Concurrency gate + timeout wrapper for all search handlers."""
        timeout_s = app.state.search_timeout_s
        # The tenant slot the request is holding, if any. A search that
        # outlives its request keeps occupying the shared pool, so the
        # tenant has to keep paying for it until the thread returns.
        tenant_slot = getattr(request.state, "tenant_slot", None)
        slot, capped = _search_gate(
            request,
            getattr(request.state, "capped_tenant", None),
            request_id,
        )
        if capped is not None:
            return capped
        try:
            loop = asyncio.get_running_loop()
            executor = app.state.search_executor
            if executor is None:
                future = loop.run_in_executor(None, fn)
                completion_future = future
            else:
                completion_future = executor.submit(fn)
                future = asyncio.wrap_future(completion_future)
            try:
                if timeout_s > 0:
                    result = await asyncio.wait_for(
                        asyncio.shield(future), timeout=timeout_s
                    )
                else:
                    result = await future
                if isinstance(result, dict) and result.get("ok"):
                    return _trace_body(
                        result,
                        request,
                        request_id=request_id,
                    )
                if isinstance(result, dict) and result.get("ok") is False:
                    status_code = (
                        404 if result.get("error_type") == "not_found" else 500
                    )
                    return _error(
                        status_code,
                        result.get("code", "search_failed"),
                        result.get("error", "search failed"),
                        request=request,
                        request_id=request_id,
                    )
                return result
            except asyncio.TimeoutError:
                # Thread keeps running; suppress its eventual result/exception.
                slot.release_on(completion_future)
                if tenant_slot is not None:
                    tenant_slot.release_on(completion_future)
                metrics_inc("searches_timeout_total", 1.0)
                return _error(
                    503,
                    "search_timeout",
                    f"search timed out after {int(timeout_s * 1000)}ms",
                    request=request,
                    request_id=request_id,
                )
            except asyncio.CancelledError:
                slot.release_on(completion_future)
                if tenant_slot is not None:
                    tenant_slot.release_on(completion_future)
                raise
            except ServiceError as exc:
                status_map = {
                    "embedder_unavailable": 503,
                    "collection_not_found": 404,
                    "vectors_only": 400,
                    "invalid_vector": 400,
                    "dim_mismatch": 400,
                    "invalid_collection_name": 400,
                    "invalid_content_filter": 400,
                    "invalid_search_mode": 400,
                    "search_mode_requires_text": 400,
                    "search_mode_disabled": 400,
                    "collection_limit_reached": 429,
                }
                return _error(
                    status_map.get(exc.code, 500),
                    exc.code,
                    exc.message,
                    request=request,
                    request_id=request_id,
                )
        finally:
            if not slot.detached:
                slot.release()

    # Starlette's base class, so FastAPI's own body-parse and routing errors
    # get the envelope too, not a bare {"detail": ...}.
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ):
        detail = exc.detail
        if isinstance(detail, dict):
            code = detail.get("code", "http_error")
            message = detail.get("error") or detail.get("message") or str(detail)
        else:
            code = "http_error"
            message = str(detail)
        return JSONResponse(
            _apply_trace_fields(
                {"ok": False, "code": code, "error": message},
                request_id=_get_request_id(request),
                latency_ms=_get_latency_ms(request),
            ),
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request,
        exc: RequestValidationError,
    ):
        return JSONResponse(
            _apply_trace_fields(
                {
                    "ok": False,
                    "code": "validation_error",
                    "error": "validation failed",
                    "details": {
                        "errors": jsonable_encoder(
                            _client_validation_errors(exc.errors())
                        ),
                    },
                },
                request_id=_get_request_id(request),
                latency_ms=_get_latency_ms(request),
            ),
            status_code=422,
        )

    @app.middleware("http")
    async def http_metrics_middleware(request: Request, call_next):
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            path = getattr(route, "path", None)
            if path is None:
                # No route matched, so the URL path is pure caller input:
                # labelling with it would mint unbounded, permanently
                # persisted series in the shared metrics catalog.
                path = "<unmatched>"
            elif request.url.path.startswith("/v1/") and not path.startswith("/v1/"):
                path = f"/v1{path}"
            # Same reason as the path above: HTTP accepts any RFC token as a
            # method, so an unrecognised verb is caller input and would mint
            # unbounded permanent series just as a raw path would.
            method = request.method
            if method not in _KNOWN_HTTP_METHODS:
                method = "<OTHER>"  # record_http_request upper-cases
            record_http_request(method, path, status_code)

    app.include_router(
        build_health_router(
            cfg,
            VERSION,
            _trace_body,
        ),
        prefix="",
    )
    app.include_router(
        build_admin_router(
            _error,
            _resp,
            _get_request_id,
            _trace_body,
            _do_search,
        ),
        prefix="/v1",
    )
    app.include_router(
        build_collections_router(
            _error,
            _resp,
            _get_request_id,
            _trace_body,
        ),
        prefix="/v1",
    )
    app.include_router(
        build_documents_router(
            cfg,
            _error,
            _resp,
            _get_request_id,
            _trace_body,
        ),
        prefix="/v1",
    )
    app.include_router(
        build_search_router(
            cfg,
            _do_search,
            _error,
            _resp,
            _get_request_id,
            _trace_body,
        ),
        prefix="/v1",
    )
    return app


def _wrap_banner_fields(
    fields: list[str], prefix: str = "│  ", width: int = 88
) -> list[str]:
    """Pack key=value tokens into prefixed lines no wider than `width`.

    Each token is kept atomic (values may contain spaces), so the only break
    points are between tokens.
    """
    lines: list[str] = []
    current = prefix
    for token in fields:
        candidate = current + (" " if current != prefix else "") + token
        if len(candidate) > width and current != prefix:
            lines.append(current)
            current = prefix + token
        else:
            current = candidate
    if current != prefix:
        lines.append(current)
    return lines


def _uvicorn_access_log_config(path: str | None = None) -> dict:
    log_config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    log_config["formatters"]["access"]["()"] = "pave.log.AccessFormatter"
    if path is not None:
        log_config["handlers"]["access"] = {
            "formatter": "access",
            "class": "pave.log.AccessFileHandler",
            "filename": path,
            "encoding": "utf-8",
        }
    return log_config


def main_srv(argv=None):
    """
    HTTP server entrypoint.
    Precedence: active config (env first) > defaults.
    """
    p = argparse.ArgumentParser(
        prog="pavesrv",
        description="Run the PaveDB HTTP server",
        epilog=help_notice("pavesrv"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "-V",
        "--version",
        action="version",
        version=version_banner("pavesrv"),
        help="Print version, copyright, and license information",
    )
    p.add_argument(
        "--home",
        help=(
            "Instance home directory; omitted config.yml, tenants.yml, "
            "and data/ default under that root"
        ),
    )
    p.add_argument(
        "--config",
        help="Explicit config.yml path; default comes from PAVEDB_CONFIG/config search",
    )
    p.add_argument(
        "--tenants",
        help=(
            "Explicit tenants.yml path; default comes from "
            "PAVEDB_AUTH__TENANTS_FILE/config"
        ),
    )
    p.add_argument(
        "--data-dir",
        dest="data_dir",
        help="Explicit data directory; default comes from PAVEDB_DATA_DIR/config",
    )
    args = p.parse_args(argv)
    default_home = os.path.expanduser(DEFAULT_HOME)
    default_config = os.path.join(default_home, "config.yml")
    has_explicit_instance = any(
        (
            args.home,
            args.config,
            args.tenants,
            args.data_dir,
            os.environ.get(HOME_ENV),
            os.environ.get("PAVEDB_CONFIG"),
            os.environ.get("PAVEDB_AUTH__TENANTS_FILE"),
            os.environ.get("PAVEDB_DATA_DIR"),
        )
    )

    paths = apply_runtime_env(
        home=args.home,
        config=args.config,
        tenants=args.tenants,
        data_dir=args.data_dir,
    )
    global _app
    _app = None
    cfg = reload_cfg()

    # Parent bootstrap lets policy validate first boot; workers repeat it in
    # build_app so their fresh config sees the generated key file.
    bootstrap_admin_key(cfg, paths.home)
    log = get_logger()
    if not has_explicit_instance and not os.path.isfile(default_config):
        log.warning(
            "Running from defaults/env only. Run `pavecli init` to create "
            f"{default_config} and {os.path.join(default_home, 'tenants.yml')}"
        )

    # Policy:
    # - fail fast without auth in prod;
    # - auth=none only in dev with loopback;
    # - raises on invalid config.
    enforce_policy(cfg)

    # Resolve bind host/port.
    host, port = resolve_bind(cfg)
    cfg.set("server.host", host)
    cfg.set("server.port", port)

    # Read flags from the active config.
    reload = bool(cfg.get("server.reload"))
    workers = int(cfg.get("server.workers"))
    log_level = str(cfg.get("log.level")).lower()
    timeout_keep_alive = int(cfg.get("server.timeout_keep_alive"))

    if cfg_bool(cfg.get("dev")) and "PAVEDB_LOG__LEVEL" not in os.environ:
        log_level = "debug"
    log.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    _s_cap = int(cfg.get("search.max_concurrent"))
    _s_to = int(cfg.get("search.timeout_ms"))
    _i_cap = int(cfg.get("ingest.max_concurrent"))
    _tc = cfg.get("tenants") or {}
    _tcap = (
        int(_tc.get("default_max_concurrent", -1))
        if isinstance(_tc, dict)
        else -1
    )
    _ops_dest = cfg.get("log.ops_log") or "null"
    _acc_dest = cfg.get("log.access_log")
    _s_cap_label = (
        "unlimited (-1 disables gate)" if _s_cap == -1 else str(_s_cap)
    )
    _i_cap_label = (
        "unlimited (-1 disables gate)" if _i_cap == -1 else str(_i_cap)
    )
    _t_cap_label = (
        "unlimited (-1 disables gate)" if _tcap == -1 else str(_tcap)
    )
    _inst_desc = cfg.get("instance.desc")
    log.info(" ")
    log.info(f"┌─ {software_title('pavesrv', emoji=True)}")
    log.info(f"│  {product_display_name(cfg.get('instance.name'))} — {_inst_desc}")
    log.info(f"│  {COPYRIGHT}")
    log.info("│")
    _fields = [
        f"auth={cfg.get('auth.mode')}",
        f"store={cfg.get('vector_store.type')}",
        f"data_dir={cfg.get('data_dir')}",
        f"bind={host}:{port}",
        f"workers={workers}",
        f"search.max_concurrent={_s_cap_label}",
        f"search.timeout_ms={_s_to}",
        f"ingest.max_concurrent={_i_cap_label}",
        f"tenants.default_max_concurrent={_t_cap_label}",
        f"ops_log={_ops_dest}",
    ]
    for _line in _wrap_banner_fields(_fields):
        log.info(_line)
    log.info("└" + "─" * 40)
    log.info(" ")

    # Access log routing
    _acc_val = str(_acc_dest).strip().lower() if _acc_dest else ""
    _access_log_config = _uvicorn_access_log_config()
    if _acc_val and _acc_val not in ("null", "none"):
        if _acc_val != "stdout":
            try:
                access_path = os.path.expanduser(str(_acc_dest))
                access_dir = os.path.dirname(access_path)
                if access_dir:
                    os.makedirs(access_dir, mode=0o700, exist_ok=True)
                _access_log_config = _uvicorn_access_log_config(access_path)
            except OSError as exc:
                log.warning(
                    f"access log disabled: cannot open {_acc_dest} ({exc})"
                )
    _access_log = _acc_val not in ("", "null", "none")

    # Run the server.
    _run_kwargs = {
        "host": host,
        "port": port,
        "reload": reload,
        "workers": workers,
        "log_level": log_level,
        "timeout_keep_alive": timeout_keep_alive,
        "access_log": _access_log,
    }
    if _access_log_config:
        _run_kwargs["log_config"] = _access_log_config
    previous_asgi_context = os.environ.get(_ASGI_CONTEXT_ENV)
    os.environ[_ASGI_CONTEXT_ENV] = "pavesrv"
    try:
        with DataDirLease(str(cfg.get("data_dir"))):
            _run_pave_server("pave.main:app", **_run_kwargs)
    except (
        DataDirInUseError,
        SchemaMigrationError,
        UnsupportedSchemaVersionError,
    ) as exc:
        raise SystemExit(f"ERROR: {exc}") from None
    finally:
        if previous_asgi_context is None:
            os.environ.pop(_ASGI_CONTEXT_ENV, None)
        else:
            os.environ[_ASGI_CONTEXT_ENV] = previous_asgi_context

# Lazy module-level `app` — only built when first accessed (e.g. by Uvicorn).
# Importing `build_app` or `VERSION` from this module is side-effect-free.
_app = None


def __getattr__(name: str):
    if name == "app":
        if os.environ.get(_ASGI_CONTEXT_ENV) != "pavesrv":
            raise SystemExit(
                "ERROR: direct ASGI startup is unsupported; run `pavesrv`."
            )
        global _app
        if _app is None:
            _app = build_app(get_cfg())
            if getattr(_app.state, "ui_enabled", True):
                try:
                    attach_ui(_app)
                except Exception:
                    pass
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    main_srv()
