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

import errno
import json
import os
import re
import shutil
import tempfile
import unicodedata
import uuid
import zipfile
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone as tz
from pathlib import Path
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

import time as _time
from pave.config import get_cfg, get_logger
from pave.embedders import EmbedderUnavailableError
from pave.embedders.base import EmbedderConfig
from pave.embedders.factory import (
    embedder_instances,
    embedder_space_key,
    resolve_default_embedder,
    resolve_embedder_spec,
)
from pave.metrics import (
    inc as m_inc, timed as m_timed, record_latency as m_record_latency
)
from pave.preprocess import CsvOptions, preprocess, preprocess_text
from pave.stores.base import (
    BaseStore,
    FilterSpec,
    Metadata,
    MetadataValidationError,
    QueryLogEntry,
    SearchOutput,
    SearchResult,
)

log = get_logger()

ServiceResult = dict[str, Any]

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


def _resolve_global_embedder_config() -> tuple[str, str]:
    try:
        spec = resolve_embedder_spec(get_cfg())
    except ValueError as exc:
        raise ServiceError(
            "invalid_embedder_config",
            str(exc),
        ) from exc
    return str(spec["embedder_type"]), str(spec["embed_model"])


def create_collection(
    store,
    tenant: str,
    name: str | None,
    *,
    display_name: str | None = None,
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
        created = store.create_collection(
            tenant,
            resolved_name,
            embedder_type=embedder_type,
            embed_model=embed_model,
            embedder_config=embedder_config,
            display_name=resolved_display_name,
        )
        if isinstance(created, dict):
            resolved_type = str(created.get("embedder_type") or "")
            resolved_model = str(created.get("embed_model") or "")
            resolved_config = dict(created.get("embedder_config") or {})
            returned_display = str(
                created.get("display_name") or resolved_display_name
            )
        else:
            global_type, global_model = _resolve_global_embedder_config()
            resolved_type = str(embedder_type or global_type).strip().lower()
            resolved_model = str(embed_model or global_model).strip()
            resolved_config = dict(embedder_config or {})
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
        updated = store.update_collection(
            tenant,
            resolved_name,
            display_name=resolved_display_name,
        )
        if updated is None:
            raise ValueError(f"collection '{resolved_name}' does not exist")
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
        err = str(e)
        if "does not exist" in err or "missing" in err:
            return {
                "ok": False,
                "code": "collection_not_found",
                "error": err,
                "error_type": "not_found",
            }
        return {
            "ok": False,
            "code": "update_collection_invalid",
            "error": err,
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


def _normalize_search_output(result: SearchOutput | list[SearchResult]) -> SearchOutput:
    if isinstance(result, SearchOutput):
        return result
    return SearchOutput(matches=list(result))


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

def delete_collection(store, tenant: str, name: str) -> ServiceResult:
    try:
        store.delete_collection(tenant, name)
        m_inc("collections_deleted_total", 1.0)
        return {
            "ok": True,
            "tenant": tenant,
            "deleted": name
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
    # Uppercase
    base = filename.upper()
    # replace space and dot with underscore
    base = base.replace(" ", "_").replace(".", "_")
    # replace all non A-Z0-9_ with underscore
    base = re.sub(r"[^A-Z0-9_]", "_", base)
    # collapse multiple underscores
    base = re.sub(r"_+", "_", base).strip("_")
    if base != '': return base
    return "PVDOC_"+str(uuid.uuid4())

def _index_records_result(store, tenant: str, collection: str, baseid: str,
                          records, doc_meta, t0: float, label: str) -> ServiceResult:
    """Shared tail for ingest paths: index records, metrics, log, envelope."""
    result = store.index_records(tenant, collection, baseid, records, doc_meta)
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
    _t0 = _time.perf_counter()
    with m_timed("ingest"):
        try:
            stage = "prepare"
            baseid = docid or _default_docid(filename)
            meta_from_call = metadata or {}
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
    text: str,
    docid: str | None,
    metadata: Metadata | None,
) -> ServiceResult:
    _t0 = _time.perf_counter()
    with m_timed("ingest"):
        try:
            baseid = docid or _default_docid("")
            meta_from_call = metadata or {}
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


def ingest_documents_batch(
    store,
    tenant: str,
    collection: str,
    documents: Iterable[Mapping[str, Any]],
) -> ServiceResult:
    items = list(documents)
    if not items:
        return {
            "ok": False,
            "code": "invalid_batch",
            "error": "documents must not be empty",
            "error_type": "invalid",
        }

    results: list[dict[str, Any]] = []
    succeeded = 0
    failed = 0
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            return {
                "ok": False,
                "code": "invalid_batch",
                "error": f"documents[{index}] must be an object",
                "error_type": "invalid",
            }
        text = item.get("text")
        if not isinstance(text, str):
            return {
                "ok": False,
                "code": "invalid_batch",
                "error": f"documents[{index}].text must be a string",
                "error_type": "invalid",
            }
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

    for index, item in enumerate(items):
        try:
            result = ingest_text(
                store,
                tenant,
                collection,
                str(item["text"]),
                item.get("docid"),
                item.get("metadata"),
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


def search(store, tenant: str, collection: str, q: str, k: int = 5,
           filters: FilterSpec | None = None,
           include_common: bool = False,
           common_tenant: str | None = None,
           common_collection: str | None = None,
           *,
           request_id: str | None = None,
           actor: str,
           _log: bool = True) -> ServiceResult:
    start = _time.perf_counter()
    m_inc("search_total", 1.0)
    try:
        if include_common and common_tenant and common_collection:
            _require_compatible_embeddings(
                store,
                tenant,
                collection,
                common_tenant,
                common_collection,
            )
            local = _normalize_search_output(store.search(
                tenant, collection, q, max(10, k * 2), filters=filters))
            common = _normalize_search_output(store.search(
                common_tenant, common_collection, q, max(10, k * 2),
                filters=filters))
            matches = list(local.matches) + list(common.matches)
            timing = _sum_timing(local.timing, common.timing)
            from heapq import nlargest
            top = nlargest(k, matches, key=lambda x: x.score)
        else:
            result = _normalize_search_output(
                store.search(tenant, collection, q, k, filters=filters)
            )
            top = result.matches
            timing = result.timing
        m_inc("matches_total", float(len(top) or 0))
        latency_ms = round((_time.perf_counter() - start) * 1000, 2)
        m_record_latency("search", latency_ms)
        _t = top[0] if top else None
        log.info(
            f"search tenant={tenant} coll={collection} k={k} "
            f"hits={len(top)} ms={latency_ms:.2f}"
            + (f" top=[{_t.id} {_t.score:.3f}] "
               f"\"{(_t.text or '')[:60]}"
               f"{'...' if len(_t.text or '') > 60 else ''}\""
               if _t else "")
            + (f" req={request_id}" if request_id else ""))
        query_id: str | None = None
        if _log:
            query_id = str(uuid.uuid4())
            try:
                store.log_query(
                    query_id=query_id,
                    tenant=tenant,
                    collection=collection,
                    actor=actor,
                    query_text=q,
                    k=k,
                    filters=filters,
                    include_common=include_common,
                    common_tenant=common_tenant,
                    common_collection=common_collection,
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
    try:
        result = search(
            store,
            tenant,
            collection,
            entry["query_text"],
            entry["k"],
            filters=entry.get("filters"),
            include_common=entry.get("include_common", False),
            common_tenant=entry.get("common_tenant"),
            common_collection=entry.get("common_collection"),
            request_id=request_id,
            actor=actor,
            _log=False,
        )
        if not result.get("ok"):
            return result

        replay_qid = str(uuid.uuid4())
        try:
            store.log_query(
                query_id=replay_qid,
                tenant=tenant,
                collection=collection,
                actor=actor,
                query_text=entry["query_text"],
                k=entry["k"],
                filters=entry.get("filters"),
                include_common=entry.get("include_common", False),
                common_tenant=entry.get("common_tenant"),
                common_collection=entry.get("common_collection"),
                result_ids=[match["id"] for match in result["matches"]],
                result_count=len(result["matches"]),
                latency_ms=result.get("latency_ms"),
                timing=result.get("timing"),
                request_id=request_id,
                replay_of=query_id,
            )
        except Exception:
            log.warning("replay log failed", exc_info=True)

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
