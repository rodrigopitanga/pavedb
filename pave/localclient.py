# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pavesdk.batch import batch_item
from pavesdk.errors import (
    Conflict,
    InvalidRequest,
    NotFoundError,
    PaveError,
    Unavailable,
)
from pavesdk.interface import (
    BaseClient,
    Collection,
)

import pave.embedders as embedders
from pave.config import get_cfg, get_logger, should_warm_default_embedder
from pave.data_dir_lease import DataDirInUseError, DataDirLease
from pave.embedders import LazyEmbedder
from pave.service import (
    ServiceError,
    cancel_reindex_job as svc_cancel_reindex_job,
    create_collection as svc_create_collection,
    delete_collection as svc_delete_collection,
    delete_document as svc_delete_document,
    dump_archive as svc_dump_archive,
    dump_collection_archive as svc_dump_collection_archive,
    get_chunk as svc_get_chunk,
    get_chunk_content as svc_get_chunk_content,
    get_collection_detail as svc_get_collection_detail,
    get_document as svc_get_document,
    get_query_log_entry_scoped as svc_get_query_log_entry_scoped,
    get_reindex_job as svc_get_reindex_job,
    ingest_document as svc_ingest_document,
    ingest_documents_batch as svc_ingest_documents_batch,
    ingest_text as svc_ingest_text,
    list_chunks as svc_list_chunks,
    list_collections as svc_list_collections,
    list_documents as svc_list_documents,
    list_embedders as svc_list_embedders,
    list_query_homes as svc_list_query_homes,
    list_tenants as svc_list_tenants,
    move_collection as svc_move_collection,
    pause_reindex_job as svc_pause_reindex_job,
    replay_query_scoped as svc_replay_query_scoped,
    restore_archive as svc_restore_archive,
    restore_collection_archive as svc_restore_collection_archive,
    resume_reindex_job as svc_resume_reindex_job,
    search as svc_search,
    start_collection_reindex as svc_start_collection_reindex,
    update_collection as svc_update_collection,
)
from pave.stores.base import (
    BaseStore,
    ContentFilterSpec,
    FilterSpec,
    Metadata,
    SearchMode,
)
from pave.stores.local import LocalStore


_ERROR_TYPES = {
    "not_found": NotFoundError,
    "invalid": InvalidRequest,
    "conflict": Conflict,
    "unavailable": Unavailable,
}


def _error_class(code: str, error_type: str | None = None) -> type[PaveError]:
    if error_type in _ERROR_TYPES:
        return _ERROR_TYPES[error_type]
    if code == "embedder_unavailable" or code.endswith("_unavailable"):
        return Unavailable
    if code.endswith("_not_found") or code == "not_found":
        return NotFoundError
    if "conflict" in code or code.endswith("_exists"):
        return Conflict
    if (
        code.startswith("invalid_")
        or code.endswith("_invalid")
        or code in ("no_text_extracted", "vectors_only", "dim_mismatch")
    ):
        return InvalidRequest
    return PaveError


def _raise_result(result: Mapping[str, Any]) -> None:
    code = str(result.get("code") or "pavedb_error")
    message = str(result.get("error") or result.get("message") or code)
    error_type = result.get("error_type")
    cls = _error_class(code, str(error_type) if error_type else None)
    raise cls(code, message)


def _translate_service_error(exc: ServiceError) -> PaveError:
    cls = _error_class(exc.code)
    return cls(exc.code, exc.message)


def _batch_item_with_vector(document: object) -> dict[str, Any]:
    # pavesdk.batch_item drops `vector`; re-attach it for BYOV batches.
    item = batch_item(document)
    if isinstance(document, Mapping) and document.get("vector") is not None:
        item["vector"] = document["vector"]
    return item


