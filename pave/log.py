# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import asyncio, functools, json, logging, os, sys, threading, time
from copy import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from uvicorn.logging import AccessFormatter as UvicornAccessFormatter


def exception_diagnostic(exc: BaseException) -> str:
    """Describe a failure chain without provider payloads or runtime locals.

    Exception messages can contain arbitrary credentials, including fragments
    that cannot be reliably matched against configuration or a token pattern.
    Keep types, OS error numbers and frame line numbers for operator diagnosis.
    Traceback filenames can also contain credentials, so omit them.
    """
    failures = []
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        detail = type(exc).__name__
        if isinstance(exc, OSError) and isinstance(exc.errno, int):
            detail += f" errno={exc.errno}"
        frames = []
        tb = exc.__traceback__
        while tb is not None:
            frames.append(f"frame {len(frames) + 1} line {tb.tb_lineno}")
            tb = tb.tb_next
        if frames:
            detail += " at " + " -> ".join(frames)
        failures.append(detail)
        exc = exc.__cause__ or (
            None if exc.__suppress_context__ else exc.__context__
        )
    return " caused by ".join(failures)


class AccessFormatter(UvicornAccessFormatter):
    """Keep query parameters out of HTTP access records."""

    def format(self, record: logging.LogRecord) -> str:
        record = copy(record)
        client, method, target, version, status = record.args
        record.args = (client, method, target.split("?", 1)[0], version, status)
        return super().format(record)


class AccessFileHandler(logging.FileHandler):
    """Open access logs owner-only, including existing files and reopenings."""

    def _open(self):
        def opener(path, flags):
            fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, 0o600)
            except OSError:
                os.close(fd)
                raise
            return fd

        return open(
            self.baseFilename, self.mode, encoding=self.encoding,
            errors=self.errors, opener=opener,
        )


_dest: str | None = None
_handle = None
_lock = threading.Lock()
_AUTO_ACTOR = object()


def configure(dest: str | None) -> None:
    """
    Called once from build_app(). dest is None/null, 'stdout', or a file path.
    Opens the file handle if needed. No-op if dest is None/null.
    """
    global _dest, _handle
    with _lock:
        if _handle is not None:
            try:
                _handle.flush()
                _handle.close()
            finally:
                _handle = None
        _dest = None

    if not dest or str(dest).strip().lower() in ("null", "none", ""):
        return

    target = str(dest).strip()
    if target == "stdout":
        _dest = "stdout"
        return
    try:
        path = Path(target).expanduser()
        path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        # The stream names every tenant and collection, so create it
        # owner/group-only instead of trusting the ambient umask.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
        handle = os.fdopen(fd, "a", encoding="utf-8", buffering=1)
        if os.fstat(fd).st_mode & 0o007:
            LOG.warning(
                "ops log %s is accessible to other users; chmod o-rwx it", path
            )
    except OSError as exc:
        LOG.warning("ops log disabled: cannot open %s (%s)", target, exc)
        return
    # Assign the handle before _dest so a concurrent emit() never sees a
    # file dest with no handle (which would silently drop the line).
    with _lock:
        _handle = handle
        _dest = str(path)


