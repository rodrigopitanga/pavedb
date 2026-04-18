# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone as tz
from pathlib import Path
from collections.abc import Iterable, Iterator
from typing import Any

import time as _time
from pave.config import get_cfg, get_logger
from pave.metrics import (
    inc as m_inc, timed as m_timed, record_latency as m_record_latency
)
from pave.preprocess import preprocess
from pave.stores.base import (
    BaseStore,
    MetadataValidationError,
    SearchOutput,
    SearchResult,
)

log = get_logger()

# Pure-ish service functions operating on a store adapter
class ServiceError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _resolve_global_embedder_config() -> tuple[str, str]:
    cfg = get_cfg()
    embedder_type = cfg.get("embedder.type").lower()
    embed_model = cfg.get(f"embedder.{embedder_type}.model")
    if not embed_model:
        raise ServiceError(
            "invalid_embedder_config",
            f"no model configured for embedder.{embedder_type}.model",
        )
    return embedder_type, str(embed_model)


def create_collection(
    store,
    tenant: str,
    name: str,
    *,
    embedder_type: str | None = None,
    embed_model: str | None = None,
) -> dict[str, Any]:
    try:
        global_type, global_model = _resolve_global_embedder_config()
        resolved_type = (embedder_type or global_type).strip().lower()
        resolved_model = str(embed_model or global_model).strip()

        if resolved_type != global_type:
            raise ServiceError(
                "embedder_type_not_supported",
                "per-collection embedder type not yet supported",
            )
        if resolved_model != global_model:
            raise ServiceError(
                "embed_model_not_supported",
                "per-collection embed model not yet supported",
            )

        store.create_collection(tenant, name)
        m_inc("collections_created_total", 1.0)
        return {
            "ok": True,
            "tenant": tenant,
            "collection": name,
            "embedder_type": global_type,
            "embed_model": global_model,
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

def delete_collection(store, tenant: str, name: str) -> dict[str, Any]:
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

def rename_collection(store, tenant: str,
                      old_name: str, new_name: str) -> dict[str, Any]:
    if old_name == new_name:
        log.info(
            "rename_collection rejected tenant=%s: "
            "same name %s", tenant, old_name,
        )
        return {
            "ok": False,
            "code": "rename_invalid",
            "error": "old and new names are the same",
            "error_type": "invalid",
        }
    try:
        store.rename_collection(tenant, old_name, new_name)
        m_inc("collections_renamed_total", 1.0)
        return {
            "ok": True,
            "tenant": tenant,
            "old_name": old_name,
            "new_name": new_name
        }
    except ValueError as e:
        err = str(e)
        if "does not exist" in err:
            log.info(
                "rename_collection not_found tenant=%s "
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
                "rename_collection conflict tenant=%s "
                "new=%s: %s", tenant, new_name, err,
            )
            return {
                "ok": False,
                "code": "collection_conflict",
                "error": err,
                "error_type": "conflict",
            }
        log.info(
            "rename_collection invalid tenant=%s "
            "old=%s new=%s: %s",
            tenant, old_name, new_name, err,
        )
        return {
            "ok": False,
            "code": "rename_invalid",
            "error": err,
            "error_type": "invalid",
        }
    except Exception as e:
        log.warning(
            "rename_collection failed tenant=%s "
            "old=%s new=%s: %s",
            tenant, old_name, new_name, e,
        )
        return {
            "ok": False,
            "code": "rename_failed",
            "error": str(e),
            "error_type": "failed",
        }

def delete_document(store, tenant: str, collection: str, docid: str) -> dict[str, Any]:
    try:
        if store.has_doc(tenant, collection, docid):
            purged = store.purge_doc(tenant, collection, docid)
            m_inc("purge_total", float(purged))
            m_inc("documents_deleted_total", 1.0)
        else:
            purged = 0
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
) -> dict[str, Any]:
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


def list_documents(
    store,
    tenant: str,
    collection: str,
) -> dict[str, Any]:
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

