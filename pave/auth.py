# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

# pave/auth.py

from __future__ import annotations
from contextlib import asynccontextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
# typing imports removed
from fastapi import HTTPException, Depends, Security, Request, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from .config import get_cfg, get_logger

bearer = HTTPBearer(auto_error=False)
log = get_logger()


def _admin_key_path(home: str | Path) -> Path:
    return Path(home).expanduser() / "admin.key"


def _read_admin_key(path: Path) -> str:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise RuntimeError(
            f"admin key file must be mode 0600; run chmod 600 {path}"
        )
    key = path.read_text(encoding="utf-8").strip()
    if not key:
        raise RuntimeError(f"admin key file is empty: {path}")
    return key


def _write_admin_key(path: Path, key: str, *, replace: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not replace:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as key_file:
                key_file.write(f"{key}\n")
                key_file.flush()
                os.fsync(key_file.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        os.chmod(path, 0o600)
        return

    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".admin.key.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as key_file:
            key_file.write(f"{key}\n")
            key_file.flush()
            os.fsync(key_file.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def current_admin_key(cfg, home: str | Path) -> str:
    configured = cfg.get("auth.global_key")
    if configured:
        return str(configured)
    path = _admin_key_path(home)
    cfg.set("auth.admin_key_file", str(path))
    try:
        return _read_admin_key(path)
    except FileNotFoundError as exc:
        raise RuntimeError(f"admin key file does not exist: {path}") from exc


def ensure_admin_key(cfg, home: str | Path) -> str:
    configured = cfg.get("auth.global_key")
    if configured:
        return str(configured)
    path = _admin_key_path(home)
    cfg.set("auth.admin_key_file", str(path))
    try:
        return _read_admin_key(path)
    except FileNotFoundError:
        key = secrets.token_urlsafe(32)
        try:
            _write_admin_key(path, key, replace=False)
        except FileExistsError:
            return _read_admin_key(path)
        print(
            f"pavedb: generated admin key (prefix={key[:4]}...); "
            f"full key in {path}",
            file=sys.stderr,
        )
        return key


def bootstrap_admin_key(cfg, home: str | Path) -> str | None:
    """Generate/persist the admin key only when static auth needs it.

    In ``auth.mode=none`` (dev), ``auth_ctx`` short-circuits to admin and never
    reads the key file, so bootstrapping a 0600 admin.key there is pointless.
    """
    if str(cfg.get("auth.mode")).strip().lower() != "static":
        return None
    return ensure_admin_key(cfg, home)


def rotate_admin_key(cfg, home: str | Path) -> str:
    if cfg.get("auth.global_key"):
        raise RuntimeError(
            "admin key is configured explicitly; update auth.global_key instead"
        )
    path = _admin_key_path(home)
    if path.exists():
        _read_admin_key(path)
    key = secrets.token_urlsafe(32)
    _write_admin_key(path, key, replace=True)
    cfg.set("auth.admin_key_file", str(path))
    return key


def _file_admin_key(cfg) -> str | None:
    path = cfg.get("auth.admin_key_file")
    return _read_admin_key(Path(path)) if path else None

@dataclass
class AuthContext:
    tenant: str | None
    is_admin: bool


def actor_of(ctx: AuthContext) -> str:
    if ctx.is_admin:
        return "admin"
    if ctx.tenant:
        return f"tenant:{ctx.tenant}"
    return "admin"

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
    # Read per request so tests and runtime overlays are visible.
    cfg = get_cfg()
    mode = str(cfg.get("auth.mode")).strip().lower()

    if mode == "none":
        # open mode (dev): treat as admin
        tenant = str(cfg.get("auth.default_access_tenant"))
        return AuthContext(tenant=tenant, is_admin=True)

    if mode == "static":
        token = credentials.credentials.strip() if credentials else None
        if not token:
            _raise_401()

        # global key
        global_key = cfg.get("auth.global_key") or _file_admin_key(cfg)
        # No str() here either: this credential grants cross-tenant admin, so
        # a mistyped config value must not become a guessable literal token.
        if isinstance(global_key, str) and global_key and token == global_key:
            return AuthContext(tenant=None, is_admin=True)

        # per-tenant keys
        api_keys: dict[str, str] = cfg.get("auth.api_keys") or {}
        for t, expected in api_keys.items():
            # No str() here: coercing a non-string would turn a mistyped
            # config value into a guessable literal token ("True", "12345").
            if isinstance(expected, str) and token == expected:
                return AuthContext(tenant=t, is_admin=False)
        _raise_403()

    _raise_500(mode)

def authorize_tenant(tenant: str, ctx: AuthContext = Depends(auth_ctx)) -> AuthContext:
    if ctx.is_admin or ctx.tenant == tenant:
        return ctx
    _raise_403()


def _tenant_cap_target(path: str, method: str) -> str:
    if path.endswith("/search"):
        return "search"
    if method.upper() == "POST" and path.endswith("/documents"):
        return "ingest"
    return "tenant request"


def _tenant_limit_exception(
    *,
    tenant: str,
    active: int,
    limit: int,
    source: str,
    target: str,
    path: str,
    request_id: str | None,
) -> HTTPException:
    log.warning(
        "%s capped by %s tenant=%s active=%s limit=%s path=%s request_id=%s",
        target,
        source,
        tenant,
        active,
        limit,
        path,
        request_id,
    )
    return HTTPException(
        status_code=429,
        detail={
            "code": "tenant_rate_limited",
            "error": (
                f"{target} capped by {source} for tenant {tenant} "
                f"(active={active}, limit={limit})"
            ),
        },
        headers={
            "Retry-After": "1",
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": "0",
        },
    )


class ConcurrencySlot:
    """One held concurrency slot, released exactly once from any thread.

    Work handed to an executor cannot be recalled when the request that
    submitted it times out or the client disconnects, so the slot has to
    follow the worker thread rather than the response. ``release_on``
    detaches it; the request scope then leaves the release to the pool
    thread. Release takes the same lock the admission check takes because
    it now runs off the event loop, and it is idempotent so the normal and
    the detached path cannot both decrement.
    """

    def __init__(self, lock, decrement):
        self._lock = lock
        self._decrement = decrement
        self._released = False
        self.detached = False

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
            self._decrement()

    def release_on(self, future) -> None:
        """Hand the slot to `future`'s worker; release when it finishes."""
        self.detached = True
        future.add_done_callback(self._finish)

    def _finish(self, done) -> None:
        try:
            if not done.cancelled():
                # Consume it: nothing awaits this result any more.
                done.exception()
        finally:
            self.release()


async def tenant_rate_limit(
    request: Request,
    response: Response,
    ctx: AuthContext = Depends(authorize_tenant),
):
    """Per-tenant concurrent cap. Admin and unconfigured tenants bypass."""
    if ctx.is_admin or ctx.tenant is None:
        yield ctx
        return
    async with tenant_limit_gate(request, response, ctx.tenant):
        yield ctx


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

    # Published so a gate outside this dependency (the search share in
    # pave.main) charges the same identity this cap charges, instead of
    # guessing one. Set before the max_c short-circuit: an instance that
    # turned the request cap off still wants the share to work.
    request.state.capped_tenant = tenant

    max_c = request.app.state.tenant_limits.get(
        tenant, request.app.state.tenant_default_limit
    )
    if max_c <= 0:
        yield
        return

    active = request.app.state.tenant_active
    lock = request.app.state.tenant_lock
    # Under the lock: a detached search thread releases its slot from the
    # pool, so the check and the increment can no longer assume the event
    # loop serializes every writer.
    with lock:
        current = active.get(tenant, 0)
        admitted = current < max_c
        if admitted:
            active[tenant] = current + 1
    if not admitted:
        source = (
            "tenant-scoped cap"
            if tenant in request.app.state.tenant_limits
            else "instance-scoped tenant cap"
        )
        raise _tenant_limit_exception(
            tenant=tenant,
            active=current,
            limit=max_c,
            source=source,
            target=_tenant_cap_target(request.url.path, request.method),
            path=request.url.path,
            request_id=getattr(request.state, "request_id", None),
        )

    def _decrement() -> None:
        active[tenant] = max(0, active.get(tenant, 0) - 1)

    slot = ConcurrencySlot(lock, _decrement)
    # Published so work that outlives this request (a detached search
    # thread) can keep the tenant's slot until it really finishes.
    request.state.tenant_slot = slot
    remaining = max(0, max_c - (current + 1))
    response.headers["X-RateLimit-Limit"] = str(max_c)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    try:
        yield
    finally:
        if not slot.detached:
            slot.release()


# --- Startup security policy -------------------------------------------------

def _is_dev(cfg) -> bool:
    # check dev flag from config/env overlay
    return bool(cfg.get("dev"))


# Deliberately the same rule as _COLLECTION_NAME_RE in pave/service.py: one
# identifier rule for both, asserted by a test so the two cannot drift.
# Tenant names become a directory component while authorization compares them
# byte-exactly, so the rule has to hold on every filesystem: lowercase ASCII
# means no pair can collide under case folding (APFS, NTFS, SMB) or Unicode
# normalization, and excluding '.' '/' '\' and control characters keeps the
# name a single, traversal-free path segment everywhere.
_TENANT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def _check_tenant_name(name: str, source: str) -> None:
    if _TENANT_NAME_RE.fullmatch(name):
        return
    raise RuntimeError(
        f"invalid tenant name {name!r} in {source}: tenant names must match "
        "^[a-z0-9][a-z0-9-]{0,62}$ (lowercase ASCII, digits and hyphen). "
        "Rename the tenant and move its data directory "
        f"(<data_dir>/t_{name} -> <data_dir>/t_<new-name>) before restarting; "
        "renaming without moving the directory orphans that tenant's data."
    )


def _check_api_key(source: str, key) -> None:
    # An unquoted YAML scalar parses as bool/int/date, so `acme: yes` used to
    # become the bearer token "True" once auth_ctx coerced it with str().
    # Surrounding whitespace is rejected too: auth_ctx strips the presented
    # token, so a padded value could never match and would fail obscurely.
    if isinstance(key, str) and key.strip() and key == key.strip():
        return
    raise RuntimeError(
        f"invalid bearer token for {source}: tokens must be non-empty "
        "strings without surrounding whitespace. Quote the value so YAML "
        "does not parse it as a boolean, number or date."
    )


def _enforce_tenant_names(cfg) -> None:
    api_keys = cfg.get("auth.api_keys") or {}
    if not isinstance(api_keys, dict):
        raise RuntimeError(
            "auth.api_keys must be a map of tenant name to bearer token"
        )
    # auth_ctx returns the first tenant whose token matches, so a token shared
    # by two tenants binds every holder to whichever comes first in map order:
    # one customer would read and write the other's data.
    seen: dict[str, str] = {}
    # The admin credential is the first token auth_ctx tries, so it belongs in
    # the same type check and the same collision map: a tenant token equal to
    # it would authenticate as admin, not as that tenant.
    global_key = cfg.get("auth.global_key")
    if global_key is not None:
        _check_api_key("auth.global_key", global_key)
        seen[global_key] = "auth.global_key"
    for name, key in api_keys.items():
        _check_tenant_name(str(name), "auth.api_keys")
        _check_api_key(f"tenant {str(name)!r} in auth.api_keys", key)
        first = seen.setdefault(key, str(name))
        if first != str(name):
            raise RuntimeError(
                f"duplicate bearer token: {first!r} and {str(name)!r} share "
                "one key, so its holder authenticates as the first of them. "
                "Give each its own key before restarting."
            )
    # Not just the configured keys: in auth.mode=none this value becomes a
    # real tenant namespace on disk (auth_ctx, above).
    _check_tenant_name(
        str(cfg.get("auth.default_access_tenant") or ""),
        "auth.default_access_tenant",
    )
    # The shared-corpus tenant reaches store.search() unvalidated from
    # routes/search.py, so it is a raw t_<x> path segment too.
    if cfg.get("common_enabled"):
        _check_tenant_name(str(cfg.get("common_tenant") or ""), "common_tenant")
        # Same rule, same reason: it reaches store.search() as a raw c_<x> path
        # segment, and validating it here is what lets the search path reject a
        # caller-supplied collection name without also rejecting this one.
        _check_tenant_name(
            str(cfg.get("common_collection") or ""), "common_collection"
        )

def enforce_policy(cfg) -> None:
    """
    Fail fast if auth is not configured in prod.
    Allow auth=none only in dev mode, force loopback bind.
    """
    mode = str(cfg.get("auth.mode")).strip().lower()
    dev = _is_dev(cfg)

    _enforce_tenant_names(cfg)

    if mode == "none":
        if not dev:
            raise RuntimeError(
                "auth.mode=none not allowed in production. "
                "Set auth.mode=static with a key or run with PAVEDB_DEV=1 for dev."
            )
        host = str(cfg.get("server.host")).strip()
        if host not in ("127.0.0.1", "localhost"):
            cfg.set("server.host", "127.0.0.1")

    if mode == "static":
        has_global = bool(cfg.get("auth.global_key"))
        has_map = bool(cfg.get("auth.api_keys"))
        has_file = bool(cfg.get("auth.admin_key_file"))
        if not (has_global or has_map or has_file):
            raise RuntimeError(
                "auth.mode=static requires global_key or api_keys"
            )

    if mode not in ("none", "static"):
        raise RuntimeError(
            f"unknown auth.mode: {mode!r}. Valid values: none, static."
        )

def resolve_bind(cfg) -> tuple[str, int]:
    # return host/port after policy enforcement
    host = str(cfg.get("server.host"))
    port = int(cfg.get("server.port"))
    return host, port