def emit(**fields) -> None:
    """
    Write one JSON line. No-op if not configured. Thread-safe:
    - stdout: single sys.stdout.write() call (atomic under GIL for small lines)
    - file: protected by a module-level threading.Lock()
    None values are dropped before serialisation.
    """
    if _dest is None:
        return
    ts = (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    payload: dict = {"ts": ts}
    payload.update({k: v for k, v in fields.items() if v is not None})
    line = json.dumps(payload, separators=(",", ":")) + "\n"
    if _dest == "stdout":
        sys.stdout.write(line)
    else:
        with _lock:
            if _handle is not None:
                _handle.write(line)


def close() -> None:
    """Flush and close the file handle if open. Called from lifespan shutdown."""
    global _handle
    with _lock:
        if _handle is not None:
            try:
                _handle.flush()
                _handle.close()
            finally:
                _handle = None


# --------------- dev stream (stderr) ---------------

class _ColorFormatter(logging.Formatter):
    """Formatter with ANSI colors for terminal output."""
    COLORS = {
        logging.DEBUG:    "\033[36m",   # cyan
        logging.INFO:     "\033[32m",   # green
        logging.WARNING:  "\033[33m",   # yellow
        logging.ERROR:    "\033[31m",   # red
        logging.CRITICAL: "\033[35m",   # magenta
    }
    RESET = "\033[0m"
    BOLD  = "\033[1m"

    def __init__(self, fmt: str, datefmt: str, use_color: bool = True):
        super().__init__(fmt, datefmt)
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        if self.use_color:
            color = self.COLORS.get(record.levelno, "")
            if record.name.startswith("pave"):
                record.name = f"{self.BOLD}pave{self.RESET}{color}"
            record.levelname = f"{color}{record.levelname}{self.RESET}"
            record.msg = f"{color}{record.msg}{self.RESET}"
        return super().format(record)


def _init_logger() -> logging.Logger:
    """
    Initializes hierarchical logging levels:
      - pave (base) → bold + colored, to stderr
      - watch namespaces (base -1 → more verbose)
      - quiet namespaces (base +1 → less verbose)
      - all others (base +2)
    Lazy import of get_cfg avoids a circular import with config.py.
    """
    from pave.config import cfg_bool, get_cfg
    cfg = get_cfg()
    base_level = getattr(logging, str(cfg.get("log.level")).upper(), logging.INFO)

    def shift(level: int, delta: int) -> int:
        return min(logging.CRITICAL, max(logging.DEBUG, level + 10 * delta))

    def namespaces(value: Any) -> list[str]:
        if not value:
            return []
        if isinstance(value, str):
            return [value]
        return [str(item) for item in value if str(item)]

    root = logging.getLogger()
    root.setLevel(shift(base_level, +2))
    root.handlers.clear()

    use_color = hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(_ColorFormatter(
        "%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        "%H:%M:%S",
        use_color=use_color,
    ))
    root.addHandler(handler)

    pave_log = logging.getLogger("pave")
    # cfg_bool, not truthiness: PAVEDB_DEV=no reaches the overlay as the
    # string "no", which is true to Python but false to the operator.
    dev_forces_debug = (
        cfg_bool(cfg.get("dev")) and "PAVEDB_LOG__LEVEL" not in os.environ
    )
    pave_log.setLevel(logging.DEBUG if dev_forces_debug else base_level)

    for ns in namespaces(cfg.get("log.debug")):
        logging.getLogger(ns).setLevel(logging.DEBUG)

    for ns in namespaces(cfg.get("log.watch")):
        logging.getLogger(ns).setLevel(shift(base_level, -1))

    for ns in namespaces(cfg.get("log.quiet")):
        logging.getLogger(ns).setLevel(shift(base_level, +1))

    return pave_log


_LOGGER_SINGLETON = _init_logger()


def get_logger() -> logging.Logger:
    """Returns the global PaveDB logger."""
    return _LOGGER_SINGLETON


LOG = _LOGGER_SINGLETON

# --------------- ops stream ---------------

def _result_status(result: Any) -> tuple[str, str | None]:
    """Return (status, error_code) from a handler return value."""
    sc = getattr(result, "status_code", None)
    if sc is not None:
        if sc >= 400:
            try:
                code = json.loads(result.body).get("code")
            except Exception:
                code = None
            return "error", code
        return "ok", None
    if isinstance(result, dict):
        if not result.get("ok", True):
            return "error", result.get("code")
        return "ok", None
    return "ok", None


def ops_event(
    op: str,
    *,
    tenant: Any = "tenant",
    coll: Any = "name",
    actor: Any = _AUTO_ACTOR,
    **extra_keys,
):
    """
    Route decorator: times the call and emits one ops_log line.
    Works for both sync and async handlers.

    Parameters
    ----------
    op:
        Operation name emitted in the ``op`` field (e.g. ``"search"``).
    tenant:
        Source for the tenant field.
        - str value  → resolved as ``kwargs[value]``
        - callable   → called as ``fn(kwargs, result)`` after the handler returns
        - ``None``   → omit the ``tenant`` field
    coll:
        Source for the collection field.
        - str value  → resolved as ``kwargs[value]``
        - callable   → called as ``fn(kwargs, result)`` after the handler returns
        - ``None``   → omit the ``collection`` field (e.g. for list_collections).
    actor:
        Source for the actor field.
        - omitted    → auto-resolve from ``kwargs["ctx"]`` when present
        - str value  → resolved as ``kwargs[value]``
        - callable   → called as ``fn(kwargs, result)`` after the handler returns
        - ``None``   → omit the ``actor`` field
    **extra_keys:
        Additional event fields.
        - str value  → resolved as ``kwargs[value]``
        - callable   → called as ``fn(kwargs, result)`` after the handler returns
    """
    def _resolve(src: Any, kwargs: dict, result: Any) -> Any:
        if src is None:
            return None
        if callable(src):
            return src(kwargs, result)
        return kwargs.get(src)

    def _resolve_actor(src: Any, kwargs: dict, result: Any) -> Any:
        if src is _AUTO_ACTOR:
            ctx = kwargs.get("ctx")
            if ctx is None:
                return None
            try:
                from pave.auth import actor_of
                return actor_of(ctx)
            except Exception:
                return None
        return _resolve(src, kwargs, result)

    def _extras(kwargs: dict, result: Any) -> dict:
        out: dict = {}
        for field, src in extra_keys.items():
            try:
                out[field] = _resolve(src, kwargs, result)
            except Exception:
                out[field] = None
        return out

    def decorator(fn):
        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrapper(*args, **kwargs):
                _t0 = time.perf_counter()
                _s, _c = "error", None
                result = None
                try:
                    result = await fn(*args, **kwargs)
                    _s, _c = _result_status(result)
                    return result
                finally:
                    emit(
                        op=op,
                        tenant=_resolve(tenant, kwargs, result),
                        collection=_resolve(coll, kwargs, result),
                        actor=_resolve_actor(actor, kwargs, result),
                        latency_ms=round((time.perf_counter() - _t0) * 1000, 2),
                        status=_s, error_code=_c,
                        **_extras(kwargs, result),
                    )
            return awrapper
        else:
            @functools.wraps(fn)
            def swrapper(*args, **kwargs):
                _t0 = time.perf_counter()
                _s, _c = "error", None
                result = None
                try:
                    result = fn(*args, **kwargs)
                    _s, _c = _result_status(result)
                    return result
                finally:
                    emit(
                        op=op,
                        tenant=_resolve(tenant, kwargs, result),
                        collection=_resolve(coll, kwargs, result),
                        actor=_resolve_actor(actor, kwargs, result),
                        latency_ms=round((time.perf_counter() - _t0) * 1000, 2),
                        status=_s, error_code=_c,
                        **_extras(kwargs, result),
                    )
            return swrapper
    return decorator
