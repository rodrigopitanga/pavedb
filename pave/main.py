# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import argparse, asyncio, copy, os, logging, signal, sys, threading, time, uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager, suppress
import uvicorn
from uvicorn.config import Config
from uvicorn.main import STARTUP_FAILURE
from uvicorn.server import HANDLED_SIGNALS, Server
from uvicorn.supervisors import ChangeReload, Multiprocess

from fastapi import FastAPI, Request, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from pave.config import get_cfg, get_logger, reload_cfg, should_warm_default_embedder
from pave.auth import bootstrap_admin_key, enforce_policy, resolve_bind
from pave.embedders import LazyEmbedder, get_embedder
from pave.metrics import (
    flush as metrics_flush,
    flush_if_idle as metrics_flush_if_idle,
    record_http_request,
    set_error as metrics_set_error,
    set_data_dir as metrics_set_data_dir,
)
from pave.stores.local import LocalStore
from pave.service import ServiceError
from pave.schemas import ErrorResponse
from pave.routes import (
    build_admin_router,
    build_collections_router,
    build_documents_router,
    build_health_router,
    build_search_router,
)
from pave.ui import attach_ui
from pave.runtime_paths import DEFAULT_HOME, HOME_ENV, apply_runtime_env
from pave.version import (
    COPYRIGHT,
    VERSION,
    help_notice,
    product_display_name,
    software_title,
    version_banner,
)
import pave.log as ops_log

METRICS_FLUSH_INTERVAL_SECONDS = 10.5
METRICS_SNAPSHOT_IDLE_SECONDS = 0.5
CATALOG_METRICS_REFRESH_INTERVAL_SECONDS = 10.0
GRACEFUL_SHUTDOWN_MESSAGE = (
    "Finishing current work, shutting down soon. Press Ctrl+C again to "
    "shut down immediately (potential data loss)."
)
FORCE_SHUTDOWN_MESSAGE = (
    "Immediate shutdown requested; exiting now (potential data loss)."
)


def _hw_info() -> dict:
    """Collect server hardware info once at startup (stdlib-only, multiplatform)."""
    import platform, sys
    info: dict = {
        "hw_cpu":   platform.processor() or platform.machine(),
        "hw_cores": os.cpu_count(),
        "hw_os":    f"{platform.system()} {platform.release()}",
    }
    try:
        if sys.platform == "linux":
            with open("/proc/meminfo", encoding="ascii") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        info["hw_ram_gb"] = round(int(line.split()[1]) / 1_000_000, 1)
                        break
        elif sys.platform == "darwin":
            import subprocess
            out = subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"], text=True, timeout=2
            )
            info["hw_ram_gb"] = round(int(out.strip()) / 1_000_000_000, 1)
    except Exception:
        pass
    return info


async def _flush_metrics_periodically() -> None:
    while True:
        await asyncio.sleep(METRICS_FLUSH_INTERVAL_SECONDS)
        metrics_flush_if_idle(METRICS_SNAPSHOT_IDLE_SECONDS)


async def _refresh_catalog_metrics_periodically(store) -> None:
    loop = asyncio.get_running_loop()
    while True:
        try:
            await loop.run_in_executor(None, store.refresh_catalog_metrics)
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


