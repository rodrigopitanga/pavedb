# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

# pave/auth.py

from __future__ import annotations
from contextlib import asynccontextmanager
from dataclasses import dataclass
# typing imports removed
from fastapi import HTTPException, Depends, Security, Request, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from . import config as cfg

bearer = HTTPBearer(auto_error=False)

@dataclass
class AuthContext:
    tenant: str | None
    is_admin: bool

def _raise_401():
    raise HTTPException(
        status_code=401,
        detail={
            "code": "auth_invalid",
            "error": "missing or invalid authorization header",
        },
        headers={"WWW-Authenticate": 'Bearer realm="pavedb", error="invalid_token"'},
    )

def _raise_403():
    raise HTTPException(
        status_code=403,
        detail={"code": "auth_forbidden", "error": "forbidden"},
        headers={
            "WWW-Authenticate":
                'Bearer realm="pavedb", error="insufficient_scope"'
        },
    )

def _raise_500(mode):
    raise HTTPException(
        status_code=500,
        detail={"code": "auth_mode_unknown", "error": f"unknown auth mode: {mode}"},
    )

def auth_ctx(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer)
) -> AuthContext:
    # read from CFG.get so tests and env overrides work
    mode = str(cfg.CFG.get("auth.mode", "none")).strip().lower()

    if mode == "none":
        # open mode (dev): treat as admin
        tenant = str(cfg.CFG.get("auth.default_access_tenant", None))
        return AuthContext(tenant=tenant, is_admin=True)

    if mode == "static":
        token = None
        if credentials and credentials.scheme == "Bearer":
            token = credentials.credentials.strip()
        if not token:
            _raise_401()

        # global key
        global_key = cfg.CFG.get("auth.global_key")
        if global_key and token == str(global_key):
            return AuthContext(tenant=None, is_admin=True)

        # per-tenant keys
        api_keys: dict[str, str] = cfg.CFG.get("auth.api_keys", {}) or {}
        for t, expected in api_keys.items():
            if token == str(expected):
                return AuthContext(tenant=t, is_admin=False)
        _raise_403()

    _raise_500(mode)

def authorize_tenant(tenant: str, ctx: AuthContext = Depends(auth_ctx)) -> AuthContext:
    if ctx.is_admin or ctx.tenant == tenant:
        return ctx
    _raise_403()


async def tenant_rate_limit(
    request: Request,
    response: Response,
    ctx: AuthContext = Depends(authorize_tenant),
):
    """Per-tenant concurrent cap. Admin and unconfigured tenants bypass."""
    tenant = ctx.tenant
    if ctx.is_admin or tenant is None:
        yield ctx
        return

    # per-tenant override → global default → 0 (unlimited)
    max_c = request.app.state.tenant_limits.get(
        tenant, request.app.state.tenant_default_limit
    )
    if max_c <= 0:
        yield ctx
        return

    active = request.app.state.tenant_active
    if active.get(tenant, 0) >= max_c:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "tenant_rate_limited",
                "error": "too many concurrent requests for this tenant",
            },
            headers={
                "Retry-After": "1",
                "X-RateLimit-Limit": str(max_c),
                "X-RateLimit-Remaining": "0",
            },
        )
    active[tenant] = active.get(tenant, 0) + 1
    remaining = max(0, max_c - active[tenant])
    response.headers["X-RateLimit-Limit"] = str(max_c)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    try:
        yield ctx
    finally:
        active[tenant] = max(0, active[tenant] - 1)


@asynccontextmanager
async def tenant_limit_gate(
    request: Request,
    response: Response,
    tenant: str | None,
):
    """Apply per-tenant concurrent caps outside auth-bound dependencies."""
    if tenant is None:
        yield
        return

    max_c = request.app.state.tenant_limits.get(
        tenant, request.app.state.tenant_default_limit
    )
    if max_c <= 0:
        yield
        return

    active = request.app.state.tenant_active
    if active.get(tenant, 0) >= max_c:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "tenant_rate_limited",
                "error": "too many concurrent requests for this tenant",
            },
            headers={
                "Retry-After": "1",
                "X-RateLimit-Limit": str(max_c),
                "X-RateLimit-Remaining": "0",
            },
        )

    active[tenant] = active.get(tenant, 0) + 1
    remaining = max(0, max_c - active[tenant])
    response.headers["X-RateLimit-Limit"] = str(max_c)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    try:
        yield
    finally:
        active[tenant] = max(0, active[tenant] - 1)


# --- Startup security policy -------------------------------------------------

def _is_dev(cfg) -> bool:
    # check dev flag from config/env overlay
    return bool(cfg.get("dev", False))

def enforce_policy(cfg) -> None:
    """
    Fail fast if auth is not configured in prod.
    Allow auth=none only in dev mode, force loopback bind.
    """
    mode = str(cfg.get("auth.mode", "none")).strip().lower()
    dev = _is_dev(cfg)

    if mode == "none":
        if not dev:
            raise RuntimeError(
                "auth.mode=none not allowed in production. "
                "Set auth.mode=static with a key or run with PAVEDB_DEV=1 for dev."
            )
        host = str(cfg.get("server.host", "127.0.0.1")).strip()
        if host not in ("127.0.0.1", "localhost"):
            # enforce loopback in dev
            try:
                cfg._data["server.host"] = "127.0.0.1"
            except Exception:
                pass

    if mode == "static":
        has_global = bool(cfg.get("auth.global_key"))
        has_map = bool(cfg.get("auth.api_keys"))
        if not (has_global or has_map):
            raise RuntimeError(
                "auth.mode=static requires global_key or api_keys"
            )

def resolve_bind(cfg) -> tuple[str, int]:
    # return host/port after policy enforcement
    host = str(cfg.get("server.host", "127.0.0.1"))
    port = int(cfg.get("server.port", 8086))
    return host, port
