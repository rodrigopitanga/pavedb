# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import asyncio, functools, json, logging, sys, threading, time
from datetime import datetime, timezone
from typing import Any

_dest: str | None = None
_handle = None
_lock = threading.Lock()


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

    _dest = str(dest).strip()
    if _dest != "stdout":
        with _lock:
            _handle = open(_dest, "a", encoding="utf-8", buffering=1)


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
    from pave.config import get_cfg
    cfg = get_cfg()
    base_level = getattr(logging, str(cfg.get("log.level")).upper(), logging.INFO)

    def shift(level: int, delta: int) -> int:
        return min(logging.CRITICAL, max(logging.DEBUG, level + 10 * delta))

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
    pave_log.setLevel(logging.DEBUG if cfg.get("dev", 0) else base_level)

    for ns in cfg.get("log.debug", []):
        logging.getLogger(ns).setLevel(logging.DEBUG)

    for ns in cfg.get("log.watch", []):
        logging.getLogger(ns).setLevel(shift(base_level, -1))

    for ns in cfg.get("log.quiet", ["uvicorn", "uvicorn.access", "uvicorn.error",
                                     "fastapi", "sqlalchemy", "urllib3", "httpx"]):
        logging.getLogger(ns).setLevel(shift(base_level, +1))

    return pave_log


_LOGGER_SINGLETON = _init_logger()


def get_logger() -> logging.Logger:
    """Returns the global PatchVec logger."""
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
                        latency_ms=round((time.perf_counter() - _t0) * 1000, 2),
                        status=_s, error_code=_c,
                        **_extras(kwargs, result),
                    )
            return swrapper
    return decorator