# Dependency injection builder
def build_app(cfg=get_cfg()) -> FastAPI:

    log = get_logger()

    def _resp(*codes: int) -> dict[int, dict[str, type[ErrorResponse]]]:
        return {code: {"model": ErrorResponse} for code in codes}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        metrics_flush_task = asyncio.create_task(_flush_metrics_periodically())
        catalog_metrics_task = asyncio.create_task(
            _refresh_catalog_metrics_periodically(app.state.store)
        )
        if should_warm_default_embedder(cfg):
            started = time.perf_counter()
            try:
                app.state.store.warm()
                elapsed_ms = (time.perf_counter() - started) * 1000
                log.info("Default store warm-up complete in %.1f ms", elapsed_ms)
            except Exception as exc:
                elapsed_ms = (time.perf_counter() - started) * 1000
                log.warning(
                    "Default store warm-up failed after %.1f ms: %s",
                    elapsed_ms,
                    exc,
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
            metrics_flush()
            ops_log.close()

    app = FastAPI(
        title=product_display_name(cfg.get("instance.name")),
        description=cfg.get("instance.desc"),
        version=VERSION,
        lifespan=lifespan,
    )

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
    app.state.cfg = cfg
    app.state.version = VERSION
    app.state.hw_info = _hw_info()

    # Search limits
    _max_conc = int(cfg.get("search.max_concurrent"))
    _to_ms = int(cfg.get("search.timeout_ms"))
    # Dedicated executor: threads == max_concurrent so work starts immediately.
    app.state.search_executor = (
        ThreadPoolExecutor(max_workers=_max_conc) if _max_conc > 0 else None
    )
    # Plain counter instead of threading.Semaphore: check+increment has no
    # await between them, so it is atomic in the asyncio event loop.
    app.state.max_searches = _max_conc
    app.state.active_searches = 0
    app.state.search_timeout_s = _to_ms / 1000.0 if _to_ms > 0 else 0.0

    _max_iconc = int(cfg.get("ingest.max_concurrent"))
    app.state.ingest_executor = (
        ThreadPoolExecutor(max_workers=_max_iconc) if _max_iconc > 0 else None
    )
    app.state.max_ingests = _max_iconc
    app.state.active_ingests = 0

    # Per-tenant concurrency limits
    _tenants_cfg = cfg.get("tenants") or {}
    _raw_def = (
        _tenants_cfg.get("default_max_concurrent")
        if isinstance(_tenants_cfg, dict) else None
    )
    app.state.tenant_default_limit = int(_raw_def) if _raw_def is not None else 0
    app.state.tenant_limits = {}
    app.state.tenant_active = {}
    for _t, _tcfg in (_tenants_cfg.items() if isinstance(_tenants_cfg, dict) else []):
        if _t == "default_max_concurrent" or not isinstance(_tcfg, dict):
            continue
        _lim = _tcfg.get("max_concurrent")
        if _lim is not None:
            app.state.tenant_limits[_t] = int(_lim)
            app.state.tenant_active[_t] = 0

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

    async def _do_search(fn, *, request: Request, request_id: str | None = None):
        """Concurrency gate + timeout wrapper for all search handlers."""
        timeout_s = app.state.search_timeout_s
        max_s = app.state.max_searches
        # Check-and-increment has no await between them: atomic in asyncio.
        if max_s > 0:
            if app.state.active_searches >= max_s:
                active = app.state.active_searches
                log.warning(
                    "search capped by instance-scoped search cap active=%s "
                    "limit=%s path=%s request_id=%s",
                    active,
                    max_s,
                    request.url.path,
                    request_id,
                )
                return _error(
                    503, "search_overloaded",
                    "search capped by instance-scoped search cap "
                    f"(active={active}, limit={max_s})",
                    request=request,
                    request_id=request_id,
                )
            app.state.active_searches += 1
        try:
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(app.state.search_executor, fn)
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
                        404 if result.get("error_type") == "not_found"
                        else 500
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
                future.add_done_callback(
                    lambda f: f.exception() if not f.cancelled() else None
                )
                return _error(
                    503, "search_timeout",
                    f"search timed out after {int(timeout_s * 1000)}ms",
                    request=request,
                    request_id=request_id,
                )
            except ServiceError as exc:
                status_map = {
                    "embedder_unavailable": 503,
                    "collection_not_found": 404,
                }
                return _error(
                    status_map.get(exc.code, 500),
                    exc.code,
                    exc.message,
                    request=request,
                    request_id=request_id,
                )
        finally:
            if max_s > 0:
                app.state.active_searches -= 1

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
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
                        "errors": jsonable_encoder(exc.errors()),
                    },
                },
                request_id=_get_request_id(request),
                latency_ms=_get_latency_ms(request),
            ),
            status_code=422,
        )

    # Initialize metrics persistence.
    workers = int(cfg.get("server.workers") or 1)
    data_dir = cfg.get("data_dir")
    if data_dir and workers == 1:
        metrics_set_data_dir(data_dir)
    elif data_dir and workers > 1:
        log.warning(
            "metrics persistence disabled with server.workers=%s; "
            "metrics.json is process-local",
            workers,
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
            path = getattr(route, "path", request.url.path)
            if request.url.path.startswith("/v1/") and not path.startswith("/v1/"):
                path = f"/v1{path}"
            record_http_request(request.method, path, status_code)

    app.include_router(
        build_health_router(cfg, VERSION, _trace_body),
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
        build_collections_router(_error, _resp, _get_request_id, _trace_body),
        prefix="/v1",
    )
    app.include_router(
        build_documents_router(
            cfg, _error, _resp, _get_request_id, _trace_body),
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


def _uvicorn_access_log_config(path: str) -> dict:
    log_config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    log_config["handlers"]["access"] = {
        "formatter": "access",
        "class": "logging.FileHandler",
        "filename": path,
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
    has_explicit_instance = any((
        args.home,
        args.config,
        args.tenants,
        args.data_dir,
        os.environ.get(HOME_ENV),
        os.environ.get("PAVEDB_CONFIG"),
        os.environ.get("PAVEDB_AUTH__TENANTS_FILE"),
        os.environ.get("PAVEDB_DATA_DIR"),
    ))

    paths = apply_runtime_env(
        home=args.home,
        config=args.config,
        tenants=args.tenants,
        data_dir=args.data_dir,
    )
    global _app
    _app = None
    cfg = reload_cfg()
    # Bootstrap the file admin key only under static auth (none mode never
    # reads it). ponytail: under server.workers>1 the auto-generated key is not
    # visible to worker subprocesses (they import build_app, which does not
    # bootstrap), so admin auth fails-closed there — deferred, same known
    # degradation as metrics persistence under workers>1.
    bootstrap_admin_key(cfg, paths.home)
    log = get_logger()
    if not has_explicit_instance and not os.path.isfile(default_config):
        log.warning(
            "Running from defaults/env only. Run `pavecli init` to create "
            "%s and %s",
            default_config,
            os.path.join(default_home, "tenants.yml"),
        )
    # Policy:
    # - fail fast without auth in prod;
    # - auth=none only in dev with loopback;
    # - raises on invalid config.
    enforce_policy(cfg)

    # resolve bind host/port
    host, port = resolve_bind(cfg)
    cfg.set("server.host", host)
    cfg.set("server.port", port)

    # flags from active config
    reload = bool(cfg.get("server.reload"))
    workers = int(cfg.get("server.workers"))
    log_level = str(cfg.get("log.level")).lower()
    timeout_keep_alive = int(cfg.get("server.timeout_keep_alive"))

    if cfg.get("dev") and "PAVEDB_LOG__LEVEL" not in os.environ:
        log_level = "debug"
    log.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    _s_cap = int(cfg.get("search.max_concurrent"))
    _s_to = int(cfg.get("search.timeout_ms"))
    _i_cap = int(cfg.get("ingest.max_concurrent"))
    _tc = cfg.get("tenants") or {}
    _tcap = (
        int(_tc.get("default_max_concurrent") or 0)
        if isinstance(_tc, dict) else 0
    )
    _ops_dest = cfg.get("log.ops_log") or "null"
    _acc_dest = cfg.get("log.access_log")
    _s_cap_label = (
        "unlimited (0 disables gate)" if _s_cap == 0 else str(_s_cap)
    )
    _i_cap_label = (
        "unlimited (0 disables gate)" if _i_cap == 0 else str(_i_cap)
    )
    _t_cap_label = (
        "unlimited (0 disables gate)" if _tcap == 0 else str(_tcap)
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
    _access_log_config = None
    if _acc_val and _acc_val not in ("null", "none"):
        if _acc_val != "stdout":
            try:
                access_path = os.path.expanduser(str(_acc_dest))
                access_dir = os.path.dirname(access_path)
                if access_dir:
                    os.makedirs(access_dir, exist_ok=True)
                _access_log_config = _uvicorn_access_log_config(access_path)
            except OSError as exc:
                log.warning(
                    "access log disabled: cannot open %s (%s)", _acc_dest, exc
                )
    _access_log = _acc_val not in ("", "null", "none")

    # run server
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
    _run_pave_server("pave.main:app", **_run_kwargs)

# Lazy module-level `app` — only built when first accessed (e.g. by uvicorn).
# Importing `build_app` or `VERSION` from this module is now side-effect-free.
_app = None

def __getattr__(name: str):
    if name == "app":
        global _app
        if _app is None:
            _app = build_app()
            try:
                attach_ui(_app)
            except Exception:
                pass
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

if __name__ == "__main__":
    main_srv()
