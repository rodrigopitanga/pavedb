# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pave.embedders as embedders
from pave.config import get_cfg
from pave.embedders import LazyEmbedder
from pave.service import (
    ServiceError,
    create_collection as svc_create_collection,
    delete_collection as svc_delete_collection,
    delete_document as svc_delete_document,
    dump_archive as svc_dump_archive,
    get_chunk as svc_get_chunk,
    get_chunk_content as svc_get_chunk_content,
    get_collection_detail as svc_get_collection_detail,
    get_document as svc_get_document,
    get_query_log_entry as svc_get_query_log_entry,
    ingest_documents_batch as svc_ingest_documents_batch,
    ingest_document as svc_ingest_document,
    ingest_text as svc_ingest_text,
    list_chunks as svc_list_chunks,
    list_collections as svc_list_collections,
    list_documents as svc_list_documents,
    list_query_homes as svc_list_query_homes,
    list_tenants as svc_list_tenants,
    move_collection as svc_move_collection,
    replay_query as svc_replay_query,
    restore_archive as svc_restore_archive,
    search as svc_search,
    update_collection as svc_update_collection,
)
from pave.stores.base import BaseStore, FilterSpec, Metadata
from pave.stores.local import LocalStore


class PaveError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class NotFoundError(PaveError):
    pass


class InvalidRequest(PaveError):
    pass


class Conflict(PaveError):
    pass


class Unavailable(PaveError):
    pass


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
    if code.startswith("invalid_") or code.endswith("_invalid"):
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


