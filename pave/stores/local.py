# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations
import errno
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, date, timezone as tz
from pathlib import Path
from threading import Condition, Lock, get_ident
from typing import Any
import time as _time

# Python 3.12 deprecated the default sqlite3 datetime adapters.
# Register explicit ISO adapters so sqlite I/O doesn't warn.
sqlite3.register_adapter(date, date.isoformat)
sqlite3.register_adapter(datetime, datetime.isoformat)

from pave.backends import FaissBackend, VectorBackend
from pave.embedders import Embedder
from pave.filters import lookup_meta, matches_filters, sanit_field, sanit_meta_dict
from pave.metadb import CatalogDB, CollectionDB, UnsupportedSchemaVersionError
from pave.stores.base import (
    BaseStore,
    MetadataValidationError,
    Record,
    SearchOutput,
    SearchResult,
)
from pave.config import get_cfg, get_logger

log = get_logger()


class _StoreStateLock:
    """Reentrant reader/writer lock for whole-data-dir replacement.

    Read-to-write upgrade is not supported: a thread holding read_lock that
    calls write_lock will deadlock waiting for itself to release.
    """

    def __init__(self) -> None:
        self._cond = Condition(Lock())
        self._readers: dict[int, int] = {}
        self._writer: int | None = None
        self._write_depth = 0
        self._pending_writers = 0

    @contextmanager
    def read_lock(self) -> Iterator[None]:
        tid = get_ident()
        with self._cond:
            while (
                (self._writer is not None and self._writer != tid)
                or (
                    self._pending_writers > 0
                    and self._writer != tid
                    and tid not in self._readers
                )
            ):
                self._cond.wait()
            self._readers[tid] = self._readers.get(tid, 0) + 1
        try:
            yield
        finally:
            with self._cond:
                depth = self._readers[tid] - 1
                if depth:
                    self._readers[tid] = depth
                else:
                    self._readers.pop(tid, None)
                if not self._readers:
                    self._cond.notify_all()

    @contextmanager
    def write_lock(self) -> Iterator[None]:
        tid = get_ident()
        with self._cond:
            if self._writer == tid:
                self._write_depth += 1
            else:
                self._pending_writers += 1
                try:
                    while self._writer is not None or self._readers:
                        self._cond.wait()
                    self._writer = tid
                    self._write_depth = 1
                finally:
                    self._pending_writers -= 1
        try:
            yield
        finally:
            with self._cond:
                if self._writer != tid:
                    raise RuntimeError("state write lock released by non-owner")
                self._write_depth -= 1
                if self._write_depth == 0:
                    self._writer = None
                    self._cond.notify_all()


