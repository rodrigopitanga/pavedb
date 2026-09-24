# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import threading

from fastapi import Request

from pave.auth import ConcurrencySlot
from pave.config import get_cfg, get_logger
from pave.service import ServiceError
from pave.stores.base import BaseStore

log = get_logger()


def current_store(request: Request) -> BaseStore:
    return request.app.state.store


_REINDEX_STATUS = {
    "not_found": 404,
    "conflict": 409,
    "invalid": 400,
    "unavailable": 503,
    "failed": 500,
}


def reindex_response(result, request: Request, rid, *, error, trace):
    """Map a reindex service result to the shared status/envelope."""
    if result.get("ok"):
        return trace(result, request, request_id=rid)
    return error(
        _REINDEX_STATUS.get(result.get("error_type", "failed"), 500),
        result.get("code", "reindex_failed"),
        result.get("error", "reindex operation failed"),
        request=request,
        request_id=rid,
    )


def archive_restore_error(exc: Exception, request: Request, rid, *, error):
    """Map a collection-archive restore failure to the shared envelope."""
    if isinstance(exc, ServiceError):
        return error(400, exc.code, exc.message, request=request, request_id=rid)
    if isinstance(exc, ValueError):
        return error(
            400, "collection_archive_invalid", str(exc),
            request=request, request_id=rid,
        )
    log.warning(
        "collection archive restore failed request_id=%s", rid, exc_info=exc
    )
    return error(
        500,
        "collection_archive_restore_failed",
        "failed to restore collection archive",
        request=request,
        request_id=rid,
    )


_op_gate_bootstrap = threading.Lock()


def operation_gate(request: Request, tenant: str, op: str):
    """Take one instance slot and one per-tenant slot for an expensive op.

    The `<op>.max_concurrent` / `<op>.max_concurrent_per_tenant` pattern,
    generalized from the ingest gate: both counters move under one lock
    because a detached worker thread releases them from the pool, not the
    event loop. Returns ``(slot, None)`` when admitted or ``(None,
    (source, active, limit))`` when a bound is reached; 0 disables a
    bound.
    """
    state = request.app.state
    with _op_gate_bootstrap:
        gates = getattr(state, "op_gates", None)
        if gates is None:
            gates = {}
            state.op_gates = gates
        gate = gates.get(op)
        if gate is None:
            gate = {
                "lock": threading.Lock(),
                "active": 0,
                "per_tenant": {},
            }
            gates[op] = gate

    cfg = get_cfg()
    max_i = int(cfg.get(f"{op}.max_concurrent") or 0)
    max_t = int(cfg.get(f"{op}.max_concurrent_per_tenant") or 0)
    lock = gate["lock"]
    per = gate["per_tenant"]
    with lock:
        active_i = gate["active"]
        active_t = per.get(tenant, 0)
        if max_i > 0 and active_i >= max_i:
            return None, (f"instance-scoped {op} cap", active_i, max_i)
        if max_t > 0 and active_t >= max_t:
            return None, (f"tenant-scoped {op} cap", active_t, max_t)
        gate["active"] = active_i + 1
        per[tenant] = active_t + 1

    def _decrement() -> None:
        gate["active"] -= 1
        per[tenant] = max(0, per.get(tenant, 0) - 1)

    return ConcurrencySlot(lock, _decrement), None
