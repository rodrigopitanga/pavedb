# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import argparse, asyncio, os, logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import uvicorn

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

from pave.config import get_cfg, get_logger, reload_cfg
from pave.auth import enforce_policy, resolve_bind
from pave.embedders import get_embedder
from pave.metrics import set_data_dir as metrics_set_data_dir, \
    flush as metrics_flush
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
from pave.runtime_paths import DEFAULT_HOME, apply_runtime_env
import pave.log as ops_log

VERSION = "0.5.9"


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


# Dependency injection builder
def build_app(cfg=get_cfg()) -> FastAPI:

    log = get_logger()

    def _resp(*codes: int) -> dict[int, dict[str, type[ErrorResponse]]]:
        return {code: {"model": ErrorResponse} for code in codes}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Eagerly load the sentence-transformer model so the first
        # request doesn't pay the cold-start penalty.
        try:
            app.state.store.create_collection("_system", "health")
            log.info("Embedding model warm-up complete")
        except Exception as e:
            log.warning(f"Embedding model warm-up failed: {e}")
        yield
        metrics_flush()
        ops_log.close()
        for _exec in (app.state.search_executor, app.state.ingest_executor):
            if _exec is not None:
                _exec.shutdown(wait=False)

    app = FastAPI(
        title=cfg.get("instance.name","Patchvec"),
        description=cfg.get("instance.desc","Vector Search Microservice"),
        lifespan=lifespan,
    )
    app.state.store = LocalStore(
        data_dir=str(cfg.get("data_dir")),
        embedder=get_embedder(),
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

    async def _do_search(fn):
        """Concurrency gate + timeout wrapper for all search handlers."""
        timeout_s = app.state.search_timeout_s
        max_s = app.state.max_searches
        # Check-and-increment has no await between them: atomic in asyncio.
        if max_s > 0:
            if app.state.active_searches >= max_s:
                return _error(
                    503, "search_overloaded",
                    "too many concurrent searches, try again later",
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
                return result
            except asyncio.TimeoutError:
                # Thread keeps running; suppress its eventual result/exception.
                future.add_done_callback(
                    lambda f: f.exception() if not f.cancelled() else None
                )
                return _error(
                    503, "search_timeout",
                    f"search timed out after {int(timeout_s * 1000)}ms",
                )
            except ServiceError as exc:
                return _error(500, exc.code, exc.message)
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
            {"ok": False, "code": code, "error": message},
            status_code=exc.status_code,
            headers=exc.headers,
        )

    # Initialize metrics persistence
    data_dir = cfg.get("data_dir")
    if data_dir:
        metrics_set_data_dir(data_dir)

    def _error(status_code: int, code: str, message: str) -> JSONResponse:
        return JSONResponse(
            {"ok": False, "code": code, "error": message},
            status_code=status_code,
        )

    app.include_router(build_health_router(cfg, VERSION))
    app.include_router(build_admin_router(_error, _resp), prefix="/v1")
    app.include_router(build_collections_router(_error, _resp), prefix="/v1")
    app.include_router(build_documents_router(cfg, _error, _resp), prefix="/v1")
    app.include_router(build_search_router(cfg, _do_search, _resp), prefix="/v1")

    return app

def main_srv(argv=None):
    """
    HTTP server entrypoint.
    Precedence: CFG (reads env first) > defaults.
    """
    p = argparse.ArgumentParser(prog="pavesrv")
    p.add_argument(
        "--home",
        help="Use an instance home dir",
    )
    p.add_argument("--config", help="Explicit config.yml path")
    p.add_argument("--tenants", help="Explicit tenants.yml path")
    p.add_argument("--data-dir", dest="data_dir", help="Explicit data directory")
    args = p.parse_args(argv)
    default_home = os.path.expanduser(DEFAULT_HOME)
    default_config = os.path.join(default_home, "config.yml")
    has_explicit_instance = any((
        args.home,
        args.config,
        args.tenants,
        args.data_dir,
        os.environ.get("PAVEDB_CONFIG"),
        os.environ.get("PAVEDB_AUTH__TENANTS_FILE"),
        os.environ.get("PAVEDB_DATA_DIR"),
        os.environ.get("PATCHVEC_CONFIG"),
        os.environ.get("PATCHVEC_AUTH__TENANTS_FILE"),
        os.environ.get("PATCHVEC_DATA_DIR"),
    ))

    apply_runtime_env(
        home=args.home,
        config=args.config,
        tenants=args.tenants,
        data_dir=args.data_dir,
    )
    global _app
    _app = None
    cfg = reload_cfg()
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

    # flags from CFG
    reload = bool(cfg.get("server.reload", False))
    workers = int(cfg.get("server.workers", 1))
    log_level = str(cfg.get("log.level")).lower()
    timeout_keep_alive = int(cfg.get("server.timeout_keep_alive"))

    if cfg.get("dev",0):
        log_level = "debug"
        log.setLevel(logging.DEBUG)

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
    log.info(f"┌─ Welcome to PatchVEC 🍰 v{VERSION}")
    log.info(
        f"│  auth={cfg.get('auth.mode','none')} "
        f"store={cfg.get('vector_store.type','faiss')} "
        f"data_dir={cfg.get('data_dir')} "
        f"bind={host}:{port} workers={workers}"
    )
    log.info(
        f"│  search_cap={_s_cap} search_to={_s_to}ms "
        f"ingest_cap={_i_cap} "
        f"tenant_cap={'unlimited' if _tcap == 0 else _tcap} "
        f"ops_log={_ops_dest}"
    )
    log.info("└" + "─" * 40)

    # Access log routing
    _acc_val = str(_acc_dest).strip().lower() if _acc_dest else ""
    if _acc_val and _acc_val not in ("null", "none"):
        if _acc_val != "stdout":
            logging.getLogger("uvicorn.access").addHandler(
                logging.FileHandler(_acc_dest)
            )
    _access_log = True

    # run server
    uvicorn.run("pave.main:app",
                host=host,
                port=port,
                reload=reload,
                workers=workers,
                log_level=log_level,
                timeout_keep_alive=timeout_keep_alive,
                access_log=_access_log,
                )

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