def ingest_document(store, tenant: str, collection: str, filename: str, content: bytes,
                    docid: str | None, metadata: dict[str, Any] | None,
                    csv_options: dict[str, Any] | None = None) -> dict[str, Any]:
    _t0 = _time.perf_counter()
    with m_timed("ingest"):
        try:
            baseid = docid or _default_docid(filename)
            if baseid and store.has_doc(tenant, collection, baseid):
                purged = store.purge_doc(tenant, collection, baseid)
                m_inc("purge_total", purged)
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
            count = store.index_records(tenant, collection, baseid, records, doc_meta)
            m_inc("documents_indexed_total", 1.0)
            m_inc("chunks_indexed_total", float(count or 0))
            latency_ms = round((_time.perf_counter() - _t0) * 1000, 2)
            log.info(
                f"ingest tenant={tenant} coll={collection} "
                f"docid={baseid} chunks={count} ms={latency_ms:.2f}"
            )
            return {
                "ok": True,
                "tenant": tenant,
                "collection": collection,
                "docid": baseid,
                "chunks": count
            }
        except ServiceError:
            raise
        except MetadataValidationError as exc:
            raise ServiceError("invalid_metadata_keys", str(exc)) from exc
        except ValueError as exc:
            raise ServiceError("invalid_csv_options", str(exc)) from exc
        except Exception as e:
            log.warning(
                "ingest failed tenant=%s coll=%s "
                "docid=%s: %s",
                tenant, collection,
                docid or filename, e,
            )
            return {
                "ok": False,
                "code": "ingest_failed",
                "error": str(e),
            }

def search(store, tenant: str, collection: str, q: str, k: int = 5,
              filters: dict[str, Any] | None = None, include_common: bool = False,
              common_tenant: str | None = None, common_collection: str | None = None,
              request_id: str | None = None
              ) -> dict[str, Any]:
    start = _time.perf_counter()
    m_inc("search_total", 1.0)
    try:
        if include_common and common_tenant and common_collection:
            local = _normalize_search_output(store.search(
                tenant, collection, q, max(10, k * 2), filters=filters))
            common = _normalize_search_output(store.search(
                common_tenant, common_collection, q, max(10, k * 2),
                filters=filters))
            matches = list(local.matches) + list(common.matches)
            timing = _sum_timing(local.timing, common.timing)
            from heapq import nlargest
            top = nlargest(k, matches, key=lambda x: x.score)
            m_inc("matches_total", float(len(top) or 0))
            latency_ms = round((_time.perf_counter() - start) * 1000, 2)
            m_record_latency("search", latency_ms)
            _t = top[0] if top else None
            log.info(
                f"search tenant={tenant} coll={collection} k={k} "
                f"hits={len(top)} ms={latency_ms:.2f}"
                + (f" top=[{_t.id} {_t.score:.3f}] \"{(_t.text or '')[:60]}{'...' if len(_t.text or '') > 60 else ''}\"" if _t else "")
                + (f" req={request_id}" if request_id else ""))
            return {
                "ok": True,
                "matches": [r.to_dict() for r in top],
                "latency_ms": latency_ms,
                "timing": timing,
                "request_id": request_id,
            }
        result = _normalize_search_output(
            store.search(tenant, collection, q, k, filters=filters)
        )
        top = result.matches
        m_inc("matches_total", float(len(top) or 0))
        latency_ms = round((_time.perf_counter() - start) * 1000, 2)
        m_record_latency("search", latency_ms)
        _t = top[0] if top else None
        log.info(
            f"search tenant={tenant} coll={collection} k={k} "
            f"hits={len(top)} ms={latency_ms:.2f}"
            + (f" top=[{_t.id} {_t.score:.3f}] \"{(_t.text or '')[:60]}{'...' if len(_t.text or '') > 60 else ''}\"" if _t else "")
            + (f" req={request_id}" if request_id else ""))
        return {
            "ok": True,
            "matches": [r.to_dict() for r in top],
            "latency_ms": latency_ms,
            "timing": result.timing,
            "request_id": request_id,
        }
    except Exception as exc:
        raise ServiceError("search_failed", str(exc)) from exc


def dump_archive(
    store: BaseStore,
    output_path: str | os.PathLike[str] | None = None,
) -> tuple[str, str | None]:
    return store.dump_archive(output_path)


def restore_archive(store: BaseStore, archive_bytes: bytes) -> dict[str, Any]:
    store.restore_archive(archive_bytes)
    return {"ok": True}


def list_tenants(store) -> dict[str, Any]:
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

def list_collections(store, tenant: str) -> dict[str, Any]:
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


def get_collection_detail(
    store,
    tenant: str,
    name: str,
) -> dict[str, Any]:
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
