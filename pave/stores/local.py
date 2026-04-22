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
from threading import Lock
from typing import Any
import time as _time

# Python 3.12 deprecated the default sqlite3 datetime adapters.
# Register explicit ISO adapters so sqlite I/O doesn't warn.
sqlite3.register_adapter(date, date.isoformat)
sqlite3.register_adapter(datetime, datetime.isoformat)

from pave.backends import FaissBackend, VectorBackend
from pave.embedders import Embedder
from pave.filters import lookup_meta, matches_filters, sanit_field, sanit_meta_dict
from pave.metadb import CatalogDB, CollectionDB
from pave.stores.base import (
    BaseStore,
    MetadataValidationError,
    Record,
    SearchOutput,
    SearchResult,
)
from pave.config import get_cfg, get_logger

log = get_logger()

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
        self._catalog = cat_db or CatalogDB()
        self._catalog_path: Path | None = None
        self._catalog_guard = Lock()
        self._locks: dict[str, Lock] = {}
        self._locks_guard = Lock()
        self._ensure_catalog()

    def _get_lock(self, key: str) -> Lock:
        if key not in self._locks:
            with self._locks_guard:
                if key not in self._locks:
                    self._locks[key] = Lock()
        return self._locks[key]

    @contextmanager
    def _collection_lock(self, tenant: str, collection: str) -> Iterator[None]:
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

    def _ensure_catalog(self) -> CatalogDB:
        with self._catalog_guard:
            catalog_path = self._catalog_db_path()
            if self._catalog_path == catalog_path and self._catalog._conn is not None:
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
        self._ensure_catalog().register_collection(
            tenant,
            name,
            backend_type=defaults["backend_type"],
            backend_config=defaults["backend_config"],
            embedder_type=defaults["embedder_type"],
            embed_model=defaults["embed_model"],
            embed_config=defaults["embedder_config"],
        )

    def _load_or_init(self, tenant: str, collection: str) -> None:
        key = (tenant, collection)
        if key in self._emb:
            return

        base = self._base_path(tenant, collection)
        os.makedirs(base, exist_ok=True)

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

        self._emb[key] = backend

        # Open CollectionDB if not already open
        if key not in self._dbs:
            col_db = CollectionDB()
            col_db.open(self._db_path(tenant, collection))
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
            catalog = self._ensure_catalog()
            catalog.unregister_collection(tenant, collection)
            catalog.purge_query_homes_for_collection(tenant, collection)

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
            catalog = self._ensure_catalog()
            catalog.rename_collection(tenant, old_name, new_name)
            catalog.rename_query_homes_for_collection(
                tenant,
                old_name,
                new_name,
            )

    @staticmethod
    def _is_transient_db_read_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        if isinstance(exc, sqlite3.ProgrammingError):
            return "closed database" in msg
        if isinstance(exc, sqlite3.OperationalError):
            return (
                "unable to open database file" in msg
                or "database is locked" in msg
            )
        if isinstance(exc, RuntimeError):
            return (
                "not opened" in msg
                or "closing" in msg
                or "closed" in msg
            )
        return False

    def _read_meta_batch_safe(
        self, tenant: str, collection: str, rids: list[str]
    ) -> dict[str, dict[str, Any]]:
        if not rids:
            return {}

        key = (tenant, collection)
        cached = self._dbs.get(key)
        if cached is not None:
            try:
                return cached.get_meta_batch(rids)
            except Exception as e:
                if not self._is_transient_db_read_error(e):
                    raise
                log.debug(
                    "Transient cached meta read failure for %s/%s: %s",
                    tenant, collection, e,
                )

        db_path = self._db_path(tenant, collection)
        if not db_path.exists():
            return {}

        fallback = CollectionDB()
        try:
            fallback.open(db_path, read_only=True)
            return fallback.get_meta_batch(rids)
        except Exception as e:
            if not self._is_transient_db_read_error(e):
                raise
            log.debug(
                "Transient fallback meta read failure for %s/%s: %s",
                tenant, collection, e,
            )
            return {}
        finally:
            try:
                fallback.close()
            except Exception:
                pass

    def list_collections(self, tenant: str) -> list[dict[str, Any]]:
        return self._ensure_catalog().list_collection_summaries(tenant)

    def get_collection_detail(
        self,
        tenant: str,
        name: str,
    ) -> dict[str, Any] | None:
        catalog = self._ensure_catalog()
        cfg = catalog.get_collection_config(tenant, name)
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
        return self._ensure_catalog().list_tenants()

    def get_collection_config(
        self,
        tenant: str,
        collection: str,
    ) -> dict[str, Any] | None:
        return self._ensure_catalog().get_collection_config(tenant, collection)

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
        key = (tenant, collection)
        col_db = self._dbs.get(key)
        close_after = False
        if col_db is None:
            db_path = self._db_path(tenant, collection)
            if not db_path.exists():
                return None
            col_db = CollectionDB()
            col_db.open(db_path, read_only=True)
            close_after = True
        try:
            entry = col_db.get_query_log_entry(query_id)
            return entry
        finally:
            if close_after:
                try:
                    col_db.close()
                except Exception:
                    pass

    def list_query_logs(
        self,
        tenant: str,
        collection: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        key = (tenant, collection)
        col_db = self._dbs.get(key)
        close_after = False
        if col_db is None:
            db_path = self._db_path(tenant, collection)
            if not db_path.exists():
                return []
            col_db = CollectionDB()
            col_db.open(db_path, read_only=True)
            close_after = True
        try:
            return col_db.list_query_logs(limit, offset)
        finally:
            if close_after:
                try:
                    col_db.close()
                except Exception:
                    pass

    def put_query_home(
        self,
        query_id: str,
        tenant: str,
        collection: str,
    ) -> None:
        self._ensure_catalog().put_query_home(query_id, tenant, collection)

    def resolve_query_home(
        self,
        query_id: str,
    ) -> tuple[str, str] | None:
        return self._ensure_catalog().resolve_query_home(query_id)

    def purge_query_homes_for_collection(
        self,
        tenant: str,
        collection: str,
    ) -> None:
        self._ensure_catalog().purge_query_homes_for_collection(
            tenant,
            collection,
        )

    def list_query_homes(
        self,
        tenant: str | None = None,
        collection: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return self._ensure_catalog().list_query_homes(
            tenant=tenant,
            collection=collection,
            limit=limit,
            offset=offset,
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
        db_path = self._db_path(tenant, collection)
        if not db_path.is_file():
            return (0, 0)

        key = (tenant, collection)
        col_db = self._dbs.get(key)
        if col_db is not None:
            try:
                return col_db.get_doc_chunk_counts()
            except Exception as e:
                if not self._is_transient_db_read_error(e):
                    raise

        fallback = CollectionDB()
        try:
            fallback.open(db_path, read_only=True)
            return fallback.get_doc_chunk_counts()
        except Exception as e:
            if self._is_transient_db_read_error(e):
                return (0, 0)
            raise
        finally:
            try:
                fallback.close()
            except Exception:
                pass

    def catalog_metrics(self) -> dict[str, int]:
        """Return tenant/collection/doc/chunk counters from store metadata."""
        catalog = self._ensure_catalog()
        doc_count = 0
        chunk_count = 0

        for tenant, collection in catalog.list_collection_refs():
            db_path = self._db_path(tenant, collection)
            if not db_path.is_file():
                continue

            key = (tenant, collection)
            col_db = self._dbs.get(key)
            close_after = False
            if col_db is None:
                col_db = CollectionDB()
                col_db.open(db_path, read_only=True)
                close_after = True
            try:
                docs, chunks = col_db.get_doc_chunk_counts()
                doc_count += docs
                chunk_count += chunks
            finally:
                if close_after:
                    col_db.close()

        return {
            "tenant_count": catalog.tenant_count(),
            "collection_count": catalog.collection_count(),
            "doc_count": doc_count,
            "chunk_count": chunk_count,
        }

    def has_doc(self, tenant: str, collection: str, docid: str) -> bool:
        key = (tenant, collection)
        col_db = self._dbs.get(key)
        if col_db is not None:
            try:
                return col_db.has_doc(docid)
            except Exception as e:
                if not self._is_transient_db_read_error(e):
                    raise
        # Fallback: open DB read-only (no wconn, no migrations)
        db_path = self._db_path(tenant, collection)
        if not db_path.exists():
            return False
        col_db = CollectionDB()
        try:
            col_db.open(db_path, read_only=True)
            return col_db.has_doc(docid)
        except Exception as e:
            if self._is_transient_db_read_error(e):
                return False
            raise
        finally:
            try:
                col_db.close()
            except Exception:
                pass

    def get_document(
        self,
        tenant: str,
        collection: str,
        docid: str,
    ) -> dict[str, Any] | None:
        key = (tenant, collection)
        col_db = self._dbs.get(key)
        if col_db is not None:
            try:
                return col_db.get_document(docid)
            except Exception as e:
                if not self._is_transient_db_read_error(e):
                    raise
                log.debug(
                    "Transient cached document read failure for %s/%s/%s: %s",
                    tenant, collection, docid, e,
                )

        db_path = self._db_path(tenant, collection)
        if not db_path.exists():
            return None
        col_db = CollectionDB()
        try:
            col_db.open(db_path, read_only=True)
            return col_db.get_document(docid)
        except Exception as e:
            if self._is_transient_db_read_error(e):
                return None
            raise
        finally:
            try:
                col_db.close()
            except Exception:
                pass

    def list_documents(
        self,
        tenant: str,
        collection: str,
    ) -> list[dict[str, Any]]:
        key = (tenant, collection)
        col_db = self._dbs.get(key)
        if col_db is not None:
            try:
                return col_db.list_documents()
            except Exception as e:
                if not self._is_transient_db_read_error(e):
                    raise
                log.debug(
                    "Transient cached documents read failure for %s/%s: %s",
                    tenant, collection, e,
                )

        db_path = self._db_path(tenant, collection)
        if not db_path.exists():
            return []
        col_db = CollectionDB()
        try:
            col_db.open(db_path, read_only=True)
            return col_db.list_documents()
        except Exception as e:
            if self._is_transient_db_read_error(e):
                return []
            raise
        finally:
            try:
                col_db.close()
            except Exception:
                pass

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

        Key concurrency improvement (Phase 1):
        - FAISS search runs inside collection_lock
        - Meta read (get_meta_batch) runs OUTSIDE lock — WAL concurrent reads
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

        # --- OUTSIDE lock: WAL meta read is concurrent ---
        t0 = _perf()
        meta_batch = self._read_meta_batch_safe(tenant, collection, candidate_rids)
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
            old_catalog = self._catalog if self._catalog._conn is not None else None
            self._catalog_path = None
        self._dbs.clear()
        self._emb.clear()

        if old_catalog is not None:
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

    def _iter_lock_keys(self) -> Iterator[str]:
        data_dir_path = Path(self._data_dir).resolve()
        if not data_dir_path.is_dir():
            return
        for tenant_dir in data_dir_path.iterdir():
            if not tenant_dir.is_dir() or not tenant_dir.name.startswith("t_"):
                continue
            tenant = tenant_dir.name[2:]
            if not tenant:
                continue
            for coll_dir in tenant_dir.iterdir():
                if not coll_dir.is_dir() or not coll_dir.name.startswith("c_"):
                    continue
                collection = coll_dir.name[2:]
                if collection:
                    yield f"t_{tenant}:c_{collection}"

    @contextmanager
    def _lock_all(self) -> Iterator[None]:
        locks: list[Lock] = []
        self._locks_guard.acquire()
        try:
            keys = set(self._iter_lock_keys())
            keys.update(self._locks.keys())
            for key in sorted(keys):
                if key not in self._locks:
                    self._locks[key] = Lock()
                lock = self._locks[key]
                lock.acquire()
                locks.append(lock)
            yield
        finally:
            for lock in reversed(locks):
                lock.release()
            self._locks_guard.release()

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
        data_dir_path = Path(self._data_dir).resolve()
        if not data_dir_path.is_dir():
            raise FileNotFoundError(f"data directory not found: {data_dir_path}")

        if output_path is not None:
            archive_path = Path(output_path).resolve()
            with self._lock_all():
                self._write_zip(data_dir_path, archive_path)
            return str(archive_path), None

        tmp_dir = Path(tempfile.mkdtemp(prefix="pavedb_export_"))
        timestamp = datetime.now(tz.utc).strftime("%Y%m%dT%H%M%SZ")
        archive_path = tmp_dir / f"pavedb-data-{timestamp}.zip"
        with self._lock_all():
            self._write_zip(data_dir_path, archive_path)
        return str(archive_path), str(tmp_dir)

    def restore_archive(self, archive_bytes: bytes) -> None:
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

            with self._lock_all():
                self._flush_caches(async_close=False)
                for entry in data_dir_path.iterdir():
                    self._remove_path(entry)

                for entry in extract_dir.iterdir():
                    target = data_dir_path / entry.name
                    if target.exists() or target.is_symlink():
                        self._remove_path(target)
                    shutil.move(str(entry), str(target))
