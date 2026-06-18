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
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack, contextmanager, suppress
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
from pave.embedders import Embedder, EmbedderUnavailableError
import pave.embedders.factory as embedder_factory_mod
from pave.filters import lookup_meta, matches_filters, sanit_field, sanit_meta_dict
from pave.metadb import CatalogDB, CollectionDB, UnsupportedSchemaVersionError
from pave.stores.base import (
    BaseStore,
    ChunkInfo,
    CollectionInfo,
    DocumentInfo,
    EmbedderConfig,
    FilterSpec,
    IndexResult,
    Metadata,
    MetadataValidationError,
    QueryHomeEntry,
    QueryLogEntry,
    Record,
    SearchOutput,
    SearchResult,
)
from pave.config import get_cfg, get_logger

log = get_logger()

CollectionConfig = dict[str, Any]

# `catalog_metrics()` walks every collection under its read lock to count
# docs/chunks, so its result is memoized for this many seconds. Keeps
# concurrent /metrics scrapers from each re-running the walk and stampeding
# the per-collection locks under load.
_CATALOG_METRICS_TTL_S = 5.0


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


class _CollectionReadWriteLock:
    """Per-collection lock allowing concurrent readers and exclusive writes.

    Reads are reentrant on the same thread (search() already holds a read
    lock when it calls _read_meta_batch_safe which re-enters via
    _read_collection_db). A thread that already holds a read lock bypasses
    the writer-priority gate so a queued writer cannot deadlock the nested
    re-acquisition.

    Read-to-write upgrade is still not supported: a thread holding read_lock
    that calls write_lock will deadlock waiting for itself to release.
    """

    def __init__(self) -> None:
        self._cond = Condition(Lock())
        self._readers: dict[int, int] = {}
        self._writer = False
        self._pending_writers = 0

    @contextmanager
    def read_lock(self) -> Iterator[None]:
        tid = get_ident()
        with self._cond:
            while (
                self._writer
                or (self._pending_writers > 0 and tid not in self._readers)
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
        with self._cond:
            self._pending_writers += 1
            try:
                while self._writer or self._readers:
                    self._cond.wait()
                self._writer = True
            finally:
                self._pending_writers -= 1
        try:
            yield
        finally:
            with self._cond:
                self._writer = False
                self._cond.notify_all()


class LocalStore(BaseStore):
    def __init__(
        self,
        data_dir: str,
        embedder: Embedder,
        cat_db: CatalogDB | None = None,
        embedder_factory: (
            Callable[[str, str, EmbedderConfig], Embedder] | None
        ) = None,
    ) -> None:
        self._data_dir = data_dir
        self._embedder = embedder
        self._embedder_factory = embedder_factory or self._make_embedder
        self._emb: dict[tuple[str, str], VectorBackend] = {}
        self._collection_embedders: dict[tuple[str, str], Embedder] = {}
        self._embedder_cache: dict[str, Embedder] = {}
        self._dbs: dict[tuple[str, str], CollectionDB] = {}
        self._catalog_factory = cat_db.__class__ if cat_db is not None else CatalogDB
        self._catalog = cat_db or self._catalog_factory()
        self._catalog_path: Path | None = None
        self._catalog_guard = Lock()
        self._collection_locks: dict[str, _CollectionReadWriteLock] = {}
        self._collection_locks_guard = Lock()
        self._state_lock = _StoreStateLock()
        self._catalog_metrics_lock = Lock()
        self._catalog_metrics_cache: tuple[float, dict[str, int]] | None = None
        configured_embedders = embedder_factory_mod.embedder_instances()
        log.info(
            "Configured embedders: %s",
            ", ".join(
                f"{instance.key}/{instance.vector_key}"
                for instance in configured_embedders
            ),
        )
        default_resolution = embedder_factory_mod.resolve_default_embedder()
        log.info(
            "Default embedder resolved selector=%s vector_key=%s instances=%s",
            default_resolution.selector,
            default_resolution.vector_key,
            ",".join(instance.key for instance in default_resolution.instances),
        )
        self._embedder_cache[default_resolution.vector_key] = embedder
        self._ensure_catalog()

    @staticmethod
    def _make_embedder(
        embedder_type: str,
        embed_model: str,
        embedder_config: EmbedderConfig,
    ) -> Embedder:
        return embedder_factory_mod.get_embedder(
            embedder_type=embedder_type,
            model=embed_model,
            embedder_config=embedder_config,
        )

    def warm(self) -> None:
        """Load the default embedder and pay FAISS' one-time process init.

        Single `.encode` covers model load + first inference. A throwaway
        FaissBackend in a temp dir triggers FAISS' library-level setup
        (BLAS, threading) without persisting any catalog row or index.
        """
        self._embedder.encode(["warmup"])
        with tempfile.TemporaryDirectory(prefix="pavedb-warm-") as td:
            sentinel = FaissBackend(
                int(self._embedder.dim),
                storage_dir=Path(td),
            )
            sentinel.initialize()
            sentinel.close()

    def _get_collection_lock(self, key: str) -> _CollectionReadWriteLock:
        if key not in self._collection_locks:
            with self._collection_locks_guard:
                if key not in self._collection_locks:
                    self._collection_locks[key] = _CollectionReadWriteLock()
        return self._collection_locks[key]

    @contextmanager
    def _collection_read_lock(
        self,
        tenant: str,
        collection: str,
    ) -> Iterator[None]:
        with self._state_lock.read_lock():
            lock = self._get_collection_lock(f"t_{tenant}:c_{collection}")
            with lock.read_lock():
                yield

    @contextmanager
    def _collection_write_lock(
        self,
        tenant: str,
        collection: str,
    ) -> Iterator[None]:
        with self._collection_write_locks((tenant, collection)):
            yield

    @contextmanager
    def _collection_write_locks(
        self,
        *collections: tuple[str, str],
    ) -> Iterator[None]:
        """Atomic write across one or more collections (e.g. rename)."""
        with self._state_lock.read_lock():
            locks = [
                self._get_collection_lock(f"t_{tenant}:c_{collection}")
                for tenant, collection in collections
            ]
            unique_locks = sorted(set(locks), key=id)
            with ExitStack() as stack:
                for lock in unique_locks:
                    stack.enter_context(lock.write_lock())
                yield

    def _base_path(self, tenant: str, collection: str) -> str:
        return os.path.join(self._data_dir, f"t_{tenant}", f"c_{collection}")

    def _db_path(self, tenant: str, collection: str) -> Path:
        return Path(self._base_path(tenant, collection)) / "meta.db"

    def _catalog_db_path(self) -> Path:
        return Path(self._data_dir).resolve() / "catalog.db"

    def _resolve_collection_config(
        self,
        *,
        embedder_type: str | None = None,
        embed_model: str | None = None,
        embedder_config: EmbedderConfig | None = None,
    ) -> CollectionConfig:
        cfg = get_cfg()
        backend_type = str(cfg.get("vector_store.type")).lower()
        embedder_spec = embedder_factory_mod.resolve_embedder_spec(
            cfg,
            embedder_type=embedder_type,
            model=embed_model,
            embedder_config=embedder_config,
        )
        stored_spec = embedder_factory_mod.persistable_embedder_spec(
            embedder_spec
        )
        return {
            "backend_type": backend_type,
            "backend_config": {},
            **stored_spec,
        }

    def _default_collection_config(self) -> CollectionConfig:
        return self._resolve_collection_config()

    def _effective_collection_config(
        self,
        stored: CollectionConfig | None,
    ) -> CollectionConfig:
        if stored is None:
            return self._default_collection_config()
        return self._resolve_collection_config(
            embedder_type=stored.get("embedder_type"),
            embed_model=stored.get("embed_model"),
            embedder_config=stored.get("embedder_config") or {},
        )

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

    def _register_catalog_collection(
        self,
        tenant: str,
        name: str,
        cfg: CollectionConfig | None = None,
        display_name: str | None = None,
    ) -> None:
        resolved = cfg or self._default_collection_config()
        self._with_catalog_retry(
            "register_collection",
            lambda catalog: catalog.register_collection(
                tenant,
                name,
                display_name=display_name,
                backend_type=resolved["backend_type"],
                backend_config=resolved["backend_config"],
                embedder_type=resolved["embedder_type"],
                embed_model=resolved["embed_model"],
                embed_config=resolved["embedder_config"],
            ),
        )

    def _collection_config_for_load(
        self,
        tenant: str,
        collection: str,
    ) -> CollectionConfig:
        stored = self._with_catalog_retry(
            "get_collection_config",
            lambda catalog: catalog.get_collection_config(tenant, collection),
        )
        cfg = self._effective_collection_config(stored)
        if stored is None:
            self._register_catalog_collection(tenant, collection, cfg)
        return cfg

    def _get_embedder_for_config(self, cfg: CollectionConfig) -> Embedder:
        key = embedder_factory_mod.vector_space_key(cfg)
        embedder = self._embedder_cache.get(key)
        if embedder is None:
            try:
                runtime = embedder_factory_mod.runtime_embedder_spec_for_vector(
                    get_cfg(),
                    cfg,
                )
                embedder = self._embedder_factory(
                    str(runtime["embedder_type"]),
                    str(runtime["embed_model"]),
                    dict(runtime.get("embedder_config") or {}),
                )
            except EmbedderUnavailableError:
                raise
            except Exception as exc:
                raise EmbedderUnavailableError(
                    f"embedder {cfg.get('embedder_type')!s}:"
                    f"{cfg.get('embed_model')!s} could not be initialised: "
                    f"{exc}"
                ) from exc
            self._embedder_cache[key] = embedder
        return embedder

    def _embedder_dim_for_config(
        self,
        cfg: CollectionConfig,
        embedder: Embedder,
    ) -> int:
        try:
            return int(embedder.dim)
        except EmbedderUnavailableError:
            self._embedder_cache.pop(
                embedder_factory_mod.vector_space_key(cfg),
                None,
            )
            raise
        except Exception as exc:
            self._embedder_cache.pop(
                embedder_factory_mod.vector_space_key(cfg),
                None,
            )
            raise EmbedderUnavailableError(
                f"embedder {cfg.get('embedder_type')!s}:"
                f"{cfg.get('embed_model')!s} dimension unavailable: {exc}"
            ) from exc

    def _validate_embedder_for_config(self, cfg: CollectionConfig) -> None:
        embedder = self._get_embedder_for_config(cfg)
        self._embedder_dim_for_config(cfg, embedder)

    def _load_or_init(
        self,
        tenant: str,
        collection: str,
        *,
        collection_config: CollectionConfig | None = None,
    ) -> None:
        key = (tenant, collection)
        if (
            key in self._emb
            and key in self._dbs
            and key in self._collection_embedders
        ):
            return

        base = self._base_path(tenant, collection)
        os.makedirs(base, exist_ok=True)
        coll_cfg = collection_config or self._collection_config_for_load(
            tenant,
            collection,
        )
        embedder = self._get_embedder_for_config(coll_cfg)
        embedder_dim = self._embedder_dim_for_config(coll_cfg, embedder)

        backend = self._emb.get(key)
        if backend is None:
            idx_dir = Path(os.path.join(base, "index"))
            backend = FaissBackend(
                embedder_dim,
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
                    embedder_dim,
                    storage_dir=idx_dir,
                )

        col_db = self._dbs.get(key)
        if col_db is None:
            col_db = CollectionDB()
            col_db.open(self._db_path(tenant, collection))

        self._emb[key] = backend
        self._collection_embedders[key] = embedder
        self._dbs[key] = col_db

    def _save(self, tenant: str, collection: str) -> None:
        key = (tenant, collection)
        em = self._emb.get(key)
        if not em:
            return
        em.flush()

    def create_collection(
        self,
        tenant: str,
        name: str,
        *,
        embedder_type: str | None = None,
        embed_model: str | None = None,
        embedder_config: EmbedderConfig | None = None,
        display_name: str | None = None,
    ) -> CollectionInfo:
        requested = self._resolve_collection_config(
            embedder_type=embedder_type,
            embed_model=embed_model,
            embedder_config=embedder_config,
        )
        explicit = (
            embedder_type is not None
            or embed_model is not None
            or embedder_config is not None
        )
        resolved_display_name = display_name
        is_new = False
        with self._collection_write_lock(tenant, name):
            try:
                stored = self._with_catalog_retry(
                    "get_collection_config",
                    lambda catalog: catalog.get_collection_config(
                        tenant, name,
                    )
                )
                is_new = stored is None
                if stored is None:
                    self._validate_embedder_for_config(requested)
                else:
                    existing = self._effective_collection_config(stored)
                    if (
                        explicit
                        and embedder_factory_mod.embedder_cache_key(existing)
                        != embedder_factory_mod.embedder_cache_key(requested)
                    ):
                        raise ValueError(
                            "collection already exists with a different "
                            "embedder config"
                        )
                    requested = existing
                    resolved_display_name = stored.get("display_name")

                self._load_or_init(
                    tenant,
                    name,
                    collection_config=requested,
                )
                self._save(tenant, name)
                self._register_catalog_collection(
                    tenant,
                    name,
                    requested,
                    display_name=display_name,
                )
            except Exception:
                if is_new:
                    key = (tenant, name)
                    backend = self._emb.pop(key, None)
                    if backend is not None:
                        with suppress(Exception):
                            backend.close()
                    db = self._dbs.pop(key, None)
                    if db is not None:
                        with suppress(Exception):
                            db.close()
                    self._collection_embedders.pop(key, None)
                    with suppress(Exception):
                        self._with_catalog_retry(
                            "unregister_collection",
                            lambda catalog: catalog.unregister_collection(
                                tenant, name,
                            ),
                        )
                    self._remove_path(Path(self._base_path(tenant, name)))
                raise
        requested["display_name"] = resolved_display_name
        return requested

    def delete_collection(self, tenant: str, collection: str) -> None:
        with self._collection_write_lock(tenant, collection):
            key = (tenant, collection)
            backend = self._emb.pop(key, None)
            if backend is not None:
                try:
                    backend.close()
                except Exception:
                    pass
            self._collection_embedders.pop(key, None)
            col_db = self._dbs.pop(key, None)
            if col_db is not None:
                col_db.close()
            path = Path(self._base_path(tenant, collection))
            if path.exists() or path.is_symlink():
                self._remove_path(path)
            self._with_catalog_retry(
                "unregister_collection",
                lambda catalog: catalog.unregister_collection(
                    tenant, collection,
                ),
            )
            self._with_catalog_retry(
                "purge_query_homes_for_collection",
                lambda catalog: catalog.purge_query_homes_for_collection(
                    tenant,
                    collection,
                ),
            )

    def update_collection(
        self,
        tenant: str,
        name: str,
        *,
        display_name: str | None = None,
    ) -> CollectionInfo | None:
        with self._collection_write_lock(tenant, name):
            if not os.path.isdir(self._base_path(tenant, name)):
                raise ValueError(f"collection '{name}' does not exist")
            self._with_catalog_retry(
                "update_collection",
                lambda catalog: catalog.update_collection(
                    tenant,
                    name,
                    display_name=display_name,
                ),
            )
            return self.get_collection_config(tenant, name)

    def move_collection(self, tenant: str, old_name: str, new_name: str) -> None:
        if old_name == new_name:
            raise ValueError(
                f"old and new collection names are the same: {old_name}"
            )

        old_key = (tenant, old_name)
        new_key = (tenant, new_name)
        old_path = self._base_path(tenant, old_name)
        new_path = self._base_path(tenant, new_name)

        with self._collection_write_locks(
            (tenant, old_name),
            (tenant, new_name),
        ):
            # Pre-checks
            if not os.path.isdir(old_path):
                raise ValueError(f"collection '{old_name}' does not exist")
            if os.path.exists(new_path):
                raise ValueError(f"collection '{new_name}' already exists")
            # The catalog can hold a row for new_name even when the directory
            # doesn't (orphan rows from an interrupted op or restore). Catch
            # that here so a UNIQUE collision surfaces as a clean conflict
            # before any filesystem state changes.
            existing = self._with_catalog_retry(
                "move_conflict_check",
                lambda catalog: catalog.get_collection_config(
                    tenant, new_name,
                ),
            )
            if existing is not None:
                raise ValueError(
                    f"collection '{new_name}' already exists"
                )
            rename_cfg = self._collection_config_for_load(tenant, old_name)

            # Flush and drop the old backend before the directory move. The
            # backend keeps its storage path internally; reusing it after move
            # would make later flushes recreate the old collection directory.
            old_backend = self._emb.get(old_key)
            if old_backend is not None:
                old_backend.close()
                self._emb.pop(old_key, None)
            self._collection_embedders.pop(old_key, None)

            # Close DB for old collection before rename
            old_db = self._dbs.pop(old_key, None)
            if old_db is not None:
                old_db.close()

            # Atomic directory rename
            os.rename(old_path, new_path)

            # Re-open caches from the new path.
            self._load_or_init(
                tenant,
                new_name,
                collection_config=rename_cfg,
            )

            # Catalog updates stay inside the two-collection lock so a
            # concurrent delete/create on either name can't race the
            # register-then-rename pair (which is what surfaced as
            # "catalog row missing" and spurious UNIQUE collisions).
            self._register_catalog_collection(tenant, old_name)
            self._with_catalog_retry(
                "move_collection",
                lambda catalog: catalog.move_collection(
                    tenant,
                    old_name,
                    new_name,
                ),
            )
            self._with_catalog_retry(
                "move_query_homes_for_collection",
                lambda catalog: catalog.move_query_homes_for_collection(
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
        # Per-collection read lock pins the cached col_db against concurrent
        # delete_collection / move_collection / restore_archive, which take
        # the write side. Without it, the cached SQLite handle can be closed
        # underneath an in-flight read and surface as "disk I/O error" rather
        # than something the transient-error fallback can recover from.
        with self._collection_read_lock(tenant, collection):
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
    ) -> dict[str, Metadata]:
        if not rids:
            return {}
        return self._read_collection_db(
            tenant,
            collection,
            op_name="meta_batch",
            default={},
            reader=lambda db: db.get_meta_batch(rids),
        )

    def list_collections(self, tenant: str) -> list[CollectionInfo]:
        summaries = self._with_catalog_retry(
            "list_collection_summaries",
            lambda catalog: catalog.list_collection_summaries(tenant),
        )
        out: list[CollectionInfo] = []
        for row in summaries:
            row["vector_space_key"] = embedder_factory_mod.vector_space_key(
                {
                    "embedder_type": row.get("embedder_type"),
                    "embed_model": row.get("embed_model"),
                    "embedder_config": row.get("embedder_config") or {},
                }
            )
            out.append(row)
        return out

    def get_collection_detail(
        self,
        tenant: str,
        name: str,
    ) -> CollectionInfo | None:
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
            "embedder_config": cfg.get("embedder_config") or {},
            "vector_space_key": embedder_factory_mod.vector_space_key(
                {
                    "embedder_type": cfg.get("embedder_type"),
                    "embed_model": cfg.get("embed_model"),
                    "embedder_config": cfg.get("embedder_config") or {},
                }
            ),
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
    ) -> CollectionConfig | None:
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
        filters: FilterSpec | None = None,
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
        key = (tenant, collection)

        def _write_log(col_db: CollectionDB) -> None:
            col_db.log_query(
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

        for attempt in range(3):
            with self._collection_read_lock(tenant, collection):
                col_db = self._dbs.get(key)
                if col_db is not None:
                    _write_log(col_db)
                    break

            with self._collection_write_lock(tenant, collection):
                self._load_or_init(tenant, collection)
                if attempt == 2:
                    _write_log(self._dbs[key])
                    break
        try:
            self.put_query_home(query_id, tenant, collection)
        except Exception:
            log.warning("query_home upsert failed", exc_info=True)

    def get_query_log_entry(
        self,
        tenant: str,
        collection: str,
        query_id: str,
    ) -> QueryLogEntry | None:
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
    ) -> list[QueryLogEntry]:
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
    ) -> list[QueryHomeEntry]:
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

    def catalog_probe(self) -> None:
        """Cheap readiness probe: confirm the catalog is reachable.

        Unlike `catalog_metrics`, this touches no per-collection locks — it
        runs a single catalog-level count so liveness/readiness checks don't
        contend with in-flight ingest/search/delete. Raises on failure.
        """
        self._with_catalog_retry(
            "catalog_probe",
            lambda catalog: catalog.collection_count(),
        )

    def catalog_metrics(self) -> dict[str, int]:
        """Return tenant/collection/doc/chunk counters from store metadata.

        Doc/chunk counts require walking every collection under its read
        lock, so the result is memoized for `_CATALOG_METRICS_TTL_S`. At most
        one walk runs per window; concurrent callers block on the lock and
        share the fresh result rather than each repeating the walk.
        """
        cached = self._catalog_metrics_cache
        if cached is not None:
            ts, value = cached
            if _time.monotonic() - ts < _CATALOG_METRICS_TTL_S:
                return dict(value)

        with self._catalog_metrics_lock:
            cached = self._catalog_metrics_cache
            if cached is not None:
                ts, value = cached
                if _time.monotonic() - ts < _CATALOG_METRICS_TTL_S:
                    return dict(value)
            metrics = self._compute_catalog_metrics()
            self._catalog_metrics_cache = (_time.monotonic(), metrics)
            return dict(metrics)

    def _compute_catalog_metrics(self) -> dict[str, int]:
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

    def get_document(
        self,
        tenant: str,
        collection: str,
        docid: str,
    ) -> DocumentInfo | None:
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
    ) -> list[ChunkInfo]:
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
    ) -> ChunkInfo | None:
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
    ) -> ChunkInfo | None:
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
    ) -> list[DocumentInfo]:
        return self._read_collection_db(
            tenant,
            collection,
            op_name="documents",
            default=[],
            reader=lambda db: db.list_documents(),
        )

    def purge_doc(self, tenant: str, collection: str, docid: str) -> int:
        with self._collection_write_lock(tenant, collection):
            return self._purge_doc_locked(tenant, collection, docid)

    def _purge_doc_locked(self, tenant: str, collection: str, docid: str) -> int:
        self._load_or_init(tenant, collection)
        key = (tenant, collection)
        col_db = self._dbs[key]
        ids = col_db.delete_doc(docid)
        if not ids:
            return 0

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
                      doc_meta: Metadata | None = None
                      ) -> IndexResult:
        """
        Ingests records as (rid, text, meta). Guarantees non-null text, coerces
        dict-records, updates SQLite metadata, saves index. Thread critical.
        """
        chunk_count = 0
        purged = 0
        with self._collection_write_lock(tenant, collection):
            self._load_or_init(tenant, collection)
            key = (tenant, collection)
            col_db = self._dbs[key]
            backend = self._emb[key]
            embedder = self._collection_embedders[key]
            purged = self._purge_doc_locked(tenant, collection, docid)
            if purged:
                log.debug(
                    "INGEST-REPLACE tenant=%s coll=%s docid=%s purged_chunks=%s",
                    tenant,
                    collection,
                    docid,
                    purged,
                )
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
            chunk_rows: list[tuple[str, str | None, Metadata]] = []

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
                return IndexResult(indexed_chunks=0, purged_chunks=purged)

            # Write metadata to SQLite (inside collection_lock)
            col_db.upsert_chunks(docid, chunk_rows, doc_meta=safe_doc_meta)
            vectors = embedder.encode(texts_to_encode)
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
        return IndexResult(indexed_chunks=chunk_count, purged_chunks=purged)

    def search(self, tenant: str, collection: str, query: str, k: int = 5,
               filters: FilterSpec | None = None) -> SearchOutput:
        """
        Queries the FAISS backend for top-k and keeps overfetch inside the
        store.

        Search holds the shared collection read lock while touching cached
        backend, metadata, and chunk files. Collection mutation/delete/restore
        take the write side, so same-collection searches can overlap without
        racing cache close or data-dir replacement.
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

        def _search_loaded(
            backend: VectorBackend,
            col_db: CollectionDB,
            q_vec: Any,
            embed_ms: float,
        ) -> SearchOutput:
            t0 = _perf()
            raw = backend.search(q_vec, fetch_k)
            candidate_rids = [rid for rid, _ in raw if rid]
            search_ms = _ms_since(t0)

            filter_push_ms = 0.0
            if normed_filters:
                t0 = _perf()
                surviving = col_db.filter_by_meta(
                    candidate_rids,
                    normed_filters,
                )
                raw = [
                    (rid, score)
                    for rid, score in raw
                    if rid in surviving
                ]
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

        key = (tenant, collection)

        def _try_search() -> SearchOutput | None:
            """One search attempt against the cached collection state.

            Returns None if the caches aren't loaded (caller reloads and
            retries). The embed runs *outside* the collection lock: it only
            needs the query and the stable per-collection embedder, touching
            neither the FAISS backend, col_db, nor chunk files. Keeping it out
            of the lock shrinks the read-lock hold to the FAISS search + filter
            + hydrate, so concurrent ingest/delete/move writers and embed-CPU
            contention stop serializing on the search path.
            """
            with self._collection_read_lock(tenant, collection):
                backend = self._emb.get(key)
                col_db = self._dbs.get(key)
                embedder = self._collection_embedders.get(key)
            if backend is None or col_db is None or embedder is None:
                return None

            t0 = _perf()
            q_vec = embedder.encode([query])[0]
            embed_ms = _ms_since(t0)

            with self._collection_read_lock(tenant, collection):
                backend = self._emb.get(key)
                col_db = self._dbs.get(key)
                if backend is not None and col_db is not None:
                    return _search_loaded(backend, col_db, q_vec, embed_ms)
            # Collection drained between encode and search (concurrent
            # delete/move); signal the caller to reload and retry.
            return None

        for attempt in range(3):
            out = _try_search()
            if out is not None:
                return out
            with self._collection_write_lock(tenant, collection):
                self._load_or_init(tenant, collection)
            # The collection may have been deleted between initialization and
            # reacquiring the shared read lock. Loop back and re-check caches.
        out = _try_search()
        if out is not None:
            return out
        # Caches drained across every retry — overwhelmingly means a
        # concurrent delete/move removed the collection. Surface as a
        # not-found ValueError so the service layer translates it to 404
        # instead of a generic 500.
        raise ValueError(
            f"collection '{collection}' does not exist"
        )

    def _build_match_reason(self, query: str, score: float,
                            filters: FilterSpec | None,
                            meta: Metadata | None) -> str:
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
        self._collection_embedders.clear()

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