def _unwrap(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("ok") is False:
        _raise_result(result)
    return {k: v for k, v in result.items() if k != "ok"}


def _store_for_path(path: str | os.PathLike[str]) -> BaseStore:
    store = LocalStore(
        data_dir=str(path),
        embedder=LazyEmbedder(embedders.get_embedder),
    )
    if should_warm_default_embedder(get_cfg()):
        try:
            store.warm()
        except Exception as exc:
            get_logger().warning("Local provider warm-up failed: %s", exc)
    return store


def provider(
    target: str | os.PathLike[str] | None = None,
    *,
    tenant: str = "default",
) -> LocalClient:
    if target is None:
        return LocalClient._ephemeral(tenant=tenant)
    raw = os.fspath(target)
    if raw == "":
        raise InvalidRequest(
            "unsupported_target",
            "empty local provider target; pass None for an ephemeral store",
        )
    parsed = urlparse(raw)
    if parsed.scheme in ("http", "https"):
        raise InvalidRequest(
            "unsupported_scheme",
            "network targets are handled by pavesdk.client.connect()",
        )
    if parsed.scheme and len(parsed.scheme) > 1:
        raise InvalidRequest(
            "unsupported_scheme",
            f"unsupported local provider scheme: {parsed.scheme}",
        )
    return LocalClient(raw, tenant=tenant)


class LocalClient(BaseClient):
    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        tenant: str = "default",
        store: BaseStore | None = None,
        _ephemeral: bool = False,
    ) -> None:
        self.path = os.fspath(path)
        self.tenant = tenant
        self._ephemeral = _ephemeral
        self._lease = None
        if store is None:
            try:
                self._lease = DataDirLease(self.path).acquire()
            except DataDirInUseError as exc:
                raise Unavailable("data_dir_in_use", str(exc)) from None
        try:
            self._store = (
                store if store is not None else _store_for_path(self.path)
            )
        except BaseException:
            if self._lease is not None:
                self._lease.close()
            raise
        self._closed = False

    @classmethod
    def _ephemeral(cls, *, tenant: str = "default") -> LocalClient:
        path = tempfile.mkdtemp(prefix="pavedb-")
        return cls(path, tenant=tenant, _ephemeral=True)

    def close(self) -> None:
        if self._closed:
            return
        close = getattr(self._store, "close", None)
        if callable(close):
            close()
        else:
            flush = getattr(self._store, "_flush_caches", None)
            if callable(flush):
                flush()
        lock_path = self._lease.path if self._lease is not None else None
        if self._ephemeral:
            shutil.rmtree(self.path, ignore_errors=True)
        if self._lease is not None:
            self._lease.close()
        if self._ephemeral and os.name == "nt" and lock_path is not None:
            with suppress(OSError):
                lock_path.unlink()
            with suppress(OSError):
                Path(self.path).rmdir()
        self._closed = True

    def _call(self, func, *args, **kwargs):
        self._ensure_open()
        try:
            return func(self._store, *args, **kwargs)
        except ServiceError as exc:
            raise _translate_service_error(exc) from exc

    def _create_collection(
        self,
        tenant: str,
        name: str,
        *,
        display_name: str | None,
        embedder_type: str | None,
        embed_model: str | None,
        embedder_config: Mapping[str, Any] | None,
        embedder: str | None = None,
        search_mode: SearchMode | None = None,
        chunking: Mapping[str, Any] | None = None,
        priority_key: str | None = None,
    ) -> dict:
        result = self._call(
            svc_create_collection,
            tenant,
            name,
            display_name=display_name,
            embedder=embedder,
            embedder_type=embedder_type,
            embed_model=embed_model,
            embedder_config=(
                dict(embedder_config)
                if embedder_config is not None
                else None
            ),
            search_mode=search_mode,
            chunking=dict(chunking) if chunking is not None else None,
            priority_key=priority_key,
        )
        return _unwrap(result)

    def _list_collections(self, tenant: str) -> list[Mapping[str, Any]]:
        result = self._call(svc_list_collections, tenant)
        data = _unwrap(result)
        return list(data["collections"])

    def _list_tenants(self) -> list[str]:
        result = self._call(svc_list_tenants)
        return list(_unwrap(result)["tenants"])

    def _embedders(self, tenant: str) -> dict:
        self._ensure_open()
        result = svc_list_embedders(tenant=tenant)
        return _unwrap(result)

    def _delete_collection(self, tenant: str, name: str) -> dict:
        result = self._call(svc_delete_collection, tenant, name)
        return _unwrap(result)

    def _dump_archive(self, path: str | os.PathLike[str] | None = None) -> str:
        self._ensure_open()
        try:
            archive_path, _source = svc_dump_archive(self._store, path)
        except ServiceError as exc:
            raise _translate_service_error(exc) from exc
        return archive_path

    def _restore_archive(self, archive_bytes: bytes) -> dict:
        result = self._call(svc_restore_archive, archive_bytes)
        return _unwrap(result)

    def _dump_collection_archive(
        self,
        tenant: str,
        name: str,
        path: str | os.PathLike[str] | None = None,
    ) -> str:
        self._ensure_open()
        try:
            archive_path, _source = svc_dump_collection_archive(
                self._store, tenant, name, path
            )
        except ServiceError as exc:
            raise _translate_service_error(exc) from exc
        return archive_path

    def _restore_collection_archive(
        self,
        tenant: str,
        name: str,
        archive_bytes: bytes,
        *,
        replace: bool = False,
    ) -> dict:
        result = self._call(
            svc_restore_collection_archive,
            tenant,
            name,
            archive_bytes,
            replace=replace,
        )
        return _unwrap(result)

    def _reindex(self, tenant: str, collection: str, **target: Any) -> dict:
        result = self._call(
            svc_start_collection_reindex, tenant, collection, **target
        )
        return _unwrap(result)

    def _own_reindex_job(self, tenant: str, collection: str, job_id: str) -> dict:
        """The job, if it belongs to this collection -- the self-service
        routes' scoping, so a handle never reaches another collection's job."""
        job = _unwrap(self._call(svc_get_reindex_job, job_id))
        if (job.get("tenant"), job.get("collection")) != (tenant, collection):
            raise NotFoundError(
                "reindex_job_not_found",
                f"reindex job {job_id!r} not found in {tenant}/{collection}",
            )
        return job

    def _reindex_job(self, tenant: str, collection: str, job_id: str) -> dict:
        return self._own_reindex_job(tenant, collection, job_id)

    def _cancel_reindex(
        self, tenant: str, collection: str, job_id: str
    ) -> dict:
        self._own_reindex_job(tenant, collection, job_id)
        return _unwrap(self._call(svc_cancel_reindex_job, job_id))

    def _pause_reindex(self, job_id: str) -> dict:
        return _unwrap(self._call(svc_pause_reindex_job, job_id))

    def _resume_reindex(self, job_id: str) -> dict:
        return _unwrap(self._call(svc_resume_reindex_job, job_id))

    def _ingest(
        self,
        tenant: str,
        collection: str,
        file: str | os.PathLike[str],
        *,
        docid: str | None = None,
        metadata: Metadata | None = None,
        csv_options: dict[str, Any] | None = None,
    ) -> dict:
        path = Path(file)
        result = self._call(
            svc_ingest_document,
            tenant,
            collection,
            str(path),
            path.read_bytes(),
            docid,
            metadata or {},
            csv_options=csv_options,
        )
        return _unwrap(result)

    def _add(
        self,
        tenant: str,
        collection: str,
        text: str | None = None,
        *,
        vector: list[float] | None = None,
        docid: str | None = None,
        metadata: Metadata | None = None,
    ) -> dict:
        result = self._call(
            svc_ingest_text,
            tenant,
            collection,
            text,
            docid,
            metadata or {},
            vector=vector,
        )
        return _unwrap(result)

    def _add_many(
        self,
        tenant: str,
        collection: str,
        documents: list[object],
    ) -> dict:
        result = self._call(
            svc_ingest_documents_batch,
            tenant,
            collection,
            [_batch_item_with_vector(document) for document in documents],
        )
        return _unwrap(result)

    def _search(
        self,
        tenant: str,
        collection: str,
        q: str | None,
        k: int = 5,
        *,
        filters: FilterSpec | None = None,
        content_filter: ContentFilterSpec | None = None,
        mode: SearchMode | None = None,
        include_common: bool | None = None,
        vector: list[float] | None = None,
    ) -> list[dict]:
        cfg = get_cfg()
        result = self._call(
            svc_search,
            tenant,
            collection,
            q,
            k,
            filters=filters,
            content_filter=content_filter,
            mode=mode,
            include_common=include_common and bool(cfg.common_enabled),
            common_tenant=cfg.common_tenant,
            common_collection=cfg.common_collection,
            actor="library",
            v=vector,
        )
        return list(_unwrap(result)["matches"])

    def _get_document(self, tenant: str, collection: str, docid: str) -> dict:
        result = self._call(
            svc_get_document,
            tenant,
            collection,
            docid,
        )
        return _unwrap(result)

    def _list_documents(self, tenant: str, collection: str) -> list[dict]:
        result = self._call(svc_list_documents, tenant, collection)
        return list(_unwrap(result)["documents"])

    def _delete_document(self, tenant: str, collection: str, docid: str) -> dict:
        result = self._call(
            svc_delete_document,
            tenant,
            collection,
            docid,
        )
        return _unwrap(result)

    def _collection_detail(self, tenant: str, collection: str) -> dict:
        result = self._call(svc_get_collection_detail, tenant, collection)
        return _unwrap(result)

    def _list_chunks(
        self,
        tenant: str,
        collection: str,
        docid: str,
    ) -> list[dict]:
        result = self._call(svc_list_chunks, tenant, collection, docid)
        return list(_unwrap(result)["chunks"])

    def _get_chunk(self, tenant: str, collection: str, rid: str) -> dict:
        result = self._call(svc_get_chunk, tenant, collection, rid)
        return _unwrap(result)

    def _get_chunk_content(
        self,
        tenant: str,
        collection: str,
        rid: str,
    ) -> dict:
        result = self._call(svc_get_chunk_content, tenant, collection, rid)
        return _unwrap(result)

    def _queries(
        self,
        tenant: str,
        collection: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        result = self._call(
            svc_list_query_homes,
            tenant=tenant,
            collection=collection,
            limit=limit,
            offset=offset,
        )
        return list(_unwrap(result)["queries"])

    def _get_query(self, tenant: str, collection: str, qid: str) -> dict:
        result = self._call(
            svc_get_query_log_entry_scoped, tenant, collection, qid
        )
        return _unwrap(result)["query"]

    def _replay(self, tenant: str, collection: str, qid: str) -> list[dict]:
        result = self._call(
            svc_replay_query_scoped, tenant, collection, qid, actor="library"
        )
        return list(_unwrap(result)["matches"])

    def _rename(
        self,
        tenant: str,
        old_name: str,
        new_name: str,
    ) -> None:
        result = self._call(svc_move_collection, tenant, old_name, new_name)
        _unwrap(result)

    def _update_collection(
        self,
        tenant: str,
        collection: str,
        *,
        display_name: str,
    ) -> dict:
        result = self._call(
            svc_update_collection,
            tenant,
            collection,
            display_name=display_name,
        )
        return _unwrap(result)
