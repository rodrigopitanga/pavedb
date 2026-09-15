# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Service layer.

Sits between the transport layers (HTTP routes, CLI, library
client) and `BaseStore`. Owns validation, embedder-config
resolution, file-format dispatch on ingest, error envelope shape,
and the structured-ops logging decorator hooks. Never touches
embedders, vector backends, or SQLite directly — everything goes
through the store.

Return contract: each public function returns a `ServiceResult`
dict carrying `ok: bool` plus operation-specific fields. Hard
errors raise `ServiceError(code, message)`; the route layer maps
both into the HTTP response envelope.
"""

from __future__ import annotations

import os
import re
import unicodedata
import uuid
from datetime import datetime, timezone as tz
from pathlib import Path
from collections.abc import Iterable, Mapping
from typing import Any

import time as _time
from pave.config import get_cfg, get_logger
from pave.embedders import EmbedderUnavailableError
from pave.metadb import (
    ChunkLimitError,
    CollectionLimitError,
    ReindexJobConflictError,
    ReindexJobStateError,
)
from pave.embedders.base import EmbedderConfig
from pave.embedders.factory import (
    embedder_instances,
    embedder_space_key,
    resolve_default_embedder,
    resolve_embedder_spec,
)
from pave.metrics import (
    inc as m_inc,
    inc_labeled as m_inc_labeled,
    timed as m_timed,
    record_latency as m_record_latency,
)
from pave.preprocess import (
    CsvOptions,
    PreprocessError,
    preprocess,
    preprocess_text,
)
from pave.schemas import MAX_SEARCH_K
from pave.stores.base import (
    BaseStore,
    FilterSpec,
    Metadata,
    MetadataValidationError,
    QueryLogEntry,
)

log = get_logger()

ServiceResult = dict[str, Any]


def _query_log_enabled() -> bool:
    return bool(get_cfg().get("query_log.enabled", True))


# Pure-ish service functions operating on a store adapter
class ServiceError(RuntimeError):
    """Hard failure inside a service operation.

    Carries a machine-readable `code` (mapped to HTTP status in the
    route layer) and a human-readable `message`. Raised when an
    operation cannot complete; soft failures are reported through
    the `ServiceResult` envelope's `ok=False` path instead.
    """
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


_MAX_DISPLAY_NAME_LEN = 120
_MAX_DOCID_LEN = 200
_MAX_DOCID_BYTES = 200
_COLLECTION_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def _has_control_chars(value: str) -> bool:
    for ch in value:
        cat = unicodedata.category(ch)
        if cat.startswith("C"):
            return True
    return False


def _normalize_display_name(value: str | None, fallback: str) -> str:
    display_name = (value if value is not None else fallback).strip()
    if not display_name:
        raise ServiceError(
            "invalid_display_name",
            "display_name must not be empty",
        )
    if len(display_name) > _MAX_DISPLAY_NAME_LEN:
        raise ServiceError(
            "invalid_display_name",
            f"display_name must be at most {_MAX_DISPLAY_NAME_LEN} characters",
        )
    if _has_control_chars(display_name):
        raise ServiceError(
            "invalid_display_name",
            "display_name must not contain control characters",
        )
    return display_name


def _validate_collection_name(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ServiceError(
            "invalid_collection_name",
            "collection name is required",
        )
    if not _COLLECTION_NAME_RE.fullmatch(raw):
        raise ServiceError(
            "invalid_collection_name",
            "collection name must match ^[a-z0-9][a-z0-9-]{0,62}$",
        )
    return raw


def _validate_docid(value: str) -> str:
    # The docid becomes part of a chunk sidecar filename, so a control
    # character (NUL in particular) would fail mid-write and leave metadata
    # rows without vectors.
    if _has_control_chars(value):
        raise ServiceError(
            "invalid_docid",
            "docid must not contain control characters",
        )
    if len(value) > _MAX_DOCID_LEN:
        raise ServiceError(
            "invalid_docid",
            f"docid must be at most {_MAX_DOCID_LEN} characters",
        )
    if len(value.encode("utf-8")) > _MAX_DOCID_BYTES:
        raise ServiceError(
            "invalid_docid",
            f"docid must be at most {_MAX_DOCID_BYTES} UTF-8 bytes",
        )
    return value


def _validate_metadata(value: Metadata | None) -> Metadata:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ServiceError("invalid_metadata", "metadata must be an object")
    return value


def create_collection(
    store,
    tenant: str,
    name: str | None,
    *,
    display_name: str | None = None,
    embedder: str | None = None,
    embedder_type: str | None = None,
    embed_model: str | None = None,
    embedder_config: EmbedderConfig | None = None,
) -> ServiceResult:
    try:
        resolved_name = _validate_collection_name(name)
        resolved_display_name = _normalize_display_name(
            display_name,
            resolved_name,
        )
        create_options: dict[str, Any] = {
            "embedder_type": embedder_type,
            "embed_model": embed_model,
            "embedder_config": embedder_config,
            "display_name": resolved_display_name,
        }
        if embedder is not None:
            create_options["embedder"] = embedder
        created = store.create_collection(
            tenant,
            resolved_name,
            **create_options,
        )
        if isinstance(created, dict):
            resolved_type = str(created.get("embedder_type") or "")
            resolved_model = str(created.get("embed_model") or "")
            resolved_config = dict(created.get("embedder_config") or {})
            returned_display = str(
                created.get("display_name") or resolved_display_name
            )
        else:
            resolved = resolve_embedder_spec(
                get_cfg(),
                selector=embedder,
                embedder_type=embedder_type,
                model=embed_model,
                embedder_config=embedder_config,
            )
            resolved_type = str(resolved["embedder_type"])
            resolved_model = str(resolved["embed_model"])
            resolved_config = dict(resolved.get("embedder_config") or {})
            returned_display = resolved_display_name
        m_inc("collections_created_total", 1.0)
        return {
            "ok": True,
            "tenant": tenant,
            "name": resolved_name,
            "collection": resolved_name,
            "display_name": returned_display,
            "embedder_type": resolved_type,
            "embed_model": resolved_model,
            "embedder_config": resolved_config,
        }
    except ServiceError as e:
        log.info(
            "create_collection rejected tenant=%s coll=%s: %s",
            tenant, name, e.message,
        )
        return {
            "ok": False,
            "code": e.code,
            "error": e.message,
            "error_type": "invalid",
        }
    except ValueError as e:
        log.info(
            "create_collection invalid tenant=%s coll=%s: %s",
            tenant, name, e,
        )
        return {
            "ok": False,
            "code": "invalid_embedder_config",
            "error": str(e),
            "error_type": "invalid",
        }
    except EmbedderUnavailableError as e:
        log.warning(
            "create_collection embedder_unavailable tenant=%s coll=%s: %s",
            tenant, name, e,
        )
        return {
            "ok": False,
            "code": "embedder_unavailable",
            "error": str(e),
            "error_type": "unavailable",
        }
    except CollectionLimitError as e:
        log.warning(
            "create_collection limit tenant=%s coll=%s: %s",
            tenant, name, e,
        )
        return {
            "ok": False,
            "code": "collection_limit_reached",
            "error": str(e),
            "error_type": "limit",
        }
    except Exception as e:
        log.warning(
            "create_collection failed tenant=%s coll=%s: %s",
            tenant, name, e,
        )
        return {
            "ok": False,
            "code": "create_collection_failed",
            "error": str(e),
            "error_type": "failed",
        }


def update_collection(
    store,
    tenant: str,
    name: str,
    *,
    display_name: str | None = None,
) -> ServiceResult:
    try:
        resolved_name = _validate_collection_name(name)
        if display_name is None:
            raise ServiceError(
                "invalid_display_name",
                "display_name is required",
            )
        resolved_display_name = _normalize_display_name(display_name, name)
        if store.get_collection_config(tenant, resolved_name) is None:
            return {
                "ok": False,
                "code": "collection_not_found",
                "error": f"collection '{resolved_name}' does not exist",
                "error_type": "not_found",
            }
        if store.update_collection(
            tenant,
            resolved_name,
            display_name=resolved_display_name,
        ) is None:
            return {
                "ok": False,
                "code": "collection_not_found",
                "error": f"collection '{resolved_name}' does not exist",
                "error_type": "not_found",
            }
        return {
            "ok": True,
            "tenant": tenant,
            "name": resolved_name,
            "display_name": resolved_display_name,
        }
    except ServiceError as e:
        log.info(
            "update_collection rejected tenant=%s coll=%s: %s",
            tenant, name, e.message,
        )
        return {
            "ok": False,
            "code": e.code,
            "error": e.message,
            "error_type": "invalid",
        }
    except ValueError as e:
        return {
            "ok": False,
            "code": "update_collection_invalid",
            "error": str(e),
            "error_type": "invalid",
        }
    except Exception as e:
        log.warning(
            "update_collection failed tenant=%s coll=%s: %s",
            tenant, name, e,
        )
        return {
            "ok": False,
            "code": "update_collection_failed",
            "error": str(e),
            "error_type": "failed",
        }


def start_collection_reindex(
    store,
    tenant: str,
    collection: str,
    *,
    embedder_type: str | None = None,
    embed_model: str | None = None,
    embedder_config: EmbedderConfig | None = None,
) -> ServiceResult:
    """Launch an admin reindex job against a new embedder vector space."""
    try:
        resolved_collection = _validate_collection_name(collection)
        job = store.start_reindex(
            tenant,
            resolved_collection,
            embedder_type=embedder_type,
            embed_model=embed_model,
            embedder_config=embedder_config,
        )
        return {"ok": True, **job}
    except ServiceError as exc:
        return {
            "ok": False,
            "code": exc.code,
            "error": exc.message,
            "error_type": "invalid",
        }
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "code": "collection_not_found",
            "error": str(exc),
            "error_type": "not_found",
        }
    except ReindexJobConflictError as exc:
        return {
            "ok": False,
            "code": "reindex_conflict",
            "error": str(exc),
            "error_type": "conflict",
        }
    except EmbedderUnavailableError as exc:
        return {
            "ok": False,
            "code": "embedder_unavailable",
            "error": str(exc),
            "error_type": "unavailable",
        }
    except ValueError as exc:
        return {
            "ok": False,
            "code": "reindex_invalid",
            "error": str(exc),
            "error_type": "invalid",
        }
    except Exception as exc:
        log.warning(
            f"start_collection_reindex failed tenant={tenant} "
            f"coll={collection}: {exc}"
        )
        return {
            "ok": False,
            "code": "reindex_failed",
            "error": str(exc),
            "error_type": "failed",
        }


def _reindex_job_action(store, job_id: str, action: str) -> ServiceResult:
    try:
        operation = getattr(store, action)
        return {"ok": True, **operation(job_id)}
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "code": "reindex_job_not_found",
            "error": str(exc),
            "error_type": "not_found",
        }
    except (ReindexJobStateError, ValueError) as exc:
        return {
            "ok": False,
            "code": "reindex_state_conflict",
            "error": str(exc),
            "error_type": "conflict",
        }
    except Exception as exc:
        log.warning(
            f"reindex job action failed job={job_id} action={action}: {exc}"
        )
        return {
            "ok": False,
            "code": "reindex_failed",
            "error": str(exc),
            "error_type": "failed",
        }


def get_reindex_job(store, job_id: str) -> ServiceResult:
    return _reindex_job_action(store, job_id, "get_reindex_job")


def pause_reindex_job(store, job_id: str) -> ServiceResult:
    return _reindex_job_action(store, job_id, "pause_reindex")


def resume_reindex_job(store, job_id: str) -> ServiceResult:
    return _reindex_job_action(store, job_id, "resume_reindex")


def cancel_reindex_job(store, job_id: str) -> ServiceResult:
    return _reindex_job_action(store, job_id, "cancel_reindex")


def _sum_timing(*timings: dict[str, float]) -> dict[str, float]:
    keys = ("embed_ms", "search_ms", "filter_ms", "hydrate_ms")
    return {
        key: round(sum(timing.get(key, 0.0) for timing in timings), 2)
        for key in keys
    }


def _embedder_key_from_config(
    cfg: Mapping[str, Any] | None,
) -> tuple[str, str, str] | None:
    if not cfg:
        return None
    return embedder_space_key(cfg)


def _require_compatible_embeddings(
    store,
    tenant: str,
    collection: str,
    common_tenant: str,
    common_collection: str,
) -> None:
    get_config = getattr(store, "get_collection_config", None)
    if get_config is None:
        return
    local_key = _embedder_key_from_config(get_config(tenant, collection))
    common_key = _embedder_key_from_config(
        get_config(common_tenant, common_collection)
    )
    if local_key and common_key and local_key != common_key:
        raise ServiceError(
            "incompatible_embedder_config",
            "cannot merge search results from collections with different "
            "embedder configs",
        )

def _coll_cfg(store, tenant: str, collection: str):
    fn = getattr(store, "get_collection_config", None)
    return fn(tenant, collection) if fn else None


def _is_identity_collection(store, tenant: str, collection: str) -> bool:
    cfg = _coll_cfg(store, tenant, collection)
    return cfg is not None and str(cfg.get("embedder_type") or "").lower() == "identity"


def _get_collection_vector_dim(store, tenant: str, collection: str) -> int | None:
    get_dim = getattr(store, "get_collection_vector_dim", None)
    if callable(get_dim):
        resolved = get_dim(tenant, collection)
        if resolved is not None:
            return int(resolved)
    cfg = _coll_cfg(store, tenant, collection)
    if not cfg:
        return None
    raw = (cfg.get("embedder_config") or {}).get("dim")
    return int(raw) if raw is not None else None


def delete_collection(store, tenant: str, name: str) -> ServiceResult:
    try:
        resolved_name = _validate_collection_name(name)
        if store.get_collection_config(tenant, resolved_name) is None:
            return {
                "ok": False,
                "code": "collection_not_found",
                "error": f"collection '{resolved_name}' does not exist",
                "error_type": "not_found",
            }
        store.delete_collection(tenant, resolved_name)
        m_inc("collections_deleted_total", 1.0)
        return {
            "ok": True,
            "tenant": tenant,
            "deleted": resolved_name,
        }
    except ServiceError as e:
        return {
            "ok": False,
            "code": e.code,
            "error": e.message,
            "error_type": "invalid",
        }
    except Exception as e:
        log.warning(
            "delete_collection failed tenant=%s coll=%s: %s",
            tenant, name, e,
        )
        return {
            "ok": False,
            "code": "delete_collection_failed",
            "error": str(e),
        }

def move_collection(store, tenant: str,
                    old_name: str, new_name: str) -> ServiceResult:
    try:
        resolved_old = _validate_collection_name(old_name)
        resolved_new = _validate_collection_name(new_name)
        if resolved_old == resolved_new:
            log.info(
                "move_collection rejected tenant=%s: "
                "same name %s", tenant, resolved_old,
            )
            return {
                "ok": False,
                "code": "move_invalid",
                "error": "old and new names are the same",
                "error_type": "invalid",
            }
        store.move_collection(tenant, resolved_old, resolved_new)
        m_inc("collections_moved_total", 1.0)
        return {
            "ok": True,
            "tenant": tenant,
            "old_name": resolved_old,
            "new_name": resolved_new,
        }
    except ServiceError as e:
        return {
            "ok": False,
            "code": e.code,
            "error": e.message,
            "error_type": "invalid",
        }
    except ValueError as e:
        err = str(e)
        if "does not exist" in err:
            log.info(
                "move_collection not_found tenant=%s "
                "old=%s: %s", tenant, old_name, err,
            )
            return {
                "ok": False,
                "code": "collection_not_found",
                "error": err,
                "error_type": "not_found",
            }
        if "already exists" in err:
            log.info(
                "move_collection conflict tenant=%s "
                "new=%s: %s", tenant, new_name, err,
            )
            return {
                "ok": False,
                "code": "collection_conflict",
                "error": err,
                "error_type": "conflict",
            }
        log.info(
            "move_collection invalid tenant=%s "
            "old=%s new=%s: %s",
            tenant, old_name, new_name, err,
        )
        return {
            "ok": False,
            "code": "move_invalid",
            "error": err,
            "error_type": "invalid",
        }
    except Exception as e:
        log.warning(
            "move_collection failed tenant=%s "
            "old=%s new=%s: %s",
            tenant, old_name, new_name, e,
        )
        return {
            "ok": False,
            "code": "move_collection_failed",
            "error": str(e),
            "error_type": "failed",
        }

def delete_document(store, tenant: str, collection: str, docid: str) -> ServiceResult:
    try:
        _validate_collection_name(collection)
        purged = store.purge_doc(tenant, collection, docid)
        if purged:
            m_inc("purge_total", float(purged))
            m_inc("documents_deleted_total", 1.0)
        return {
            "ok": True,
            "tenant": tenant,
            "collection": collection,
            "docid": docid,
            "chunks_deleted": purged,
        }
    except ServiceError as e:
        log.info(
            "delete_document rejected tenant=%s coll=%s docid=%s: %s",
            tenant, collection, docid, e.message,
        )
        return {"ok": False, "code": e.code, "error": e.message}
    except Exception as e:
        log.warning(
            "delete_document failed tenant=%s coll=%s "
            "docid=%s: %s", tenant, collection, docid, e,
            exc_info=True,
        )
        return {
            "ok": False,
            "code": "delete_document_failed",
            "error": str(e),
        }


def get_document(
    store,
    tenant: str,
    collection: str,
    docid: str,
) -> ServiceResult:
    try:
        doc = store.get_document(tenant, collection, docid)
        if doc is None:
            log.info(
                "get_document not_found tenant=%s coll=%s docid=%s",
                tenant, collection, docid,
            )
            return {
                "ok": False,
                "code": "document_not_found",
                "error": f"document '{docid}' not found",
                "error_type": "not_found",
            }
        return {
            "ok": True,
            "tenant": tenant,
            "collection": collection,
            **doc,
        }
    except Exception as e:
        log.warning(
            "get_document failed tenant=%s coll=%s docid=%s: %s",
            tenant, collection, docid, e,
        )
        return {
            "ok": False,
            "code": "get_document_failed",
            "error": str(e),
            "error_type": "failed",
        }


def list_chunks(
    store,
    tenant: str,
    collection: str,
    docid: str,
) -> ServiceResult:
    try:
        chunks = store.list_chunks(tenant, collection, docid)
        return {
            "ok": True,
            "tenant": tenant,
            "collection": collection,
            "docid": docid,
            "chunks": chunks,
            "count": len(chunks),
        }
    except Exception as e:
        log.warning(
            "list_chunks failed tenant=%s coll=%s docid=%s: %s",
            tenant, collection, docid, e,
        )
        return {
            "ok": False,
            "code": "list_chunks_failed",
            "error": str(e),
        }


def get_chunk(
    store,
    tenant: str,
    collection: str,
    rid: str,
) -> ServiceResult:
    try:
        chunk = store.get_chunk(tenant, collection, rid)
        if chunk is None:
            log.info(
                "get_chunk not_found tenant=%s coll=%s rid=%s",
                tenant, collection, rid,
            )
            return {
                "ok": False,
                "code": "chunk_not_found",
                "error": f"chunk '{rid}' not found",
                "error_type": "not_found",
            }
        return {"ok": True, **chunk}
    except Exception as e:
        log.warning(
            "get_chunk failed tenant=%s coll=%s rid=%s: %s",
            tenant, collection, rid, e,
        )
        return {
            "ok": False,
            "code": "get_chunk_failed",
            "error": str(e),
            "error_type": "failed",
        }


def get_chunk_content(
    store,
    tenant: str,
    collection: str,
    rid: str,
) -> ServiceResult:
    try:
        content = store.get_chunk_content(tenant, collection, rid)
        if content is None:
            log.info(
                "get_chunk_content not_found tenant=%s coll=%s rid=%s",
                tenant, collection, rid,
            )
            return {
                "ok": False,
                "code": "chunk_content_not_found",
                "error": f"chunk '{rid}' content not found",
                "error_type": "not_found",
            }
        return {"ok": True, **content}
    except Exception as e:
        log.warning(
            "get_chunk_content failed tenant=%s coll=%s rid=%s: %s",
            tenant, collection, rid, e,
        )
        return {
            "ok": False,
            "code": "get_chunk_content_failed",
            "error": str(e),
            "error_type": "failed",
        }


def list_documents(
    store,
    tenant: str,
    collection: str,
) -> ServiceResult:
    try:
        docs = store.list_documents(tenant, collection)
        return {
            "ok": True,
            "tenant": tenant,
            "collection": collection,
            "documents": docs,
            "count": len(docs),
        }
    except Exception as e:
        log.warning(
            "list_documents failed tenant=%s coll=%s: %s",
            tenant, collection, e,
        )
        return {
            "ok": False,
            "code": "list_documents_failed",
            "error": str(e),
        }


def _default_docid(filename: str) -> str:
    base = Path(filename).name.upper()
    # replace space and dot with underscore
    base = base.replace(" ", "_").replace(".", "_")
    # replace all non A-Z0-9_ with underscore
    base = re.sub(r"[^A-Z0-9_]", "_", base)
    # collapse multiple underscores
    base = re.sub(r"_+", "_", base).strip("_")
    if base != '': return base
    return "PVDOC_"+str(uuid.uuid4())


def _ingest_size_limit_bytes() -> int:
    raw = get_cfg().get("ingest.max_file_size_mb")
    try:
        max_mb = float(raw or 0)
    except (TypeError, ValueError):
        max_mb = 0.0
    if max_mb <= 0:
        return 0
    return int(max_mb * 1024 * 1024)


def _enforce_ingest_size_limit(size_bytes: int) -> None:
    limit_bytes = _ingest_size_limit_bytes()
    if limit_bytes > 0 and size_bytes > limit_bytes:
        raise ServiceError(
            "file_too_large",
            f"file exceeds the {int(limit_bytes / (1024 * 1024))} MB limit",
        )


def _ingest_batch_size_limit_bytes() -> int:
    raw = get_cfg().get("ingest.max_batch_size_mb")
    if raw is None:
        # Unset means "one batch may carry one document's worth of text".
        return _ingest_size_limit_bytes()
    try:
        max_mb = float(raw)
    except (TypeError, ValueError):
        return _ingest_size_limit_bytes()
    if max_mb <= 0:
        return 0
    return int(max_mb * 1024 * 1024)


def _enforce_batch_size_limit(total_bytes: int) -> None:
    limit_bytes = _ingest_batch_size_limit_bytes()
    if limit_bytes > 0 and total_bytes > limit_bytes:
        raise ServiceError(
            "file_too_large",
            f"batch exceeds the {int(limit_bytes / (1024 * 1024))} MB limit",
        )


def ingest_upload_limit_bytes() -> int:
    """Largest document a file upload may carry, in bytes; 0 is unlimited.

    A multipart upload holds exactly one document, so the HTTP layer sizes
    its pre-auth guard from this and the wire cap cannot drift away from the
    ingest limit it is protecting.
    """
    return _ingest_size_limit_bytes()


def _index_records_result(
    store,
    tenant: str,
    collection: str,
    baseid: str,
    records,
    doc_meta,
    t0: float,
    label: str,
    *,
    vectors=None,
) -> ServiceResult:
    """Shared tail for ingest paths: index records, metrics, log, envelope."""
    extra = {"vectors": vectors} if vectors is not None else {}
    result = store.index_records(
        tenant,
        collection,
        baseid,
        records,
        doc_meta,
        **extra,
    )
    if result.purged_chunks:
        m_inc("purge_total", float(result.purged_chunks))
    m_inc("documents_indexed_total", 1.0)
    m_inc("chunks_indexed_total", float(result.indexed_chunks))
    latency_ms = round((_time.perf_counter() - t0) * 1000, 2)
    log.info(
        f"{label} tenant={tenant} coll={collection} "
        f"docid={baseid} indexed_chunks={result.indexed_chunks} "
        f"ms={latency_ms:.2f}"
    )
    return {
        "ok": True,
        "tenant": tenant,
        "collection": collection,
        "docid": baseid,
        "chunks": result.indexed_chunks,
    }


def ingest_document(store, tenant: str, collection: str, filename: str, content: bytes,
                    docid: str | None, metadata: Metadata | None,
                    csv_options: CsvOptions | None = None) -> ServiceResult:
    # Ingest auto-creates the collection, so validate the name here too —
    # otherwise it mints names that create/delete reject, i.e. undeletable
    # orphans (delete also format-validates before its existence check).
    collection = _validate_collection_name(collection)
    _t0 = _time.perf_counter()
    with m_timed("ingest"):
        try:
            stage = "prepare"
            _enforce_ingest_size_limit(len(content))
            baseid = _validate_docid(docid or _default_docid(filename))
            meta_from_call = _validate_metadata(metadata)
            now = datetime.now(tz.utc).isoformat(timespec="seconds")
            now = now.replace("+00:00", "Z")
            doc_meta = {
                "docid": baseid, "filename": filename,
                "ingested_at": now, **meta_from_call,
            }
            records = []
            for local_id, text, extra in preprocess(
                filename, content, csv_options=csv_options
            ):
                rid = f"{baseid}::{local_id}"
                records.append((rid, text, extra))
            if not records:
                log.info(
                    "ingest no_text_extracted tenant=%s "
                    "coll=%s file=%s",
                    tenant, collection, filename,
                )
                return {
                    "ok": False,
                    "code": "no_text_extracted",
                    "error": "no text extracted",
                }
            stage = "index_records"
            return _index_records_result(
                store, tenant, collection, baseid, records, doc_meta, _t0, "ingest"
            )
        except ServiceError:
            raise
        except MetadataValidationError as exc:
            raise ServiceError("invalid_metadata_keys", str(exc)) from exc
        except ChunkLimitError as exc:
            raise ServiceError("chunk_limit_reached", str(exc)) from exc
        except CollectionLimitError as exc:
            # Ingest into an unknown collection creates it, so it can now hit
            # the per-tenant cap (B33). Report the quota, not a generic 500.
            raise ServiceError("collection_limit_reached", str(exc)) from exc
        except PreprocessError as exc:
            raise ServiceError("unsupported_or_invalid_document", str(exc)) from exc
        except ValueError as exc:
            raise ServiceError("invalid_csv_options", str(exc)) from exc
        except EmbedderUnavailableError as exc:
            log.warning(
                "ingest embedder_unavailable stage=%s tenant=%s coll=%s docid=%s: %s",
                stage,
                tenant, collection,
                docid or filename, exc,
            )
            return {
                "ok": False,
                "code": "embedder_unavailable",
                "error": str(exc),
            }
        except Exception as e:
            log.warning(
                "ingest failed stage=%s tenant=%s coll=%s docid=%s: %s",
                stage,
                tenant, collection,
                docid or filename, e,
                exc_info=True,
            )
            return {
                "ok": False,
                "code": "ingest_failed",
                "error": str(e),
            }


def ingest_text(
    store,
    tenant: str,
    collection: str,
    text: str | None,
    docid: str | None,
    metadata: Metadata | None,
    *,
    vector: list[float] | None = None,
) -> ServiceResult:
    collection = _validate_collection_name(collection)
    _t0 = _time.perf_counter()
    with m_timed("ingest"):
        try:
            if (
                _is_identity_collection(store, tenant, collection)
                and text is not None
            ):
                raise ServiceError("vectors_only", "collection accepts vectors only")
            if vector is not None:
                expected_dim = _get_collection_vector_dim(
                    store,
                    tenant,
                    collection,
                )
                if expected_dim is not None and len(vector) != expected_dim:
                    raise ServiceError(
                        "dim_mismatch",
                        f"vector length {len(vector)} does not match "
                        f"collection dim {expected_dim}",
                    )
            if text is None:
                baseid = _validate_docid(docid or _default_docid(""))
                meta_from_call = _validate_metadata(metadata)
                now = datetime.now(tz.utc).isoformat(timespec="seconds")
                now = now.replace("+00:00", "Z")
                doc_meta = {
                    "docid": baseid,
                    "ingested_at": now,
                    **meta_from_call,
                }
                # 1 vector = 1 record, rid namespaced under docid; store's
                # vector branch skips embed + chunk-text sidecar.
                records = [(f"{baseid}::0", "", {})]
                return _index_records_result(
                    store,
                    tenant,
                    collection,
                    baseid,
                    records,
                    doc_meta,
                    _t0,
                    "ingest_text",
                    vectors=[vector],
                )
            _enforce_ingest_size_limit(len(text.encode("utf-8")))
            baseid = _validate_docid(docid or _default_docid(""))
            meta_from_call = _validate_metadata(metadata)
            now = datetime.now(tz.utc).isoformat(timespec="seconds")
            now = now.replace("+00:00", "Z")
            doc_meta = {
                "docid": baseid,
                "ingested_at": now,
                **meta_from_call,
            }
            records = [
                (f"{baseid}::{local_id}", chunk, extra)
                for local_id, chunk, extra in preprocess_text(text)
            ]
            if not records:
                log.info(
                    "ingest_text no_text_extracted tenant=%s "
                    "coll=%s docid=%s",
                    tenant, collection, baseid,
                )
                return {
                    "ok": False,
                    "code": "no_text_extracted",
                    "error": "no text extracted",
                }
            return _index_records_result(
                store,
                tenant,
                collection,
                baseid,
                records,
                doc_meta,
                _t0,
                "ingest_text",
            )
        except ServiceError:
            raise
        except MetadataValidationError as exc:
            raise ServiceError("invalid_metadata_keys", str(exc)) from exc
        except ChunkLimitError as exc:
            raise ServiceError("chunk_limit_reached", str(exc)) from exc
        except CollectionLimitError as exc:
            # Same as ingest(): the implicit create is what can trip the cap.
            raise ServiceError("collection_limit_reached", str(exc)) from exc
        except EmbedderUnavailableError as exc:
            log.warning(
                "ingest_text embedder_unavailable tenant=%s coll=%s "
                "docid=%s: %s",
                tenant, collection, baseid, exc,
            )
            return {
                "ok": False,
                "code": "embedder_unavailable",
                "error": str(exc),
            }
        except Exception as e:
            log.warning(
                "ingest_text failed tenant=%s coll=%s docid=%s: %s",
                tenant, collection, baseid, e,
                exc_info=True,
            )
            return {
                "ok": False,
                "code": "ingest_failed",
                "error": str(e),
            }


def _ingest_documents_batch_serial(
    store,
    tenant: str,
    collection: str,
    items: list[Mapping[str, Any]],
) -> ServiceResult:
    results: list[dict[str, Any]] = []
    succeeded = 0
    failed = 0
    for index, item in enumerate(items):
        try:
            result = ingest_text(
                store,
                tenant,
                collection,
                item.get("text"),
                item.get("docid"),
                item.get("metadata"),
                vector=item.get("vector"),
            )
        except ServiceError as exc:
            result = {
                "ok": False,
                "code": exc.code,
                "error": exc.message,
            }
        if result.get("ok"):
            succeeded += 1
            results.append({
                "ok": True,
                "index": index,
                "docid": result["docid"],
                "chunks": result["chunks"],
            })
        else:
            failed += 1
            results.append({
                "ok": False,
                "index": index,
                "docid": item.get("docid"),
                "code": result.get("code", "ingest_failed"),
                "error": result.get("error", "failed to ingest document"),
            })

    return {
        "ok": True,
        "tenant": tenant,
        "collection": collection,
        "documents": results,
        "count": len(results),
        "succeeded": succeeded,
        "failed": failed,
    }


def ingest_documents_batch(
    store,
    tenant: str,
    collection: str,
    documents: Iterable[Mapping[str, Any]],
) -> ServiceResult:
    collection = _validate_collection_name(collection)
    items = list(documents)
    if not items:
        return {
            "ok": False,
            "code": "invalid_batch",
            "error": "documents must not be empty",
            "error_type": "invalid",
        }

    # Per-item checks alone let N documents just under the per-document limit
    # through, so one request could carry N times the configured budget; the
    # running total is what keeps a batch inside it. Both are checked before
    # any preprocessing or embedding work starts.
    batch_bytes = 0
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            return {
                "ok": False,
                "code": "invalid_batch",
                "error": f"documents[{index}] must be an object",
                "error_type": "invalid",
            }
        text = item.get("text")
        has_text = isinstance(text, str)
        has_vector = isinstance(item.get("vector"), list)
        if has_text == has_vector:
            return {
                "ok": False,
                "code": "invalid_batch",
                "error": (
                    f"documents[{index}] must provide exactly one of "
                    "text (string) or vector (list of floats)"
                ),
                "error_type": "invalid",
            }
        if has_text:
            text_bytes = len(text.encode("utf-8"))
            _enforce_ingest_size_limit(text_bytes)
            batch_bytes += text_bytes
            _enforce_batch_size_limit(batch_bytes)
        docid_value = item.get("docid")
        if docid_value is not None and not isinstance(docid_value, str):
            return {
                "ok": False,
                "code": "invalid_batch",
                "error": f"documents[{index}].docid must be a string or null",
                "error_type": "invalid",
            }
        metadata_value = item.get("metadata")
        if metadata_value is not None and not isinstance(metadata_value, dict):
            return {
                "ok": False,
                "code": "invalid_batch",
                "error": f"documents[{index}].metadata must be an object or null",
                "error_type": "invalid",
            }

    # ponytail: vector batches take the serial path (per-item ingest_text),
    # skipping index_documents' single encode/add cycle. Identity batches are
    # homogeneous all-vector; add an all-vector branch to index_documents only
    # if bulk-BYOV throughput ever demands it.
    has_any_vector = any(
        isinstance(item.get("vector"), list) for item in items
    )
    batch_index = getattr(store, "index_documents", None)
    if has_any_vector or not callable(batch_index):
        return _ingest_documents_batch_serial(store, tenant, collection, items)

    from pave.filters import sanit_meta_dict

    results: list[dict[str, Any] | None] = [None] * len(items)
    prepared: list[tuple[str, list[tuple[str, str, Metadata]], Metadata]] = []
    prepared_indexes: list[int] = []
    seen_docids: set[str] = set()
    duplicate_docid = False
    batch_started = _time.perf_counter()
    now = datetime.now(tz.utc).isoformat(timespec="seconds")
    now = now.replace("+00:00", "Z")

    for index, item in enumerate(items):
        baseid = _validate_docid(item.get("docid") or _default_docid(""))
        if baseid in seen_docids:
            duplicate_docid = True
            break
        seen_docids.add(baseid)

        metadata = item.get("metadata") or {}
        doc_meta = {
            "docid": baseid,
            "ingested_at": now,
            **metadata,
        }
        try:
            sanit_meta_dict(doc_meta)
        except MetadataValidationError as exc:
            results[index] = {
                "ok": False,
                "index": index,
                "docid": baseid,
                "code": "invalid_metadata_keys",
                "error": str(exc),
            }
            continue

        records = [
            (f"{baseid}::{local_id}", chunk, extra)
            for local_id, chunk, extra in preprocess_text(str(item["text"]))
        ]
        if not records:
            results[index] = {
                "ok": False,
                "index": index,
                "docid": baseid,
                "code": "no_text_extracted",
                "error": "no text extracted",
            }
            continue

        prepared.append((baseid, records, doc_meta))
        prepared_indexes.append(index)

    if duplicate_docid:
        return _ingest_documents_batch_serial(store, tenant, collection, items)

    if prepared:
        try:
            with m_timed("ingest"):
                indexed = batch_index(tenant, collection, prepared)
        except EmbedderUnavailableError as exc:
            indexed = []
            for result_index in prepared_indexes:
                results[result_index] = {
                    "ok": False,
                    "index": result_index,
                    "docid": items[result_index].get("docid"),
                    "code": "embedder_unavailable",
                    "error": str(exc),
                }
        except MetadataValidationError as exc:
            indexed = []
            for result_index in prepared_indexes:
                results[result_index] = {
                    "ok": False,
                    "index": result_index,
                    "docid": items[result_index].get("docid"),
                    "code": "invalid_metadata_keys",
                    "error": str(exc),
                }
        except (ChunkLimitError, CollectionLimitError) as exc:
            # The store rejects the batch as a whole, so every prepared
            # document reports the quota rather than a generic failure.
            indexed = []
            code = (
                "chunk_limit_reached"
                if isinstance(exc, ChunkLimitError)
                else "collection_limit_reached"
            )
            for result_index in prepared_indexes:
                results[result_index] = {
                    "ok": False,
                    "index": result_index,
                    "docid": items[result_index].get("docid"),
                    "code": code,
                    "error": str(exc),
                }
        except Exception as exc:
            indexed = []
            log.warning(
                f"ingest_batch failed tenant={tenant} coll={collection}: {exc}",
                exc_info=True,
            )
            for result_index in prepared_indexes:
                results[result_index] = {
                    "ok": False,
                    "index": result_index,
                    "docid": items[result_index].get("docid"),
                    "code": "ingest_failed",
                    "error": str(exc),
                }

        for result_index, doc, index_result in zip(
            prepared_indexes,
            prepared,
            indexed,
            strict=False,
        ):
            baseid = doc[0]
            if index_result.purged_chunks:
                m_inc("purge_total", float(index_result.purged_chunks))
            m_inc("documents_indexed_total", 1.0)
            m_inc("chunks_indexed_total", float(index_result.indexed_chunks))
            results[result_index] = {
                "ok": True,
                "index": result_index,
                "docid": baseid,
                "chunks": index_result.indexed_chunks,
            }

    finalized = [
        result if result is not None else {
            "ok": False,
            "index": index,
            "docid": items[index].get("docid"),
            "code": "ingest_failed",
            "error": "failed to ingest document",
        }
        for index, result in enumerate(results)
    ]
    succeeded = sum(1 for result in finalized if result.get("ok"))
    failed = len(finalized) - succeeded
    chunks = sum(
        int(result.get("chunks") or 0)
        for result in finalized
        if result.get("ok")
    )
    latency_ms = round((_time.perf_counter() - batch_started) * 1000, 2)
    log.info(
        f"ingest_batch tenant={tenant} coll={collection} "
        f"docs={succeeded} failed={failed} chunks={chunks} ms={latency_ms:.2f}"
    )

    return {
        "ok": True,
        "tenant": tenant,
        "collection": collection,
        "documents": finalized,
        "count": len(finalized),
        "succeeded": succeeded,
        "failed": failed,
    }


def search(store, tenant: str, collection: str, q: str | None, k: int = 5,
           filters: FilterSpec | None = None,
           include_common: bool = False,
           common_tenant: str | None = None,
           common_collection: str | None = None,
           *,
           request_id: str | None = None,
           actor: str,
           _log: bool = True,
           v: list[float] | None = None) -> ServiceResult:
    start = _time.perf_counter()
    m_inc("search_total", 1.0)
    try:
        # Unvalidated here, the name reached _load_or_init and auto-created a
        # collection that delete_collection then refused to remove, because it
        # validates the name before checking existence. Safe for the
        # shared-scope callers too: enforce_policy holds common_collection to
        # the same rule at startup.
        _validate_collection_name(collection)
        if (
            _is_identity_collection(store, tenant, collection)
            and q is not None
        ):
            raise ServiceError("vectors_only", "collection accepts vectors only")
        if v is not None:
            expected_dim = _get_collection_vector_dim(
                store,
                tenant,
                collection,
            )
            if expected_dim is not None and len(v) != expected_dim:
                raise ServiceError(
                    "dim_mismatch",
                    f"vector length {len(v)} does not match "
                    f"collection dim {expected_dim}",
                )
        merge_common = bool(
            q is not None and include_common and common_tenant and common_collection
        )
        effective_common_tenant = (
            common_tenant if merge_common else None
        )
        effective_common_collection = (
            common_collection if merge_common else None
        )
        if (
            merge_common
            and effective_common_tenant
            and effective_common_collection
        ):
            _require_compatible_embeddings(
                store,
                tenant,
                collection,
                effective_common_tenant,
                effective_common_collection,
            )
            local = store.search(
                tenant, collection, q, max(10, k * 2), filters=filters)
            try:
                common = store.search(
                    effective_common_tenant,
                    effective_common_collection,
                    q,
                    max(10, k * 2),
                    filters=filters)
            except ValueError:
                # The shared corpus is an enrichment, not a requirement. Now
                # that a missing collection is an error rather than something
                # the read path creates, an operator who enabled the merge
                # without a corpus must not break the caller's own search.
                log.warning(
                    "shared corpus %s/%s is missing; serving %s/%s alone",
                    effective_common_tenant,
                    effective_common_collection,
                    tenant,
                    collection,
                )
                common = None
            matches = list(local.matches) + (
                list(common.matches) if common is not None else []
            )
            timing = (
                _sum_timing(local.timing, common.timing)
                if common is not None
                else local.timing
            )
            from heapq import nlargest
            top = nlargest(k, matches, key=lambda x: x.score)
        elif q is None:
            # BYOV: q is None → pass the raw query vector, store skips embed.
            result = store.search(tenant, collection, "", k,
                                  filters=filters, query_vector=v)
            top = result.matches
            timing = result.timing
        else:
            result = store.search(tenant, collection, q, k, filters=filters)
            top = result.matches
            timing = result.timing
        m_inc("matches_total", float(len(top) or 0))
        labels = {"tenant": tenant, "collection": collection}
        if not top:
            m_inc_labeled("searches_zero_matches_total", labels)
        m_inc_labeled(
            "searches_with_filters_total"
            if filters
            else "searches_without_filters_total",
            labels,
        )
        if merge_common and (
            effective_common_tenant != tenant
            or effective_common_collection != collection
        ):
            m_inc("searches_common_merge_total", 1.0)
        latency_ms = round((_time.perf_counter() - start) * 1000, 2)
        m_record_latency("search", latency_ms)
        _t = top[0] if top else None
        log.info(
            f"search tenant={tenant} coll={collection} k={k} "
            f"hits={len(top)} ms={latency_ms:.2f}"
            # No chunk text here: on a hosted deployment it would copy tenant
            # document content into the operator's log pipeline by default.
            + (f" top=[{_t.id} {_t.score:.3f}]" if _t else "")
            + (f" req={request_id}" if request_id else ""))
        query_id: str | None = None
        if _log and _query_log_enabled():
            query_id = str(uuid.uuid4())
            try:
                store.log_query(
                    query_id=query_id,
                    tenant=tenant,
                    collection=collection,
                    actor=actor,
                    # ponytail: BYOV queries have no text; log "" (replay of a
                    # raw-vector query is out of scope until the log stores v).
                    query_text=q or "",
                    k=k,
                    filters=filters,
                    include_common=merge_common,
                    common_tenant=effective_common_tenant,
                    common_collection=effective_common_collection,
                    result_ids=[r.id for r in top],
                    result_count=len(top),
                    latency_ms=latency_ms,
                    timing=timing,
                    request_id=request_id,
                )
            except Exception:
                log.warning("query log write failed", exc_info=True)
                query_id = None
        return {
            "ok": True,
            "matches": [r.to_dict() for r in top],
            "latency_ms": latency_ms,
            "timing": timing,
            "request_id": request_id,
            "query_id": query_id,
        }
    except ServiceError:
        raise
    except CollectionLimitError as exc:
        # Binding the quota on the load path means a read can now hit it. It
        # is still a quota answer, not a server fault: 429, like every other
        # path that reports this.
        raise ServiceError("collection_limit_reached", str(exc)) from exc
    except EmbedderUnavailableError as exc:
        log.warning(
            "search embedder_unavailable tenant=%s coll=%s request_id=%s: %s",
            tenant,
            collection,
            request_id,
            exc,
        )
        raise ServiceError("embedder_unavailable", str(exc)) from exc
    except ValueError as exc:
        msg = str(exc)
        if "does not exist" in msg:
            log.info(
                "search collection_not_found tenant=%s coll=%s "
                "request_id=%s: %s",
                tenant, collection, request_id, msg,
            )
            raise ServiceError("collection_not_found", msg) from exc
        raise
    except Exception as exc:
        log.warning(
            "search failed tenant=%s coll=%s request_id=%s: %s",
            tenant,
            collection,
            request_id,
            exc,
            exc_info=True,
        )
        raise ServiceError("search_failed", str(exc)) from exc


def dump_archive(
    store: BaseStore,
    output_path: str | os.PathLike[str] | None = None,
) -> tuple[str, str | None]:
    return store.dump_archive(output_path)


def restore_archive(store: BaseStore, archive_bytes: bytes) -> ServiceResult:
    store.restore_archive(archive_bytes)
    return {"ok": True}


def dump_collection_archive(
    store: BaseStore,
    tenant: str,
    collection: str,
    output_path: str | os.PathLike[str] | None = None,
) -> tuple[str, str | None]:
    resolved_collection = _validate_collection_name(collection)
    return store.dump_collection_archive(
        tenant,
        resolved_collection,
        output_path,
    )


def restore_collection_archive(
    store: BaseStore,
    tenant: str,
    collection: str,
    archive_bytes: bytes,
) -> ServiceResult:
    resolved_collection = _validate_collection_name(collection)
    store.restore_collection_archive(
        tenant,
        resolved_collection,
        archive_bytes,
    )
    return {
        "ok": True,
        "tenant": tenant,
        "collection": resolved_collection,
    }


def list_tenants(store) -> ServiceResult:
    try:
        tenants = sorted(store.list_tenants())
        return {
            "ok": True,
            "tenants": tenants,
            "count": len(tenants),
        }
    except Exception as e:
        log.warning("list_tenants failed: %s", e)
        return {
            "ok": False,
            "code": "list_tenants_failed",
            "error": str(e),
        }


def list_embedders(*, tenant: str | None = None) -> ServiceResult:
    try:
        cfg = get_cfg()
        instances = embedder_instances(cfg)
        default = resolve_default_embedder(cfg)
        out: ServiceResult = {
            "ok": True,
            "embedders": [
                {
                    "key": instance.key,
                    "vector_key": instance.vector_key,
                    "embedder_type": instance.spec["embedder_type"],
                    "embed_model": instance.spec["embed_model"],
                }
                for instance in instances
            ],
            "default": {
                "selector": default.selector,
                "vector_key": default.vector_key,
                "instance_keys": [
                    instance.key for instance in default.instances
                ],
            },
            "count": len(instances),
        }
        if tenant is not None:
            out["tenant"] = tenant
        return out
    except Exception as e:
        log.warning("list_embedders failed tenant=%s: %s", tenant, e)
        return {
            "ok": False,
            "code": "list_embedders_failed",
            "error": str(e),
        }


def list_collections(store, tenant: str) -> ServiceResult:
    try:
        collections = store.list_collections(tenant)
        return {
            "ok": True,
            "tenant": tenant,
            "collections": collections,
            "count": len(collections),
        }
    except Exception as e:
        log.warning(
            "list_collections failed tenant=%s: %s",
            tenant, e,
        )
        return {
            "ok": False,
            "code": "list_collections_failed",
            "error": str(e),
        }


def _query_not_found(query_id: str) -> ServiceResult:
    return {
        "ok": False,
        "code": "query_not_found",
        "error": f"query '{query_id}' not found",
        "error_type": "not_found",
    }


def resolve_query_home(
    store,
    query_id: str,
) -> tuple[str, str] | None:
    return store.resolve_query_home(query_id)


def _scoped_query_home_mismatch(
    store,
    tenant: str,
    collection: str,
    query_id: str,
) -> bool:
    home = resolve_query_home(store, query_id)
    return home is not None and home != (tenant, collection)


def get_query_log_entry_scoped(
    store,
    tenant: str,
    collection: str,
    query_id: str,
) -> ServiceResult:
    try:
        if _scoped_query_home_mismatch(store, tenant, collection, query_id):
            return _query_not_found(query_id)
        entry = store.get_query_log_entry(tenant, collection, query_id)
        if entry is None:
            return _query_not_found(query_id)
        return {"ok": True, "query": entry}
    except Exception as e:
        return {
            "ok": False,
            "code": "query_log_failed",
            "error": str(e),
        }


def get_query_log_entry(
    store,
    query_id: str,
) -> ServiceResult:
    try:
        home = resolve_query_home(store, query_id)
        if home is None:
            return _query_not_found(query_id)
        tenant, collection = home
        return get_query_log_entry_scoped(
            store,
            tenant,
            collection,
            query_id,
        )
    except Exception as e:
        return {
            "ok": False,
            "code": "query_log_failed",
            "error": str(e),
        }


def list_query_logs(
    store,
    tenant: str,
    collection: str,
    limit: int = 50,
    offset: int = 0,
) -> ServiceResult:
    try:
        logs = store.list_query_logs(tenant, collection, limit, offset)
        return {
            "ok": True,
            "tenant": tenant,
            "collection": collection,
            "queries": logs,
            "count": len(logs),
        }
    except Exception as e:
        return {
            "ok": False,
            "code": "list_query_logs_failed",
            "error": str(e),
        }


def list_query_homes(
    store,
    tenant: str | None = None,
    collection: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> ServiceResult:
    try:
        logs = store.list_query_homes(
            tenant=tenant,
            collection=collection,
            limit=limit,
            offset=offset,
        )
        return {
            "ok": True,
            "tenant": tenant,
            "collection": collection,
            "queries": logs,
            "count": len(logs),
        }
    except Exception as e:
        return {
            "ok": False,
            "code": "list_query_homes_failed",
            "error": str(e),
        }


def execute_replay(
    store,
    entry: QueryLogEntry,
    tenant: str,
    collection: str,
    *,
    request_id: str | None = None,
    actor: str,
) -> ServiceResult:
    query_id = str(entry["query_id"])
    # Re-derive the shared scope from live config instead of replaying the
    # persisted target. A stored row would otherwise keep reading a common
    # collection after common_enabled was turned off or re-pointed.
    cfg = get_cfg()
    replay_common = bool(entry.get("include_common") and cfg.common_enabled)
    replay_common_tenant = cfg.common_tenant if replay_common else None
    replay_common_collection = cfg.common_collection if replay_common else None
    # Rows logged before k was bounded would otherwise stay replayable forever
    # at their original size, one request pulling a whole collection.
    replay_k = min(int(entry["k"]), MAX_SEARCH_K)
    try:
        result = search(
            store,
            tenant,
            collection,
            entry["query_text"],
            replay_k,
            filters=entry.get("filters"),
            include_common=replay_common,
            common_tenant=replay_common_tenant,
            common_collection=replay_common_collection,
            request_id=request_id,
            actor=actor,
            _log=False,
        )
        if not result.get("ok"):
            return result

        replay_qid: str | None = None
        if _query_log_enabled():
            replay_qid = str(uuid.uuid4())
            try:
                store.log_query(
                    query_id=replay_qid,
                    tenant=tenant,
                    collection=collection,
                    actor=actor,
                    query_text=entry["query_text"],
                    k=replay_k,
                    filters=entry.get("filters"),
                    include_common=replay_common,
                    common_tenant=replay_common_tenant,
                    common_collection=replay_common_collection,
                    result_ids=[match["id"] for match in result["matches"]],
                    result_count=len(result["matches"]),
                    latency_ms=result.get("latency_ms"),
                    timing=result.get("timing"),
                    request_id=request_id,
                    replay_of=query_id,
                )
            except Exception:
                log.warning("replay log failed", exc_info=True)
                replay_qid = None

        return {
            "ok": True,
            "original_query_id": query_id,
            "replay_query_id": replay_qid,
            "matches": result["matches"],
            "timing": result.get("timing"),
            "original_result_count": entry.get("result_count", 0),
            "original_latency_ms": entry.get("latency_ms"),
            "latency_ms": result.get("latency_ms"),
            "request_id": request_id,
        }
    except ServiceError:
        raise
    except Exception as e:
        raise ServiceError("replay_query_failed", str(e)) from e


def replay_query_scoped(
    store,
    tenant: str,
    collection: str,
    query_id: str,
    *,
    request_id: str | None = None,
    actor: str,
) -> ServiceResult:
    try:
        if _scoped_query_home_mismatch(store, tenant, collection, query_id):
            return _query_not_found(query_id)
        entry = store.get_query_log_entry(tenant, collection, query_id)
        if entry is None:
            return _query_not_found(query_id)
        return execute_replay(
            store,
            entry,
            tenant,
            collection,
            request_id=request_id,
            actor=actor,
        )
    except ServiceError:
        raise
    except Exception as e:
        raise ServiceError("replay_query_failed", str(e)) from e


def replay_query(
    store,
    query_id: str,
    *,
    request_id: str | None = None,
    actor: str,
) -> ServiceResult:
    try:
        home = resolve_query_home(store, query_id)
        if home is None:
            return _query_not_found(query_id)
        tenant, collection = home
        entry = store.get_query_log_entry(tenant, collection, query_id)
        if entry is None:
            return _query_not_found(query_id)
        return execute_replay(
            store,
            entry,
            tenant,
            collection,
            request_id=request_id,
            actor=actor,
        )
    except ServiceError:
        raise
    except Exception as e:
        raise ServiceError("replay_query_failed", str(e)) from e


def get_collection_detail(
    store,
    tenant: str,
    name: str,
) -> ServiceResult:
    try:
        detail = store.get_collection_detail(tenant, name)
        if detail is None:
            log.info(
                f"get_collection_detail not_found tenant={tenant} coll={name}"
            )
            return {
                "ok": False,
                "code": "collection_not_found",
                "error": f"collection '{name}' not found",
                "error_type": "not_found",
            }
        return {"ok": True, **detail}
    except Exception as e:
        log.warning(
            f"get_collection_detail failed tenant={tenant} coll={name}: {e}"
        )
        return {
            "ok": False,
            "code": "get_collection_detail_failed",
            "error": str(e),
            "error_type": "failed",
        }