def _unwrap(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("ok") is False:
        _raise_result(result)
    return {k: v for k, v in result.items() if k != "ok"}


def _store_for_path(path: str | os.PathLike[str]) -> BaseStore:
    return LocalStore(
        data_dir=str(path),
        embedder=LazyEmbedder(embedders.get_embedder),
    )


def _batch_item(document: object) -> dict[str, Any]:
    if isinstance(document, str):
        return {"text": document}
    if isinstance(document, Mapping):
        return {
            "text": document.get("text"),
            "docid": document.get("docid"),
            "metadata": document.get("metadata"),
        }
    if isinstance(document, (tuple, list)):
        if not 1 <= len(document) <= 3:
            raise InvalidRequest(
                "invalid_batch_item",
                "batch items must be text, (text, docid, metadata), or a dict",
            )
        text = document[0]
        docid = document[1] if len(document) > 1 else None
        metadata = document[2] if len(document) > 2 else None
        return {"text": text, "docid": docid, "metadata": metadata}
    raise InvalidRequest(
        "invalid_batch_item",
        "batch items must be text, (text, docid, metadata), or a dict",
    )


def connect(target: str | os.PathLike[str] | None = None) -> LocalClient:
    if target is None:
        return LocalClient._ephemeral()
    raw = os.fspath(target)
    parsed = urlparse(raw)
    if parsed.scheme in ("http", "https"):
        raise InvalidRequest(
            "http_client_unavailable",
            "HTTP client lands in v1.1",
        )
    if parsed.scheme and len(parsed.scheme) > 1:
        raise InvalidRequest(
            "unsupported_scheme",
            f"unsupported connect() scheme: {parsed.scheme}",
        )
    if raw in ("", ":memory:"):
        return LocalClient._ephemeral()
    return LocalClient(raw)


class LocalClient:
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
        self._store = store or _store_for_path(self.path)
        self._ephemeral = _ephemeral
        self._closed = False

    @classmethod
    def _ephemeral(cls, *, tenant: str = "default") -> LocalClient:
        path = tempfile.mkdtemp(prefix="pavedb-")
        return cls(path, tenant=tenant, _ephemeral=True)

    def __enter__(self) -> LocalClient:
        self._ensure_open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        flush = getattr(self._store, "_flush_caches", None)
        if callable(flush):
            flush(async_close=False)
        close = getattr(self._store, "close", None)
        if callable(close):
            close()
        if self._ephemeral:
            shutil.rmtree(self.path, ignore_errors=True)
        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise PaveError("client_closed", "client is closed")

    def _tenant(self, tenant: str | None) -> str:
        return tenant or self.tenant

    def _call(self, func, *args, **kwargs):
        self._ensure_open()
        try:
            return func(self._store, *args, **kwargs)
        except ServiceError as exc:
            raise _translate_service_error(exc) from exc

    def create_collection(
        self,
        name: str,
        *,
        tenant: str | None = None,
        display_name: str | None = None,
        embedder_type: str | None = None,
        embed_model: str | None = None,
        embedder_config: Mapping[str, Any] | None = None,
    ) -> Collection:
        result = self._call(
            svc_create_collection,
            self._tenant(tenant),
            name,
            display_name=display_name,
            embedder_type=embedder_type,
            embed_model=embed_model,
            embedder_config=(
                dict(embedder_config)
                if embedder_config is not None
                else None
            ),
        )
        _unwrap(result)
        return self.collection(name, tenant=tenant)

    def collection(self, name: str, *, tenant: str | None = None) -> Collection:
        self._ensure_open()
        return Collection(self, self._tenant(tenant), name)

    def list_collections(self, *, tenant: str | None = None) -> list[Collection]:
        result = self._call(svc_list_collections, self._tenant(tenant))
        data = _unwrap(result)
        return [
            Collection(self, self._tenant(tenant), str(item["name"]))
            for item in data["collections"]
        ]

    def list_tenants(self) -> list[str]:
        result = self._call(svc_list_tenants)
        return list(_unwrap(result)["tenants"])

    def delete_collection(self, name: str, *, tenant: str | None = None) -> dict:
        result = self._call(svc_delete_collection, self._tenant(tenant), name)
        return _unwrap(result)

    def dump_archive(self, path: str | os.PathLike[str] | None = None) -> str:
        self._ensure_open()
        try:
            archive_path, _source = svc_dump_archive(self._store, path)
        except ServiceError as exc:
            raise _translate_service_error(exc) from exc
        return archive_path

    def restore_archive(self, archive_bytes: bytes) -> dict:
        result = self._call(svc_restore_archive, archive_bytes)
        return _unwrap(result)

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
        text: str,
        *,
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
            [_batch_item(document) for document in documents],
        )
        return _unwrap(result)

    def _search(
        self,
        tenant: str,
        collection: str,
        q: str,
        k: int = 5,
        *,
        filters: FilterSpec | None = None,
        include_common: bool = False,
    ) -> list[dict]:
        cfg = get_cfg()
        result = self._call(
            svc_search,
            tenant,
            collection,
            q,
            k,
            filters=filters,
            include_common=include_common and bool(cfg.common_enabled),
            common_tenant=cfg.common_tenant,
            common_collection=cfg.common_collection,
            actor="library",
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

    def _get_query(self, qid: str) -> dict:
        result = self._call(svc_get_query_log_entry, qid)
        return _unwrap(result)["query"]

    def _replay(self, qid: str) -> list[dict]:
        result = self._call(svc_replay_query, qid, actor="library")
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


class Collection:
    def __init__(self, client: LocalClient, tenant: str, name: str) -> None:
        self.client = client
        self.tenant = tenant
        self.name = name

    def __enter__(self) -> Collection:
        self.client._ensure_open()
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def ingest(
        self,
        file: str | os.PathLike[str],
        *,
        docid: str | None = None,
        metadata: Metadata | None = None,
        csv_options: dict[str, Any] | None = None,
    ) -> dict:
        return self.client._ingest(
            self.tenant,
            self.name,
            file,
            docid=docid,
            metadata=metadata,
            csv_options=csv_options,
        )

    def add(
        self,
        text: str,
        *,
        docid: str | None = None,
        metadata: Metadata | None = None,
    ) -> dict:
        return self.client._add(
            self.tenant,
            self.name,
            text,
            docid=docid,
            metadata=metadata,
        )

    def add_many(self, documents: list[object]) -> dict:
        return self.client._add_many(self.tenant, self.name, documents)

    def search(
        self,
        q: str,
        k: int = 5,
        *,
        filters: FilterSpec | None = None,
        include_common: bool = False,
    ) -> list[dict]:
        return self.client._search(
            self.tenant,
            self.name,
            q,
            k,
            filters=filters,
            include_common=include_common,
        )

    def get(self, docid: str) -> dict:
        return self.client._get_document(self.tenant, self.name, docid)

    def list_documents(self) -> list[dict]:
        return self.client._list_documents(self.tenant, self.name)

    def delete(self, docid: str) -> dict:
        return self.client._delete_document(self.tenant, self.name, docid)

    def detail(self) -> dict:
        return self.client._collection_detail(self.tenant, self.name)

    def list_chunks(self, docid: str) -> list[dict]:
        return self.client._list_chunks(self.tenant, self.name, docid)

    def get_chunk(self, rid: str) -> dict:
        return self.client._get_chunk(self.tenant, self.name, rid)

    def get_chunk_content(self, rid: str) -> dict:
        return self.client._get_chunk_content(self.tenant, self.name, rid)

    def queries(self, limit: int = 50, offset: int = 0) -> list[dict]:
        return self.client._queries(self.tenant, self.name, limit, offset)

    def get_query(self, qid: str) -> dict:
        return self.client._get_query(qid)

    def replay(self, qid: str) -> list[dict]:
        return self.client._replay(qid)

    def rename(self, new_name: str) -> Collection:
        self.client._rename(self.tenant, self.name, new_name)
        self.name = new_name
        return self

    def update(self, *, display_name: str) -> dict:
        return self.client._update_collection(
            self.tenant,
            self.name,
            display_name=display_name,
        )