class LocalStore(BaseStore):
    def __init__(
        self,
        data_dir: str,
        embedder: Embedder,
        cat_db: CatalogDB | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._embedder = embedder
        self._emb: dict[tuple[str, str], VectorBackend] = {}
        self._dbs: dict[tuple[str, str], CollectionDB] = {}
        self._catalog_factory = cat_db.__class__ if cat_db is not None else CatalogDB
        self._catalog = cat_db or self._catalog_factory()
        self._catalog_path: Path | None = None
        self._catalog_guard = Lock()
        self._locks: dict[str, Lock] = {}
        self._locks_guard = Lock()
        self._state_lock = _StoreStateLock()
        self._ensure_catalog()

    def _get_lock(self, key: str) -> Lock:
        if key not in self._locks:
            with self._locks_guard:
                if key not in self._locks:
                    self._locks[key] = Lock()
        return self._locks[key]

    @contextmanager
    def _collection_lock(self, tenant: str, collection: str) -> Iterator[None]:
        with self._state_lock.read_lock():
            lock = self._get_lock(f"t_{tenant}:c_{collection}")
            lock.acquire()
            try:
                yield
            finally:
                lock.release()

    def _base_path(self, tenant: str, collection: str) -> str:
        return os.path.join(self._data_dir, f"t_{tenant}", f"c_{collection}")

    def _db_path(self, tenant: str, collection: str) -> Path:
        return Path(self._base_path(tenant, collection)) / "meta.db"

    def _catalog_db_path(self) -> Path:
        return Path(self._data_dir).resolve() / "catalog.db"

    def _default_collection_config(self) -> dict[str, Any]:
        cfg = get_cfg()
        backend_type = str(cfg.get("vector_store.type")).lower()
        embedder_type = str(cfg.get("embedder.type")).lower()
        embed_model = cfg.get(f"embedder.{embedder_type}.model")
        return {
            "backend_type": backend_type,
            "backend_config": {},
            "embedder_type": embedder_type,
            "embed_model": str(embed_model) if embed_model is not None else None,
            "embedder_config": {},
        }

    @staticmethod
    def _is_transient_catalog_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        if isinstance(exc, sqlite3.ProgrammingError):
            return "closed database" in msg
        if isinstance(exc, sqlite3.OperationalError):
            return (
                "unable to open database file" in msg
                or "database is locked" in msg
                or "no such table:" in msg
            )
        if isinstance(exc, RuntimeError):
            return (
                "catalogdb not opened" in msg
                or "catalogdb is closing" in msg
                or "not opened" in msg
                or "closing" in msg
                or "closed" in msg
            )
        return False

    def _with_catalog_retry(self, op_name: str, fn):
        with self._state_lock.read_lock():
            last_exc: Exception | None = None
            for attempt in range(2):
                catalog = self._ensure_catalog()
                try:
                    return fn(catalog)
                except Exception as e:
                    if not self._is_transient_catalog_error(e):
                        raise
                    last_exc = e
                    log.debug(
                        "Transient catalog %s failure (attempt %s/2): %s",
                        op_name,
                        attempt + 1,
                        e,
                    )
            assert last_exc is not None
            raise last_exc

    def _ensure_catalog(self) -> CatalogDB:
        catalog_path = self._catalog_db_path()
        cat = self._catalog
        if self._catalog_path == catalog_path and cat._conn is not None:
            return cat
        with self._catalog_guard:
            if (
                self._catalog_path == catalog_path
                and self._catalog._conn is not None
            ):
                return self._catalog

            if self._catalog._conn is not None:
                try:
                    self._catalog.close()
                except Exception:
                    pass

            self._catalog.open(catalog_path)
            defaults = self._default_collection_config()
            seeded, removed = self._catalog.bootstrap(
                Path(self._data_dir),
                backend_type=defaults["backend_type"],
                backend_config=defaults["backend_config"],
                embedder_type=defaults["embedder_type"],
                embed_model=defaults["embed_model"],
                embed_config=defaults["embedder_config"],
            )
            if seeded or removed:
                log.info(
                    "CatalogDB bootstrap: seeded=%s removed_orphans=%s path=%s",
                    seeded,
                    removed,
                    catalog_path,
                )
            self._catalog_path = catalog_path
            return self._catalog

    def _register_catalog_collection(self, tenant: str, name: str) -> None:
        if tenant == "_system":
            return
        defaults = self._default_collection_config()
        self._with_catalog_retry(
            "register_collection",
            lambda catalog: catalog.register_collection(
                tenant,
                name,
                backend_type=defaults["backend_type"],
                backend_config=defaults["backend_config"],
                embedder_type=defaults["embedder_type"],
                embed_model=defaults["embed_model"],
                embed_config=defaults["embedder_config"],
            ),
        )

    def _load_or_init(self, tenant: str, collection: str) -> None:
        key = (tenant, collection)
        if key in self._emb and key in self._dbs:
            return

        base = self._base_path(tenant, collection)
        os.makedirs(base, exist_ok=True)

        backend = self._emb.get(key)
        if backend is None:
            idx_dir = Path(os.path.join(base, "index"))
            backend = FaissBackend(
                self._embedder.dim,
                storage_dir=idx_dir,
            )

            try:
                backend.initialize()
            except Exception:
                log.warning(
                    "Corrupt index at %s for %s/%s, starting fresh",
                    idx_dir,
                    tenant,
                    collection,
                )
                backend = FaissBackend(
                    self._embedder.dim,
                    storage_dir=idx_dir,
                )

        col_db = self._dbs.get(key)
        if col_db is None:
            col_db = CollectionDB()
            col_db.open(self._db_path(tenant, collection))

        self._emb[key] = backend
        self._dbs[key] = col_db

    def _save(self, tenant: str, collection: str) -> None:
        key = (tenant, collection)
        em = self._emb.get(key)
        if not em:
            return
        em.flush()

    def create_collection(self, tenant: str, name: str) -> None:
        with self._collection_lock(tenant, name):
            self._load_or_init(tenant, name)
            self._save(tenant, name)
        self._register_catalog_collection(tenant, name)

    def delete_collection(self, tenant: str, collection: str) -> None:
        with self._collection_lock(tenant, collection):
            key = (tenant, collection)
            backend = self._emb.pop(key, None)
            if backend is not None:
                try:
                    backend.close()
                except Exception:
                    pass
            col_db = self._dbs.pop(key, None)
            if col_db is not None:
                col_db.close()
            path = Path(self._base_path(tenant, collection))
            if path.exists() or path.is_symlink():
                self._remove_path(path)
        if tenant != "_system":
            self._with_catalog_retry(
                "unregister_collection",
                lambda catalog: catalog.unregister_collection(tenant, collection),
            )
            self._with_catalog_retry(
                "purge_query_homes_for_collection",
                lambda catalog: catalog.purge_query_homes_for_collection(
                    tenant,
                    collection,
                ),
            )

    def rename_collection(self, tenant: str, old_name: str, new_name: str) -> None:
        if old_name == new_name:
            raise ValueError(
                f"old and new collection names are the same: {old_name}"
            )

        old_key = (tenant, old_name)
        new_key = (tenant, new_name)
        old_path = self._base_path(tenant, old_name)
        new_path = self._base_path(tenant, new_name)

        # Acquire locks in sorted order to prevent deadlock
        lock_old = self._get_lock(f"t_{tenant}:c_{old_name}")
        lock_new = self._get_lock(f"t_{tenant}:c_{new_name}")
        locks = sorted([lock_old, lock_new], key=id)
        locks[0].acquire()
        locks[1].acquire()
        try:
            # Pre-checks
            if not os.path.isdir(old_path):
                raise ValueError(f"collection '{old_name}' does not exist")
            if os.path.exists(new_path):
                raise ValueError(f"collection '{new_name}' already exists")

            # Close DB for old collection before rename
            old_db = self._dbs.pop(old_key, None)
            if old_db is not None:
                old_db.close()

            # Atomic directory rename
            os.rename(old_path, new_path)

            # Update in-memory cache for vector backends
            if old_key in self._emb:
                self._emb[new_key] = self._emb.pop(old_key)

            # Re-open CollectionDB at new path
            col_db = CollectionDB()
            col_db.open(self._db_path(tenant, new_name))
            self._dbs[new_key] = col_db
        finally:
            locks[1].release()
            locks[0].release()

        if tenant != "_system":
            self._register_catalog_collection(tenant, old_name)
            self._with_catalog_retry(
                "rename_collection",
                lambda catalog: catalog.rename_collection(
                    tenant,
                    old_name,
                    new_name,
                ),
            )
            self._with_catalog_retry(
                "rename_query_homes_for_collection",
                lambda catalog: catalog.rename_query_homes_for_collection(
                    tenant,
                    old_name,
                    new_name,
                ),
            )

    @staticmethod
    def _is_transient_db_read_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        if isinstance(exc, UnsupportedSchemaVersionError):
            return "collection schema version 0" in msg
        if isinstance(exc, sqlite3.ProgrammingError):
            return "closed database" in msg
        if isinstance(exc, sqlite3.OperationalError):
            return (
                "unable to open database file" in msg
                or "database is locked" in msg
                or "no such table:" in msg
            )
        if isinstance(exc, RuntimeError):
            return (
                "not opened" in msg
                or "closing" in msg
                or "closed" in msg
            )
        return False

    def _read_collection_db(
        self,
        tenant: str,
        collection: str,
        *,
        op_name: str,
        default: Any,
        reader,
    ) -> Any:
        with self._state_lock.read_lock():
            key = (tenant, collection)
            col_db = self._dbs.get(key)
            if col_db is not None:
                try:
                    return reader(col_db)
                except Exception as e:
                    if not self._is_transient_db_read_error(e):
                        raise
                    log.debug(
                        "Transient cached %s read failure for %s/%s: %s",
                        op_name,
                        tenant,
                        collection,
                        e,
                    )

            db_path = self._db_path(tenant, collection)
            if not db_path.exists():
                return default

            fallback = CollectionDB()
            try:
                fallback.open(db_path, read_only=True)
                return reader(fallback)
            except Exception as e:
                if not self._is_transient_db_read_error(e):
                    raise
                log.debug(
                    "Transient fallback %s read failure for %s/%s: %s",
                    op_name,
                    tenant,
                    collection,
                    e,
                )
                return default
            finally:
                try:
                    fallback.close()
                except Exception:
                    pass

    def _read_meta_batch_safe(
        self, tenant: str, collection: str, rids: list[str]
    ) -> dict[str, dict[str, Any]]:
        if not rids:
            return {}
        return self._read_collection_db(
            tenant,
            collection,
            op_name="meta_batch",
            default={},
            reader=lambda db: db.get_meta_batch(rids),
        )

    def list_collections(self, tenant: str) -> list[dict[str, Any]]:
        return self._with_catalog_retry(
            "list_collection_summaries",
            lambda catalog: catalog.list_collection_summaries(tenant),
        )

    def get_collection_detail(
        self,
        tenant: str,
        name: str,
    ) -> dict[str, Any] | None:
        cfg = self._with_catalog_retry(
            "get_collection_config",
            lambda catalog: catalog.get_collection_config(tenant, name),
        )
        if cfg is None:
            return None
        doc_count, chunk_count = self._read_doc_chunk_counts(tenant, name)
        return {
            "tenant": tenant,
            "name": name,
            "display_name": cfg.get("display_name"),
            "embedder_type": cfg.get("embedder_type"),
            "embed_model": cfg.get("embed_model"),
            "created_at": cfg.get("created_at"),
            "doc_count": doc_count,
            "chunk_count": chunk_count,
        }

    def list_tenants(self) -> list[str]:
        return self._with_catalog_retry(
            "list_tenants",
            lambda catalog: catalog.list_tenants(),
        )

    def get_collection_config(
        self,
        tenant: str,
        collection: str,
    ) -> dict[str, Any] | None:
        return self._with_catalog_retry(
            "get_collection_config",
            lambda catalog: catalog.get_collection_config(tenant, collection),
        )

    def log_query(
        self,
        *,
        query_id: str,
        tenant: str,
        collection: str,
        actor: str,
        query_text: str,
        k: int,
        filters: dict[str, Any] | None = None,
        include_common: bool = False,
        common_tenant: str | None = None,
        common_collection: str | None = None,
        result_ids: list[str] | None = None,
        result_count: int = 0,
        latency_ms: float | None = None,
        timing: dict[str, float] | None = None,
        request_id: str | None = None,
        replay_of: str | None = None,
    ) -> None:
        with self._collection_lock(tenant, collection):
            self._load_or_init(tenant, collection)
            self._dbs[(tenant, collection)].log_query(
                query_id=query_id,
                tenant=tenant,
                collection=collection,
                actor=actor,
                query_text=query_text,
                k=k,
                filters=filters,
                include_common=include_common,
                common_tenant=common_tenant,
                common_collection=common_collection,
                result_ids=result_ids,
                result_count=result_count,
                latency_ms=latency_ms,
                timing=timing,
                request_id=request_id,
                replay_of=replay_of,
            )
        try:
            self.put_query_home(query_id, tenant, collection)
        except Exception:
            log.warning("query_home upsert failed", exc_info=True)

    def get_query_log_entry(
        self,
        tenant: str,
        collection: str,
        query_id: str,
    ) -> dict[str, Any] | None:
        return self._read_collection_db(
            tenant,
            collection,
            op_name="query_log_entry",
            default=None,
            reader=lambda db: db.get_query_log_entry(query_id),
        )

    def list_query_logs(
        self,
        tenant: str,
        collection: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return self._read_collection_db(
            tenant,
            collection,
            op_name="query_logs",
            default=[],
            reader=lambda db: db.list_query_logs(limit, offset),
        )

    def put_query_home(
        self,
        query_id: str,
        tenant: str,
        collection: str,
    ) -> None:
        self._with_catalog_retry(
            "put_query_home",
            lambda catalog: catalog.put_query_home(query_id, tenant, collection),
        )

    def resolve_query_home(
        self,
        query_id: str,
    ) -> tuple[str, str] | None:
        return self._with_catalog_retry(
            "resolve_query_home",
            lambda catalog: catalog.resolve_query_home(query_id),
        )

    def purge_query_homes_for_collection(
        self,
        tenant: str,
        collection: str,
    ) -> None:
        self._with_catalog_retry(
            "purge_query_homes_for_collection",
            lambda catalog: catalog.purge_query_homes_for_collection(
                tenant,
                collection,
            ),
        )

    def list_query_homes(
        self,
        tenant: str | None = None,
        collection: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return self._with_catalog_retry(
            "list_query_homes",
            lambda catalog: catalog.list_query_homes(
                tenant=tenant,
                collection=collection,
                limit=limit,
                offset=offset,
            ),
        )

    def _read_doc_chunk_counts(
        self,
        tenant: str,
        collection: str,
    ) -> tuple[int, int]:
        """Return (doc_count, chunk_count) for an existing collection.

        Returns (0, 0) if meta.db is missing or hits a transient
        read error.
        """
        return self._read_collection_db(
            tenant,
            collection,
            op_name="doc_chunk_counts",
            default=(0, 0),
            reader=lambda db: db.get_doc_chunk_counts(),
        )

    def catalog_metrics(self) -> dict[str, int]:
        """Return tenant/collection/doc/chunk counters from store metadata."""
        doc_count = 0
        chunk_count = 0

        refs = self._with_catalog_retry(
            "list_collection_refs",
            lambda catalog: catalog.list_collection_refs(),
        )
        for tenant, collection in refs:
            docs, chunks = self._read_doc_chunk_counts(tenant, collection)
            doc_count += docs
            chunk_count += chunks

        return {
            "tenant_count": self._with_catalog_retry(
                "tenant_count",
                lambda catalog: catalog.tenant_count(),
            ),
            "collection_count": self._with_catalog_retry(
                "collection_count",
                lambda catalog: catalog.collection_count(),
            ),
            "doc_count": doc_count,
            "chunk_count": chunk_count,
        }

    def has_doc(self, tenant: str, collection: str, docid: str) -> bool:
        return self._read_collection_db(
            tenant,
            collection,
            op_name="has_doc",
            default=False,
            reader=lambda db: db.has_doc(docid),
        )

    def get_document(
        self,
        tenant: str,
        collection: str,
        docid: str,
    ) -> dict[str, Any] | None:
        return self._read_collection_db(
            tenant,
            collection,
            op_name="document",
            default=None,
            reader=lambda db: db.get_document(docid),
        )

    def list_chunks(
        self,
        tenant: str,
        collection: str,
        docid: str,
    ) -> list[dict[str, Any]]:
        return self._read_collection_db(
            tenant,
            collection,
            op_name="chunks",
            default=[],
            reader=lambda db: db.list_chunks(docid),
        )

    def get_chunk(
        self,
        tenant: str,
        collection: str,
        rid: str,
    ) -> dict[str, Any] | None:
        chunk = self._read_collection_db(
            tenant,
            collection,
            op_name="chunk",
            default=None,
            reader=lambda db: db.get_chunk(rid),
        )
        if chunk is None:
            return None
        chunk["tenant"] = tenant
        chunk["collection"] = collection
        return chunk

    def get_chunk_content(
        self,
        tenant: str,
        collection: str,
        rid: str,
    ) -> dict[str, Any] | None:
        chunk = self.get_chunk(tenant, collection, rid)
        if chunk is None:
            return None
        text = self._load_chunk_text(tenant, collection, rid)
        if text is None:
            return None
        return {
            "content": text.encode("utf-8"),
            "content_type": "text/plain; charset=utf-8",
        }

    def list_documents(
        self,
        tenant: str,
        collection: str,
    ) -> list[dict[str, Any]]:
        return self._read_collection_db(
            tenant,
            collection,
            op_name="documents",
            default=[],
            reader=lambda db: db.list_documents(),
        )

    def purge_doc(self, tenant: str, collection: str, docid: str) -> int:
        with self._collection_lock(tenant, collection):
            self._load_or_init(tenant, collection)
            key = (tenant, collection)
            col_db = self._dbs[key]
            ids = col_db.get_rids_for_doc(docid)
            if not ids:
                return 0

            # remove sidecar .txt files
            for urid in ids:
                p = os.path.join(
                    self._chunks_dir(tenant, collection),
                    self._urid_to_fname(urid)
                )
                if os.path.isfile(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass

            # delete from SQLite (chunks + documents rows)
            col_db.delete_doc(docid)

            # delete vectors for these chunk ids
            backend = self._emb.get(key)
            if backend and ids:
                try:
                    backend.delete(ids)
                except Exception:
                    # Skip silently. Metadata-side cleanup already happened
                    # and searches hydrate text from sidecars.
                    pass

            self._save(tenant, collection)
            return len(ids)

    def _chunks_dir(self, tenant: str, collection: str) -> str:
        return os.path.join(self._base_path(tenant, collection), "chunks")

    def _urid_to_fname(self, urid: str) -> str:
        return (
            urid.replace("/", "_").replace("\\", "_").replace(":", "_") + ".txt"
        )

    def _save_chunk_text(self, tenant: str, collection: str,
                         urid: str, t: str) -> None:
        p = os.path.join(self._chunks_dir(tenant, collection),
                         self._urid_to_fname(urid))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        data = (t or "").encode("utf-8")
        with open(p, "wb") as f:
            f.write(data)
            f.flush()

    def _load_chunk_text(self, tenant: str, collection: str,
                         urid: str) -> str | None:
        p = os.path.join(self._chunks_dir(tenant, collection),
                         self._urid_to_fname(urid))
        if not os.path.isfile(p):
            return None
        try:
            with open(p, "rb") as f:
                return f.read().decode("utf-8")
        except FileNotFoundError:
            # TOCTOU: file was removed between isfile() and open().
            return None
        except OSError:
            return None

    def index_records(self, tenant: str, collection: str, docid: str,
                      records: Iterable[Record],
                      doc_meta: dict[str, Any] | None = None
                      ) -> int:
        """
        Ingests records as (rid, text, meta). Guarantees non-null text, coerces
        dict-records, updates SQLite metadata, saves index. Thread critical.
        """
        chunk_count = 0
        with self._collection_lock(tenant, collection):
            self._load_or_init(tenant, collection)
            key = (tenant, collection)
            col_db = self._dbs[key]
            backend = self._emb[key]
            raw_doc_meta = dict(doc_meta or {})
            raw_doc_meta.setdefault("docid", docid)
            try:
                safe_doc_meta = sanit_meta_dict(raw_doc_meta)
            except MetadataValidationError:
                raise
            except Exception:
                safe_doc_meta = {"docid": docid}
            rids_to_add: list[str] = []
            texts_to_encode: list[str] = []
            chunk_rows: list[tuple[str, str | None, dict[str, Any]]] = []

            for r in records:
                if isinstance(r, dict):
                    rid = r.get("rid") or r.get("id") or r.get("uid")
                    txt = r.get("text") or r.get("content")
                    md = (
                        r.get("meta") or r.get("metadata") or
                        r.get("tags") or {}
                    )
                else:
                    try:
                        rid, txt, md = r
                    except Exception:
                        continue

                if not rid or txt is None:
                    continue

                if not isinstance(md, dict):
                    if isinstance(md, str):
                        try:
                            md = json.loads(md)
                        except:
                            md = {}
                    else:
                        try:
                            md = dict(md)
                        except:
                            md = {}

                try:
                    safe_meta = sanit_meta_dict(md)
                except MetadataValidationError:
                    raise
                except Exception:
                    safe_meta = {}

                rid = str(rid)
                txt = str(txt)
                if not rid.startswith(f"{docid}::"):
                    rid = f"{docid}::{rid}"

                chunk_path = os.path.join(
                    "chunks", self._urid_to_fname(rid)
                )
                chunk_rows.append((rid, chunk_path, safe_meta))
                rids_to_add.append(rid)
                texts_to_encode.append(txt)

                self._save_chunk_text(tenant, collection, rid, txt)
                loaded = self._load_chunk_text(tenant, collection, rid) or ""
                if txt != loaded:
                    log.warning(
                        f"Chunk text round-trip mismatch for {rid}: "
                        f"saved {len(txt)} chars, loaded {len(loaded)} chars"
                    )

            if not rids_to_add:
                return 0

            # Write metadata to SQLite (inside collection_lock)
            col_db.upsert_chunks(docid, chunk_rows, doc_meta=safe_doc_meta)
            vectors = self._embedder.encode(texts_to_encode)
            backend.add(rids_to_add, vectors)
            self._save(tenant, collection)
            _rids = rids_to_add[:3]
            _sfx = " ..." if len(rids_to_add) > 3 else ""
            log.debug(
                f"INGEST-PREPARED: {len(rids_to_add)} chunks {_rids}{_sfx}"
            )
            chunk_count = len(rids_to_add)

        if chunk_count:
            self._register_catalog_collection(tenant, collection)
        return chunk_count

    def search(self, tenant: str, collection: str, query: str, k: int = 5,
               filters: dict[str, Any] | None = None) -> SearchOutput:
        """
        Queries the FAISS backend for top-k and keeps overfetch inside the
        store.

        Search keeps metadata reads inside collection_lock so collection
        deletion cannot close the cached SQLite handle while results are being
        hydrated.
        """
        _perf = _time.perf_counter

        def _ms_since(t0: float) -> float:
            return round((_perf() - t0) * 1000, 2)

        kk = max(1, int(k))

        fetch_k = max(50, kk * 5)
        normed_filters: dict[str, list[Any]] = {}
        for key, vals in (filters or {}).items():
            safe_key = sanit_field(key)
            if not safe_key:
                continue
            if isinstance(vals, list):
                normed_filters[safe_key] = vals
            else:
                normed_filters[safe_key] = [vals]

        with self._collection_lock(tenant, collection):
            self._load_or_init(tenant, collection)
            key = (tenant, collection)
            backend = self._emb[key]
            col_db = self._dbs.get(key)

            t0 = _perf()
            q_vec = self._embedder.encode([query])[0]
            embed_ms = _ms_since(t0)

            t0 = _perf()
            raw = backend.search(q_vec, fetch_k)
            candidate_rids = [rid for rid, _ in raw if rid]
            search_ms = _ms_since(t0)

            filter_push_ms = 0.0
            if normed_filters and col_db is not None:
                t0 = _perf()
                surviving = col_db.filter_by_meta(
                    candidate_rids,
                    normed_filters,
                )
                raw = [(rid, score) for rid, score in raw if rid in surviving]
                candidate_rids = [rid for rid, _score in raw if rid]
                filter_push_ms = _ms_since(t0)

            t0 = _perf()
            meta_batch = self._read_meta_batch_safe(
                tenant,
                collection,
                candidate_rids,
            )
            hydrate_meta_ms = _ms_since(t0)

            kept: list[tuple[str, float]] = []
            if normed_filters:
                log.debug(f"SEARCH-FILTER-POST: {normed_filters}")
            t0 = _perf()
            for rid, score in raw:
                if not rid:
                    continue
                rid_meta = meta_batch.get(rid, {})
                if matches_filters(rid_meta, normed_filters):
                    kept.append((rid, score))
                    if len(kept) >= kk:
                        break
            filter_post_ms = _ms_since(t0)

            out: list[SearchResult] = []
            t0 = _perf()
            for rid, score in kept:
                txt = self._load_chunk_text(tenant, collection, rid)
                rid_meta = meta_batch.get(rid, {})
                out.append(SearchResult(
                    id=rid,
                    score=score,
                    text=txt,
                    tenant=tenant,
                    collection=collection,
                    meta=rid_meta,
                    match_reason=self._build_match_reason(
                        query, score, filters, rid_meta
                    ),
                ))
            hydrate_text_ms = _ms_since(t0)
            _hits = [(r.id, round(r.score, 3)) for r in out[:3]]
            _sfx = " ..." if len(out) > 3 else ""
            log.debug(f"SEARCH-OUT: {len(out)} hits {_hits}{_sfx}")
            return SearchOutput(
                matches=out,
                timing={
                    "embed_ms": embed_ms,
                    "search_ms": search_ms,
                    "filter_ms": round(filter_push_ms + filter_post_ms, 2),
                    "hydrate_ms": round(hydrate_meta_ms + hydrate_text_ms, 2),
                },
            )

    def _build_match_reason(self, query: str, score: float,
                            filters: dict[str, Any] | None,
                            meta: dict[str, Any] | None) -> str:
        """Build a human-readable explanation of why a result matched."""
        parts = []

        # Similarity component
        pct = int(score * 100)
        if query:
            parts.append(f"semantic similarity {pct}%")

        # Filter matches - show which filter conditions were satisfied
        if filters:
            filter_parts = []
            for key, vals in filters.items():
                meta_val = lookup_meta(meta, key)
                if meta_val is not None:
                    # Show the actual value that matched
                    filter_parts.append(f"{key}={meta_val}")
            if filter_parts:
                parts.append("filters: " + ", ".join(filter_parts))

        return "; ".join(parts) if parts else "matched"

    def _flush_caches(self, *, async_close: bool = True) -> None:
        old_dbs = list(self._dbs.values())
        old_backends = list(self._emb.values())
        with self._catalog_guard:
            old_catalog = self._catalog
            self._catalog = self._catalog_factory()
            self._catalog_path = None
        self._dbs.clear()
        self._emb.clear()

        if old_catalog._conn is not None:
            try:
                old_catalog.close()
            except Exception:
                pass

        if not old_dbs and not old_backends:
            return

        def _close_all() -> None:
            for db in old_dbs:
                try:
                    db.close()
                except Exception:
                    pass
            for backend in old_backends:
                try:
                    backend.close()
                except Exception:
                    pass

        if async_close:
            import threading

            threading.Thread(target=_close_all, daemon=True).start()
        else:
            _close_all()

    def _write_zip(self, source_dir: Path, target_path: Path) -> None:
        source_dir = source_dir.resolve()
        target_path = target_path.resolve()
        if not source_dir.is_dir():
            raise FileNotFoundError(f"data directory not found: {source_dir}")

        target_path.parent.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(
            target_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as zf:
            for root, dirs, files in os.walk(source_dir):
                root_path = Path(root)
                rel_root = root_path.relative_to(source_dir)
                if not files and not dirs:
                    arcname = (
                        str(rel_root) + "/"
                        if rel_root != Path(".")
                        else ""
                    )
                    if arcname:
                        zf.writestr(arcname, "")
                    continue

                for filename in files:
                    # SQLite WAL/SHM sidecars are tied to the live connection;
                    # bundling them risks a restored .db-wal/.db-shm pointing at
                    # state the main .db no longer matches, which surfaces as
                    # "disk I/O error" / "file is not a database" on next open.
                    if filename.endswith(("-wal", "-shm")):
                        continue
                    file_path = root_path / filename
                    rel_name = file_path.relative_to(source_dir)
                    try:
                        zf.write(file_path, rel_name.as_posix())
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        if exc.errno == errno.ENOENT:
                            continue
                        raise

    def _validate_zip_members(self, zf: zipfile.ZipFile) -> None:
        for member in zf.infolist():
            name = member.filename
            if not name:
                continue
            rel_path = Path(name)
            if rel_path.is_absolute() or ".." in rel_path.parts:
                raise ValueError(f"invalid archive member: {name}")
            if name.startswith(("/", "\\")):
                raise ValueError(f"invalid archive member: {name}")

    @staticmethod
    def _remove_path(path: Path, *, retries: int = 4) -> None:
        for attempt in range(retries):
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                return
            except FileNotFoundError:
                return
            except OSError as exc:
                transient = {
                    errno.ENOENT,
                    errno.ENOTEMPTY,
                    errno.EBUSY,
                }
                if exc.errno in transient and attempt < (retries - 1):
                    import time

                    time.sleep(0.02 * (attempt + 1))
                    continue
                raise

    def dump_archive(
        self,
        output_path: str | os.PathLike[str] | None = None,
    ) -> tuple[str, str | None]:
        with self._state_lock.write_lock():
            data_dir_path = Path(self._data_dir).resolve()
            if not data_dir_path.is_dir():
                raise FileNotFoundError(
                    f"data directory not found: {data_dir_path}"
                )
            self._flush_caches(async_close=False)

            if output_path is not None:
                archive_path = Path(output_path).resolve()
                self._write_zip(data_dir_path, archive_path)
                return str(archive_path), None

            tmp_dir = Path(tempfile.mkdtemp(prefix="pavedb_export_"))
            timestamp = datetime.now(tz.utc).strftime("%Y%m%dT%H%M%SZ")
            archive_path = tmp_dir / f"pavedb-data-{timestamp}.zip"
            self._write_zip(data_dir_path, archive_path)
            return str(archive_path), str(tmp_dir)

    def restore_archive(self, archive_bytes: bytes) -> None:
        with self._state_lock.write_lock():
            data_dir_path = Path(self._data_dir).resolve()
            data_dir_path.mkdir(parents=True, exist_ok=True)

            with tempfile.TemporaryDirectory(prefix="pavedb_import_") as tmp_dir:
                tmp_path = Path(tmp_dir)
                archive_path = tmp_path / "pavedb-data.zip"
                extract_dir = tmp_path / "extracted"
                archive_path.write_bytes(archive_bytes)
                extract_dir.mkdir()

                with zipfile.ZipFile(archive_path, "r") as zf:
                    self._validate_zip_members(zf)
                    zf.extractall(extract_dir)

                self._flush_caches(async_close=False)
                for entry in data_dir_path.iterdir():
                    self._remove_path(entry)

                for entry in extract_dir.iterdir():
                    target = data_dir_path / entry.name
                    if target.exists() or target.is_symlink():
                        self._remove_path(target)
                    shutil.move(str(entry), str(target))
