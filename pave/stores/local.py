# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""On-disk store implementation.

`LocalStore` is the default `BaseStore` for single-instance
deployments. It owns the data directory layout
(`{data_dir}/{tenant}/{collection}/{meta.db, index/, chunks/}`),
the per-collection read/write lock lattice, the catalog
(`pave.metadb.CatalogDB`) bootstrap pass, and the embedder
instance cache keyed by `vector_space_key`.

See `docs/reference/protocols.md` for the frozen v1.0 contract on
the embedder and vector-backend seams this store composes.
"""

from __future__ import annotations
import errno
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack, contextmanager, suppress
from datetime import datetime, date, timezone as tz
from pathlib import Path
from threading import Condition, Lock, get_ident
from typing import Any
import time as _time

import faiss
import numpy as np

# Python 3.12 deprecated the default sqlite3 datetime adapters.
# Register explicit ISO adapters so sqlite I/O doesn't warn.
sqlite3.register_adapter(date, date.isoformat)
sqlite3.register_adapter(datetime, datetime.isoformat)

from pave.backends import FaissBackend
from pave.embedders import Embedder, EmbedderConfig, EmbedderUnavailableError
import pave.embedders.factory as embedder_factory_mod
from pave.filters import lookup_meta, matches_filters, sanit_field, sanit_meta_dict
from pave.metadb import (
    COLLECTION_CONFIG_FILENAME,
    CatalogDB,
    CollectionConfigRecoveryError,
    CollectionDB,
    UnsupportedSchemaVersionError,
    validate_collection_config,
)
from pave.stores.base import (
    BaseStore,
    ChunkInfo,
    CollectionInfo,
    DocumentInfo,
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
from pave.metrics import inc_labeled as m_inc_labeled

log = get_logger()

CollectionConfig = dict[str, Any]

# `refresh_catalog_metrics()` walks every collection under its read lock to
# count docs/chunks. Request-path `catalog_metrics()` only reads this cache.
_CATALOG_METRICS_TTL_S = 5.0
_CHUNK_DRIFT_METRICS_TTL_S = 60.0
_QUERY_LOG_GAUGE_METRICS_TTL_S = 60.0
_QUERY_VECTOR_CACHE_MAX = 256
_COLLECTION_CACHE_DEFAULT_MAX = 64
_EMBEDDER_CACHE_DEFAULT_MAX = 32
_INDEX_DIRTY_FILE = ".pavedb-index-dirty"
_RESTORE_BACKUP_PREFIX = ".pavedb-restore-backup-"


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
    _read_collection_db). Readers wait for active writers, but not for
    queued writers; search read-lock sections are intentionally short and
    writer-priority causes second-scale search tails under mixed hammering.

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
            while self._writer:
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

    @contextmanager
    def try_write_lock(self) -> Iterator[bool]:
        acquired = False
        with self._cond:
            if not self._writer and not self._readers and not self._pending_writers:
                self._writer = True
                acquired = True
        try:
            yield acquired
        finally:
            if acquired:
                with self._cond:
                    self._writer = False
                    self._cond.notify_all()


class LocalStore:
    """Default `BaseStore` implementation backed by local files.

    Each collection maps to a directory holding a SQLite metadata DB,
    a FAISS index, and chunk-text sidecars. Collection lifecycle and
    document writes acquire a per-collection write lock; reads take
    a shared read lock and pin cached handles against concurrent
    writers. The embedder instance cache is keyed by
    `vector_space_key` so collections that share a spec share the
    runtime model.
    """
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
        self._emb: OrderedDict[tuple[str, str], FaissBackend] = OrderedDict()
        self._collection_embedders: dict[tuple[str, str], Embedder] = {}
        self._embedder_cache: OrderedDict[Any, Embedder] = OrderedDict()
        self._dbs: OrderedDict[tuple[str, str], CollectionDB] = OrderedDict()
        self._collection_cache_max = self._positive_config_int(
            "cache.collection_max",
            _COLLECTION_CACHE_DEFAULT_MAX,
        )
        self._embedder_cache_max = self._positive_config_int(
            "cache.embedder_max",
            _EMBEDDER_CACHE_DEFAULT_MAX,
        )
        self._catalog_factory = cat_db.__class__ if cat_db is not None else CatalogDB
        self._catalog = cat_db or self._catalog_factory()
        self._catalog_path: Path | None = None
        self._catalog_guard = Lock()
        self._catalog_bootstrap_guard = Lock()
        self._catalog_bootstrapped = False
        self._collection_locks: dict[str, _CollectionReadWriteLock] = {}
        self._collection_locks_guard = Lock()
        self._state_lock = _StoreStateLock()
        self._catalog_metrics_lock = Lock()
        self._catalog_metrics_cache: tuple[float, dict[str, int]] | None = None
        self._chunk_drift_metrics_lock = Lock()
        self._chunk_drift_metrics_cache: tuple[float, dict[str, int]] | None = None
        self._query_log_gauge_metrics_lock = Lock()
        self._query_log_gauge_metrics_cache: tuple[
            float,
            tuple[int, float | None],
        ] | None = None
        self._query_vector_cache: OrderedDict[
            tuple[str, str, int, str],
            tuple[Embedder, Any],
        ] = OrderedDict()
        self._query_vector_cache_lock = Lock()
        self._closed = False
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
        default_key = embedder_factory_mod.embedder_cache_key(
            default_resolution.instances[0].spec
        )
        self._embedder_cache[default_key] = embedder
        self._ensure_catalog()

    @staticmethod
    def _positive_config_int(key: str, default: int) -> int:
        try:
            return max(1, int(get_cfg().get(key)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _touch_cache_entry(cache, key) -> None:
        move_to_end = getattr(cache, "move_to_end", None)
        if callable(move_to_end) and key in cache:
            move_to_end(key)

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
        warm = getattr(self._embedder, "warm", None)
        if callable(warm):
            warm()
        else:
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
            # Order by the stable collection key, not lock id(): an id()-based
            # order is only consistent while locks are never evicted, which
            # blocks pruning (R3-14). Keys are deterministic across threads.
            keys = sorted({
                f"t_{tenant}:c_{collection}"
                for tenant, collection in collections
            })
            with ExitStack() as stack:
                for key in keys:
                    stack.enter_context(
                        self._get_collection_lock(key).write_lock()
                    )
                yield

    @contextmanager
    def _collection_try_write_lock(
        self,
        tenant: str,
        collection: str,
    ) -> Iterator[bool]:
        with self._state_lock.read_lock():
            lock = self._get_collection_lock(f"t_{tenant}:c_{collection}")
            with lock.try_write_lock() as acquired:
                yield acquired

    def _base_path(self, tenant: str, collection: str) -> str:
        return os.path.join(self._data_dir, f"t_{tenant}", f"c_{collection}")

    def _db_path(self, tenant: str, collection: str) -> Path:
        return Path(self._base_path(tenant, collection)) / "meta.db"

    def _index_dir(self, tenant: str, collection: str) -> Path:
        return Path(self._base_path(tenant, collection)) / "index"

    def _index_dirty_path(self, tenant: str, collection: str) -> Path:
        return self._index_dir(tenant, collection) / _INDEX_DIRTY_FILE

    def _mark_index_dirty(self, tenant: str, collection: str) -> None:
        path = self._index_dirty_path(tenant, collection)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text("dirty\n", encoding="utf-8")
        except OSError:
            log.warning(
                "Failed to mark index dirty tenant=%s coll=%s",
                tenant,
                collection,
                exc_info=True,
            )

    def _clear_index_dirty(self, tenant: str, collection: str) -> None:
        with suppress(OSError):
            self._index_dirty_path(tenant, collection).unlink()

    def _index_is_dirty(self, tenant: str, collection: str) -> bool:
        return self._index_dirty_path(tenant, collection).is_file()

    def _catalog_db_path(self) -> Path:
        return Path(self._data_dir).resolve() / "catalog.db"

    def _collection_config_path(self, tenant: str, collection: str) -> Path:
        return (
            Path(self._base_path(tenant, collection))
            / COLLECTION_CONFIG_FILENAME
        )

    def _write_collection_config(
        self,
        tenant: str,
        collection: str,
        config: CollectionConfig,
        *,
        replace: bool = True,
    ) -> None:
        path = self._collection_config_path(tenant, collection)
        self._write_collection_config_at(path, config, replace=replace)

    @staticmethod
    def _write_collection_config_at(
        path: Path,
        config: CollectionConfig,
        *,
        replace: bool = True,
    ) -> None:
        fd, temporary = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as config_file:
                config_file.write(json.dumps(config, sort_keys=True))
                config_file.flush()
                os.fsync(config_file.fileno())
            if replace:
                os.replace(temporary, path)
            else:
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
        finally:
            with suppress(FileNotFoundError):
                Path(temporary).unlink()

    @staticmethod
    def _archive_json_dict(payload: object) -> dict[str, Any]:
        if not isinstance(payload, str):
            raise ValueError("archive config JSON is missing")
        loaded = json.loads(payload)
        if not isinstance(loaded, dict):
            raise ValueError("archive config JSON is not an object")
        return loaded

    @staticmethod
    def _validate_archive_collection_config(
        config: object,
        tenant: str,
        collection: str,
    ) -> CollectionConfig:
        label = f"{tenant}/{collection}"
        try:
            persisted = validate_collection_config(config)
            spec = embedder_factory_mod._spec_from_parts(
                etype=persisted["embedder_type"],
                model=persisted["embed_model"],
                config=persisted["embedder_config"],
            )
            portable = embedder_factory_mod.persistable_embedder_spec(spec)
        except Exception:
            raise ValueError(
                f"invalid archive collection config: {label}"
            ) from None
        if any(
            persisted[key] != portable[key]
            for key in (
                "embedder_type",
                "embed_model",
                "embedder_config",
            )
        ):
            raise ValueError(f"invalid archive collection config: {label}")
        return persisted

    def _archive_catalog_configs(
        self,
        catalog: CatalogDB,
    ) -> dict[tuple[str, str], CollectionConfig]:
        """Read raw catalog configs without the normal recovery fallback."""
        try:
            with catalog._reader() as conn:
                rows = conn.execute(
                    """
                    SELECT tenant, name, backend_type, backend_config_json,
                           embedder_type, embed_model, embed_config_json
                    FROM collections
                    """
                ).fetchall()
        except Exception:
            raise ValueError("invalid archive catalog") from None

        configs: dict[tuple[str, str], CollectionConfig] = {}
        for row in rows:
            (
                tenant,
                collection,
                backend_type,
                backend_config,
                embedder_type,
                embed_model,
                embedder_config,
            ) = row
            if not isinstance(tenant, str) or not isinstance(collection, str):
                raise ValueError("invalid archive catalog")
            if tenant == "_system":
                continue
            try:
                configs[(tenant, collection)] = (
                    self._validate_archive_collection_config(
                        {
                            "backend_type": backend_type,
                            "backend_config": self._archive_json_dict(
                                backend_config
                            ),
                            "embedder_type": embedder_type,
                            "embed_model": embed_model,
                            "embedder_config": self._archive_json_dict(
                                embedder_config
                            ),
                        },
                        tenant,
                        collection,
                    )
                )
            except ValueError:
                raise ValueError(
                    f"invalid archive collection config: {tenant}/{collection}"
                ) from None
        return configs

    def _archive_sidecar_config(
        self,
        collection_dir: Path,
        tenant: str,
        collection: str,
    ) -> CollectionConfig | None:
        path = collection_dir / COLLECTION_CONFIG_FILENAME
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            raise ValueError(
                f"invalid archive collection config: {tenant}/{collection}"
            ) from None
        return self._validate_archive_collection_config(
            config,
            tenant,
            collection,
        )

    @staticmethod
    def _validate_archive_index(
        collection_dir: Path,
        tenant: str,
        collection: str,
        rids: set[str],
    ) -> None:
        index_dir = collection_dir / "index"
        try:
            index = faiss.read_index(str(index_dir / "faiss.index"))
            id_map = json.loads(
                (index_dir / "id_map.json").read_text(encoding="utf-8")
            )
            next_id = int(id_map["next_id"])
            rid_to_id = {
                str(rid): int(index_id)
                for rid, index_id in id_map["rid_to_id"].items()
            }
            id_to_rid = {
                int(index_id): str(rid)
                for index_id, rid in id_map["id_to_rid"].items()
            }
            if (
                index.metric_type != faiss.METRIC_INNER_PRODUCT
                or len(rid_to_id) != int(index.ntotal)
                or len(id_to_rid) != int(index.ntotal)
                or {index_id: rid for rid, index_id in rid_to_id.items()}
                != id_to_rid
                or set(rid_to_id) != rids
                or {
                    int(index_id)
                    for index_id in faiss.vector_to_array(index.id_map)
                }
                != set(id_to_rid)
                or next_id < 0
                or next_id <= max(id_to_rid, default=-1)
            ):
                raise ValueError("invalid id map")
        except Exception:
            raise ValueError(
                f"invalid archive collection index: {tenant}/{collection}"
            ) from None

    @staticmethod
    def _assert_no_restore_backup(data_dir: Path) -> None:
        if not data_dir.is_dir():
            return
        marker = next(
            (
                entry.name
                for entry in data_dir.iterdir()
                if entry.name.startswith(_RESTORE_BACKUP_PREFIX)
            ),
            None,
        )
        if marker is not None:
            raise RuntimeError(
                "interrupted archive restore detected; recover or remove "
                f"{marker} before starting"
            )

    def _preflight_archive(
        self,
        data_dir: Path,
        *,
        validate_destination: bool = False,
    ) -> None:
        """Validate archive consistency and repair missing config surfaces."""
        data_dir = data_dir.resolve()
        catalog_path = data_dir / "catalog.db"
        if not catalog_path.is_file():
            raise ValueError("archive is missing catalog.db")
        try:
            with sqlite3.connect(
                f"file:{catalog_path.as_posix()}?mode=ro&immutable=1",
                uri=True,
            ) as conn:
                schema_version = CatalogDB._schema_version(conn)
        except sqlite3.Error:
            raise ValueError("invalid archive catalog") from None
        if schema_version == 0:
            raise ValueError("invalid archive catalog")
        if any(
            entry.name.startswith(_RESTORE_BACKUP_PREFIX)
            for entry in data_dir.iterdir()
        ):
            raise ValueError("archive contains an interrupted restore backup")

        catalog = CatalogDB()
        try:
            catalog.open(catalog_path)
            catalog_configs = self._archive_catalog_configs(catalog)
            collection_dirs = CatalogDB.discover_collection_dirs(
                data_dir,
                require_metadata=False,
            )
            for key in catalog_configs:
                if key not in collection_dirs:
                    raise ValueError(
                        "archive catalog collection has no directory: "
                        f"{key[0]}/{key[1]}"
                    )

            vector_spaces: dict[str, CollectionConfig] = {}
            for (tenant, collection), collection_dir in collection_dirs.items():
                metadata_path = collection_dir / "meta.db"
                if not metadata_path.is_file():
                    raise ValueError(
                        "archive collection directory is missing metadata "
                        f"database: {tenant}/{collection}"
                    )
                metadata = CollectionDB()
                try:
                    metadata.open(
                        metadata_path,
                        read_only=True,
                        immutable=True,
                    )
                    with metadata._reader() as conn:
                        required_columns = {
                            "documents": {
                                "docid", "version", "ingested_at", "meta_json"
                            },
                            "chunks": {
                                "docid", "rid", "chunk_path", "meta_json",
                                "ingested_at",
                            },
                            "chunk_meta": {"rid", "key", "value"},
                            "document_meta": {"docid", "key", "value"},
                            "query_log": {
                                "query_id", "tenant", "collection", "actor",
                                "query_text", "k", "filters_json", "include_common",
                                "common_tenant", "common_collection", "result_ids_json",
                                "result_count", "latency_ms", "timing_json",
                                "request_id", "replay_of", "executed_at",
                            },
                        }
                        for table, columns in required_columns.items():
                            found = {
                                row[1]
                                for row in conn.execute(
                                    f"PRAGMA table_info({table})"
                                )
                            }
                            if not columns <= found:
                                raise ValueError("incomplete archive metadata schema")
                        rids = {
                            str(row[0])
                            for row in conn.execute("SELECT rid FROM chunks")
                        }
                except Exception:
                    raise ValueError(
                        "invalid archive collection metadata database: "
                        f"{tenant}/{collection}"
                    ) from None
                finally:
                    metadata.close()
                self._validate_archive_index(
                    collection_dir,
                    tenant,
                    collection,
                    rids,
                )
                sidecar = self._archive_sidecar_config(
                    collection_dir,
                    tenant,
                    collection,
                )
                catalog_config = catalog_configs.get((tenant, collection))
                if sidecar is None:
                    if catalog_config is None:
                        raise ValueError(
                            "archive collection has no recoverable config: "
                            f"{tenant}/{collection}"
                        )
                    self._write_collection_config_at(
                        collection_dir / COLLECTION_CONFIG_FILENAME,
                        catalog_config,
                    )
                    config = catalog_config
                elif catalog_config is None:
                    catalog.register_collection(
                        tenant,
                        collection,
                        backend_type=sidecar["backend_type"],
                        backend_config=sidecar["backend_config"],
                        embedder_type=sidecar["embedder_type"],
                        embed_model=sidecar["embed_model"],
                        embed_config=sidecar["embedder_config"],
                    )
                    config = sidecar
                elif catalog_config != sidecar:
                    raise ValueError(
                        "archive catalog and sidecar conflict: "
                        f"{tenant}/{collection}"
                    )
                else:
                    config = catalog_config
                if validate_destination:
                    vector_spaces.setdefault(
                        embedder_factory_mod.vector_space_key(config),
                        config,
                    )

            if validate_destination:
                for vector_key, config in vector_spaces.items():
                    try:
                        embedder_factory_mod.runtime_embedder_spec_for_vector(
                            get_cfg(),
                            config,
                        )
                    except Exception:
                        raise ValueError(
                            "no compatible destination embedder for vector space "
                            f"{vector_key}"
                        ) from None
        except ValueError:
            raise
        except Exception:
            raise ValueError("invalid archive catalog") from None
        finally:
            catalog.close()
            for suffix in ("-wal", "-shm"):
                with suppress(FileNotFoundError):
                    (data_dir / f"catalog.db{suffix}").unlink()

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
        self._assert_no_restore_backup(catalog_path.parent)
        cat = self._catalog
        if (
            self._catalog_path == catalog_path
            and self._catalog_bootstrapped
            and cat._conn is not None
        ):
            return cat
        with self._catalog_guard:
            if (
                self._catalog_path == catalog_path
                and self._catalog_bootstrapped
                and self._catalog._conn is not None
            ):
                return self._catalog

            if (
                self._catalog_path != catalog_path
                or self._catalog._conn is None
            ):
                if self._catalog._conn is not None:
                    try:
                        self._catalog.close()
                    except Exception:
                        pass

                self._catalog.open(catalog_path)
                self._catalog_path = catalog_path
                self._catalog_bootstrapped = False
            catalog = self._catalog

        with self._catalog_bootstrap_guard:
            if (
                self._catalog_path == catalog_path
                and self._catalog_bootstrapped
                and self._catalog._conn is not None
            ):
                return self._catalog
            if self._catalog is catalog:
                seeded, removed = catalog.bootstrap(Path(self._data_dir))
                for tenant, collection in catalog.list_collection_refs():
                    path = self._collection_config_path(tenant, collection)
                    if path.exists():
                        continue
                    stored = catalog.get_collection_config(tenant, collection)
                    if stored is None:
                        continue
                    config: CollectionConfig = {
                        "backend_type": stored.get("backend_type"),
                        "backend_config": stored.get("backend_config"),
                        "embedder_type": stored.get("embedder_type"),
                        "embed_model": stored.get("embed_model"),
                        "embedder_config": stored.get("embedder_config"),
                    }
                    if not any((
                        config["embedder_type"] is not None,
                        config["embed_model"] is not None,
                        bool(config["embedder_config"]),
                    )):
                        continue
                    try:
                        config = validate_collection_config(config)
                    except ValueError as exc:
                        raise CollectionConfigRecoveryError(
                            "catalog contains invalid collection config: "
                            f"{tenant}/{collection}"
                        ) from exc
                    self._write_collection_config(
                        tenant,
                        collection,
                        config,
                        replace=False,
                    )
                if seeded or removed:
                    log.info(
                        "CatalogDB bootstrap: seeded=%s "
                        "removed_orphans=%s path=%s",
                        seeded,
                        removed,
                        catalog_path,
                    )
                with self._catalog_guard:
                    if (
                        self._catalog is catalog
                        and self._catalog_path == catalog_path
                        and self._catalog._conn is not None
                    ):
                        self._catalog_bootstrapped = True
                        return catalog
        return self._ensure_catalog()

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
        try:
            runtime = embedder_factory_mod.runtime_embedder_spec_for_vector(
                get_cfg(),
                cfg,
            )
            key = embedder_factory_mod.embedder_cache_key(runtime)
            embedder = self._embedder_cache.get(key)
            if embedder is not None:
                self._touch_cache_entry(self._embedder_cache, key)
                return embedder
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

    def _drop_cached_embedder(self, embedder: Embedder) -> None:
        for key, cached in list(self._embedder_cache.items()):
            if cached is embedder:
                self._embedder_cache.pop(key, None)
        self._close_embedder_if_orphaned(embedder)

    def _evict_embedder_cache_if_needed(self) -> None:
        if not isinstance(self._embedder_cache, OrderedDict):
            return
        while len(self._embedder_cache) > self._embedder_cache_max:
            for key, embedder in list(self._embedder_cache.items()):
                if embedder is self._embedder:
                    continue
                if any(
                    cached is embedder
                    for cached in self._collection_embedders.values()
                ):
                    continue
                self._embedder_cache.pop(key, None)
                self._close_embedder_if_orphaned(embedder)
                m_inc_labeled("cache_evictions_total", {"kind": "embedder"})
                break
            else:
                return

    def _drop_collection_embedder(self, key: tuple[str, str]) -> None:
        embedder = self._collection_embedders.pop(key, None)
        self._close_embedder_if_orphaned(embedder)
        self._evict_embedder_cache_if_needed()

    def _close_embedder_if_orphaned(self, embedder: Embedder | None) -> None:
        """Close an embedder's worker once no cache still references it.

        Embedders are shared across collections by vector_space_key, so a
        pop must not close one a sibling still uses. Only close when neither
        cache holds this object any more, otherwise the SBERT worker
        subprocess leaks until GC (R3-16).
        """
        if embedder is None:
            return
        if any(e is embedder for e in list(self._embedder_cache.values())):
            return
        if any(e is embedder for e in list(self._collection_embedders.values())):
            return
        close = getattr(embedder, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _embedder_dim_for_config(
        self,
        cfg: CollectionConfig,
        embedder: Embedder,
    ) -> int:
        try:
            return int(embedder.dim)
        except EmbedderUnavailableError:
            self._drop_cached_embedder(embedder)
            raise
        except Exception as exc:
            self._drop_cached_embedder(embedder)
            raise EmbedderUnavailableError(
                f"embedder {cfg.get('embedder_type')!s}:"
                f"{cfg.get('embed_model')!s} dimension unavailable: {exc}"
            ) from exc

    @staticmethod
    def _encode_embedder(embedder: Embedder, texts: list[str]) -> Any:
        try:
            return embedder.encode(texts)
        except EmbedderUnavailableError:
            raise
        except Exception as exc:
            raise EmbedderUnavailableError(
                f"embedder encode failed: {exc}"
            ) from exc

    def _validate_embedder_for_config(self, cfg: CollectionConfig) -> None:
        embedder = self._get_embedder_for_config(cfg)
        self._embedder_dim_for_config(cfg, embedder)
        self._evict_embedder_cache_if_needed()

    @staticmethod
    def _backend_vector_count(backend: FaissBackend) -> int | None:
        count = getattr(backend, "count", None)
        if callable(count):
            try:
                return max(0, int(count()))
            except Exception:
                return None
        index = getattr(backend, "_index", None)
        total = getattr(index, "ntotal", None)
        if total is None:
            return None
        try:
            return max(0, int(total))
        except Exception:
            return None

    def _index_needs_rebuild(
        self,
        tenant: str,
        collection: str,
        backend: FaissBackend,
        col_db: CollectionDB,
    ) -> bool:
        if self._index_is_dirty(tenant, collection):
            return True
        try:
            _docs, chunks = col_db.get_doc_chunk_counts()
        except Exception:
            return False
        total = self._backend_vector_count(backend)
        return total is not None and total != chunks

    def _rebuild_index_from_chunks(
        self,
        tenant: str,
        collection: str,
        backend: FaissBackend,
        col_db: CollectionDB,
        embedder: Embedder,
    ) -> FaissBackend:
        rids: list[str] = []
        texts: list[str] = []
        missing_source = False
        for document in col_db.list_documents():
            docid = str(document.get("docid") or "")
            if not docid:
                continue
            for chunk in col_db.list_chunks(docid):
                rid = str(chunk.get("rid") or "")
                if not rid:
                    continue
                text = self._load_chunk_text(tenant, collection, rid)
                if text is None:
                    missing_source = True
                    log.warning(
                        "Skipping missing chunk text during index rebuild "
                        "tenant=%s coll=%s rid=%s",
                        tenant,
                        collection,
                        rid,
                    )
                    continue
                rids.append(rid)
                texts.append(text)
        if missing_source and backend.count():
            log.warning(
                "Preserving existing index because not every chunk has "
                f"rebuild source tenant={tenant} coll={collection}"
            )
            return backend
        rebuilt = FaissBackend(
            int(embedder.dim),
            storage_dir=self._index_dir(tenant, collection),
        )
        if rids:
            rebuilt.add(rids, self._encode_embedder(embedder, texts))
        rebuilt.flush()
        self._clear_index_dirty(tenant, collection)
        return rebuilt

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
            self._touch_cache_entry(self._emb, key)
            self._touch_cache_entry(self._dbs, key)
            return

        base = self._base_path(tenant, collection)
        os.makedirs(base, exist_ok=True)
        coll_cfg = collection_config or self._collection_config_for_load(
            tenant,
            collection,
        )
        embedder = self._get_embedder_for_config(coll_cfg)
        embedder_dim = self._embedder_dim_for_config(coll_cfg, embedder)
        idx_dir = self._index_dir(tenant, collection)

        backend = self._emb.get(key)
        if backend is None:
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

        if self._index_needs_rebuild(tenant, collection, backend, col_db):
            log.info(
                "Rebuilding dirty index tenant=%s coll=%s",
                tenant,
                collection,
            )
            backend = self._rebuild_index_from_chunks(
                tenant,
                collection,
                backend,
                col_db,
                embedder,
            )

        self._emb[key] = backend
        self._collection_embedders[key] = embedder
        self._dbs[key] = col_db
        self._evict_embedder_cache_if_needed()
        self._touch_cache_entry(self._emb, key)
        self._touch_cache_entry(self._dbs, key)
        self._evict_collection_cache_if_needed(current_key=key)

    def _evict_collection_cache_if_needed(
        self,
        *,
        current_key: tuple[str, str],
    ) -> None:
        if not isinstance(self._emb, OrderedDict):
            return
        if not isinstance(self._dbs, OrderedDict):
            return

        over_budget = (
            len(self._emb) > self._collection_cache_max
            or len(self._dbs) > self._collection_cache_max
        )
        while over_budget:
            candidates = list(dict.fromkeys([*self._emb.keys(), *self._dbs.keys()]))
            evicted = False
            for tenant, collection in candidates:
                key = (tenant, collection)
                if key == current_key:
                    continue
                with self._collection_try_write_lock(tenant, collection) as locked:
                    if not locked:
                        continue
                    self._evict_collection_cache_entry(key)
                    evicted = True
                    break
            if not evicted:
                return
            over_budget = (
                len(self._emb) > self._collection_cache_max
                or len(self._dbs) > self._collection_cache_max
            )

    def _evict_collection_cache_entry(self, key: tuple[str, str]) -> None:
        tenant, collection = key
        backend = self._emb.pop(key, None)
        col_db = self._dbs.pop(key, None)
        self._drop_collection_embedder(key)
        self._drop_query_vector_cache(tenant, collection)
        if backend is not None:
            try:
                backend.flush()
                backend.close()
                self._clear_index_dirty(tenant, collection)
                m_inc_labeled("cache_evictions_total", {"kind": "backend"})
            except Exception:
                pass
        if col_db is not None:
            try:
                col_db.close()
                m_inc_labeled("cache_evictions_total", {"kind": "db"})
            except Exception:
                pass

    def _save(self, tenant: str, collection: str) -> None:
        key = (tenant, collection)
        em = self._emb.get(key)
        if not em:
            return
        em.flush()
        self._clear_index_dirty(tenant, collection)

    def _drop_query_vector_cache(
        self,
        tenant: str | None = None,
        collection: str | None = None,
    ) -> None:
        with self._query_vector_cache_lock:
            if tenant is None and collection is None:
                self._query_vector_cache.clear()
                return
            stale = [
                key
                for key in self._query_vector_cache
                if (tenant is None or key[0] == tenant)
                and (collection is None or key[1] == collection)
            ]
            for key in stale:
                self._query_vector_cache.pop(key, None)

    def _encode_query_cached(
        self,
        tenant: str,
        collection: str,
        embedder: Embedder,
        query: str,
    ) -> tuple[Any, bool]:
        cache_key = (tenant, collection, id(embedder), query)
        with self._query_vector_cache_lock:
            cached = self._query_vector_cache.get(cache_key)
            if cached is not None and cached[0] is embedder:
                self._query_vector_cache.move_to_end(cache_key)
                return cached[1], True

        vector = self._encode_embedder(embedder, [query])[0]
        with self._query_vector_cache_lock:
            self._query_vector_cache[cache_key] = (embedder, vector)
            self._query_vector_cache.move_to_end(cache_key)
            while len(self._query_vector_cache) > _QUERY_VECTOR_CACHE_MAX:
                self._query_vector_cache.popitem(last=False)
        return vector, False

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
                self._write_collection_config(tenant, name, requested)
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
                    self._drop_collection_embedder(key)
                    self._drop_query_vector_cache(tenant, name)
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
                    discard = getattr(backend, "discard", None)
                    if callable(discard):
                        discard()
                    backend.close()
                except Exception:
                    pass
            self._drop_collection_embedder(key)
            self._drop_query_vector_cache(tenant, collection)
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
                return None
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
            self._drop_collection_embedder(old_key)
            self._drop_query_vector_cache(tenant, old_name)
            self._drop_query_vector_cache(tenant, new_name)

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

    def _cache_estimated_bytes(self) -> int:
        total = 0
        for backend in list(self._emb.values()):
            count = self._backend_vector_count(backend)
            dim = getattr(backend, "_dimension", None)
            if count is None or dim is None:
                continue
            try:
                total += count * int(dim) * 4
            except (TypeError, ValueError):
                continue
        return total

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

    def get_collection_vector_dim(self, tenant: str, collection: str) -> int:
        key = (tenant, collection)
        with self._collection_read_lock(tenant, collection):
            embedder = self._collection_embedders.get(key)
            if embedder is not None:
                return int(embedder.dim)
        with self._collection_write_lock(tenant, collection):
            self._load_or_init(tenant, collection)
            return int(self._collection_embedders[key].dim)

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
        """Return cached tenant/collection/doc/chunk counters.

        This is used by `/health/metrics` and `/metrics`, so it must never walk
        collections on the request path.
        """
        with self._catalog_metrics_lock:
            cached = self._catalog_metrics_cache
            if cached is None:
                return {
                    "tenant_count": 0,
                    "collection_count": 0,
                    "doc_count": 0,
                    "chunk_count": 0,
                }
            return dict(cached[1])

    def metrics_generation(self) -> str:
        return self._with_catalog_retry(
            "metrics_generation",
            lambda catalog: catalog.metrics_generation(),
        )

    def read_metrics(self) -> dict[str, Any]:
        return self._with_catalog_retry(
            "read_metrics",
            lambda catalog: catalog.read_metrics(),
        )

    def flush_metrics_delta(
        self,
        generation: str,
        counters: list[tuple[str, str, float]],
        latencies: dict[str, list[float]],
        last_error: tuple[str, float] | None,
        sample_limit: int,
    ) -> tuple[str, bool]:
        return self._with_catalog_retry(
            "flush_metrics_delta",
            lambda catalog: catalog.flush_metrics_delta(
                generation,
                counters,
                latencies,
                last_error,
                sample_limit,
            ),
        )

    def reset_metrics(self) -> str:
        return self._with_catalog_retry(
            "reset_metrics",
            lambda catalog: catalog.reset_metrics(),
        )

    def refresh_catalog_metrics(self) -> dict[str, int]:
        """Refresh cached catalog counters with the expensive collection walk."""
        metrics = self._compute_catalog_metrics()
        with self._catalog_metrics_lock:
            self._catalog_metrics_cache = (_time.monotonic(), metrics)
            return dict(metrics)

    def catalog_metrics_freshness(self) -> tuple[float | None, bool]:
        with self._catalog_metrics_lock:
            cached = self._catalog_metrics_cache
            if cached is None:
                return None, True
            age = _time.monotonic() - cached[0]
            return age, age > _CATALOG_METRICS_TTL_S

    def query_log_gauge_stats(self) -> tuple[int, float | None]:
        with self._query_log_gauge_metrics_lock:
            now = _time.monotonic()
            cached = self._query_log_gauge_metrics_cache
            if (
                cached is not None
                and now - cached[0] < _QUERY_LOG_GAUGE_METRICS_TTL_S
            ):
                return cached[1]

            total = 0
            oldest: float | None = None
            refs = self._with_catalog_retry(
                "query_log_collection_refs",
                lambda catalog: catalog.list_collection_refs(),
            )
            for tenant, collection in refs:
                count, first = self._read_collection_db(
                    tenant,
                    collection,
                    op_name="query_log_gauge_stats",
                    default=(0, None),
                    reader=lambda db: db.query_log_gauge_stats(),
                )
                total += count
                if first is not None and (oldest is None or first < oldest):
                    oldest = first
            stats = total, oldest
            self._query_log_gauge_metrics_cache = now, stats
            return stats

    def chunk_drift_stats(self) -> dict[str, int]:
        with self._chunk_drift_metrics_lock:
            now = _time.monotonic()
            cached = self._chunk_drift_metrics_cache
            if cached is not None and now - cached[0] < _CHUNK_DRIFT_METRICS_TTL_S:
                return dict(cached[1])

            orphaned = 0
            missing = 0
            refs = self._with_catalog_retry(
                "chunk_drift_collection_refs",
                lambda catalog: catalog.list_collection_refs(),
            )
            for tenant, collection in refs:
                paths = self._read_collection_db(
                    tenant,
                    collection,
                    op_name="chunk_drift_paths",
                    default=set(),
                    reader=lambda db: db.chunk_paths(),
                )
                base = Path(self._base_path(tenant, collection))
                expected = {base / path for path in paths}
                sidecars = set((base / "chunks").glob("*.txt"))
                orphaned += len(sidecars - expected)
                missing += len(expected - sidecars)

            stats = {
                "orphaned_sidecars": orphaned,
                "missing_sidecars": missing,
            }
            self._chunk_drift_metrics_cache = (now, stats)
            return dict(stats)

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
            "cache_estimated_bytes": self._cache_estimated_bytes(),
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

    def _purge_doc_locked(
        self,
        tenant: str,
        collection: str,
        docid: str,
    ) -> int:
        self._load_or_init(tenant, collection)
        key = (tenant, collection)
        col_db = self._dbs[key]
        existing_ids = col_db.get_rids_for_doc(docid)
        if not existing_ids:
            return 0
        self._mark_index_dirty(tenant, collection)
        ids = col_db.delete_doc(docid)

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

    def _prepare_index_records_locked(
        self,
        tenant: str,
        collection: str,
        docid: str,
        records: Iterable[Record],
        doc_meta: Metadata | None,
        *,
        verify_roundtrip: bool,
        save_text: bool = True,
    ) -> tuple[list[str], list[str], list[tuple[str, str | None, Metadata]], Metadata]:
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

        for record in records:
            if isinstance(record, dict):
                rid = record.get("rid") or record.get("id") or record.get("uid")
                txt = record.get("text") or record.get("content")
                md = (
                    record.get("meta") or record.get("metadata") or
                    record.get("tags") or {}
                )
            else:
                try:
                    rid, txt, md = record
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

            chunk_path = os.path.join("chunks", self._urid_to_fname(rid))
            chunk_rows.append((rid, chunk_path, safe_meta))
            rids_to_add.append(rid)
            texts_to_encode.append(txt)

            if save_text:
                self._save_chunk_text(tenant, collection, rid, txt)
            if save_text and verify_roundtrip:
                loaded = self._load_chunk_text(tenant, collection, rid) or ""
                if txt != loaded:
                    log.warning(
                        f"Chunk text round-trip mismatch for {rid}: "
                        f"saved {len(txt)} chars, loaded {len(loaded)} chars"
                    )

        return rids_to_add, texts_to_encode, chunk_rows, safe_doc_meta

    def index_records(
        self,
        tenant: str,
        collection: str,
        docid: str,
        records: Iterable[Record],
        doc_meta: Metadata | None = None,
        *,
        vectors=None,
    ) -> IndexResult:
        """
        Ingests records as (rid, text, meta). Guarantees non-null text, coerces
        dict-records, updates SQLite metadata, saves index. Thread critical.

        BYOV: when ``vectors`` is given (one raw vector per record) the embed
        step and the chunk-text sidecar are skipped; the vectors go straight to
        the backend and hydration returns no text for those records.
        """
        byov = vectors is not None
        chunk_count = 0
        purged = 0
        key = (tenant, collection)
        with self._collection_write_lock(tenant, collection):
            self._load_or_init(tenant, collection)
            embedder = self._collection_embedders[key]

        rids_to_add, texts_to_encode, chunk_rows, safe_doc_meta = (
            self._prepare_index_records_locked(
                tenant,
                collection,
                docid,
                records,
                doc_meta,
                verify_roundtrip=False,
                save_text=False,
            )
        )
        if byov:
            vectors = np.asarray(vectors, dtype=np.float32)
        else:
            vectors = (
                self._encode_embedder(embedder, texts_to_encode)
                if rids_to_add else None
            )

        with self._collection_write_lock(tenant, collection):
            self._load_or_init(tenant, collection)
            col_db = self._dbs[key]
            backend = self._emb[key]
            current_embedder = self._collection_embedders[key]
            purged = self._purge_doc_locked(tenant, collection, docid)
            if purged:
                log.debug(
                    "INGEST-REPLACE tenant=%s coll=%s docid=%s purged_chunks=%s",
                    tenant,
                    collection,
                    docid,
                    purged,
                )

            if not rids_to_add:
                return IndexResult(indexed_chunks=0, purged_chunks=purged)

            self._mark_index_dirty(tenant, collection)
            if not byov:
                for rid, text in zip(
                    rids_to_add,
                    texts_to_encode,
                    strict=False,
                ):
                    self._save_chunk_text(tenant, collection, rid, text)
                if current_embedder is not embedder:
                    vectors = self._encode_embedder(
                        current_embedder,
                        texts_to_encode,
                    )
            assert vectors is not None

            # Write metadata to SQLite (inside collection_lock)
            col_db.upsert_chunks(docid, chunk_rows, doc_meta=safe_doc_meta)
            backend.add(rids_to_add, vectors)
            _rids = rids_to_add[:3]
            _sfx = " ..." if len(rids_to_add) > 3 else ""
            log.debug(
                f"INGEST-PREPARED: {len(rids_to_add)} chunks {_rids}{_sfx}"
            )
            chunk_count = len(rids_to_add)

            if chunk_count:
                self._register_catalog_collection(tenant, collection)
            if byov:
                self._save(tenant, collection)
        return IndexResult(indexed_chunks=chunk_count, purged_chunks=purged)

    def index_documents(
        self,
        tenant: str,
        collection: str,
        documents: Iterable[tuple[str, Iterable[Record], Metadata | None]],
    ) -> list[IndexResult]:
        """Index several prepared documents with one encode/add/save cycle."""
        prepared = [
            (str(docid), list(records), doc_meta)
            for docid, records, doc_meta in documents
        ]
        if not prepared:
            return []

        results: list[IndexResult] = []
        all_rids: list[str] = []
        all_texts: list[str] = []
        docs_to_commit: list[
            tuple[
                str,
                list[str],
                list[str],
                list[tuple[str, str | None, Metadata]],
                Metadata,
            ]
        ] = []
        changed = False
        key = (tenant, collection)

        with self._collection_write_lock(tenant, collection):
            self._load_or_init(tenant, collection)
            embedder = self._collection_embedders[key]

        for docid, records, doc_meta in prepared:
            rids, texts, chunk_rows, safe_doc_meta = (
                self._prepare_index_records_locked(
                    tenant,
                    collection,
                    docid,
                    records,
                    doc_meta,
                    verify_roundtrip=False,
                    save_text=False,
                )
            )
            docs_to_commit.append(
                (docid, rids, texts, chunk_rows, safe_doc_meta)
            )
            all_rids.extend(rids)
            all_texts.extend(texts)

        vectors = (
            self._encode_embedder(embedder, all_texts)
            if all_rids else None
        )

        with self._collection_write_lock(tenant, collection):
            self._load_or_init(tenant, collection)
            col_db = self._dbs[key]
            backend = self._emb[key]
            current_embedder = self._collection_embedders[key]

            for docid, rids, texts, chunk_rows, safe_doc_meta in docs_to_commit:
                purged = self._purge_doc_locked(
                    tenant,
                    collection,
                    docid,
                )
                if purged:
                    log.debug(
                        "INGEST-REPLACE tenant=%s coll=%s "
                        "docid=%s purged_chunks=%s",
                        tenant,
                        collection,
                        docid,
                        purged,
                    )
                    changed = True

                results.append(
                    IndexResult(indexed_chunks=len(rids), purged_chunks=purged)
                )
                if not rids:
                    continue

                for rid, text in zip(rids, texts, strict=False):
                    self._save_chunk_text(tenant, collection, rid, text)
                col_db.upsert_chunks(docid, chunk_rows, doc_meta=safe_doc_meta)
                changed = True

            if all_rids:
                self._mark_index_dirty(tenant, collection)
                if current_embedder is not embedder:
                    vectors = self._encode_embedder(
                        current_embedder,
                        all_texts,
                    )
                assert vectors is not None
                backend.add(all_rids, vectors)
                _rids = all_rids[:3]
                _sfx = " ..." if len(all_rids) > 3 else ""
                log.debug(
                    f"INGEST-BATCH-PREPARED: {len(all_rids)} "
                    f"chunks {_rids}{_sfx}"
                )

            if all_rids:
                self._register_catalog_collection(tenant, collection)

        return results

    def search(self, tenant: str, collection: str, query: str, k: int = 5,
               filters: FilterSpec | None = None,
               *, query_vector: list[float] | None = None) -> SearchOutput:
        """
        Queries the FAISS backend for top-k and keeps overfetch inside the
        store.

        Search holds the shared collection read lock while touching cached
        backend, metadata, and chunk files. Collection mutation/delete/restore
        take the write side, so same-collection searches can overlap without
        racing cache close or data-dir replacement.
        """
        _fspec_str = "N/A" if filters is None else json.dumps(dict(filters))
        log.debug(f"SEARCH-IN: tenant> {tenant} coll> {collection} query> {query} \
                  k> {k} filters> {_fspec_str}")
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

        def _backend_count(backend: FaissBackend) -> int | None:
            count = getattr(backend, "count", None)
            if callable(count):
                try:
                    return max(0, int(count()))
                except Exception:
                    return None
            index = getattr(backend, "_index", None)
            total = getattr(index, "ntotal", None)
            if total is None:
                return None
            try:
                return max(0, int(total))
            except Exception:
                return None

        def _search_loaded(
            backend: FaissBackend,
            col_db: CollectionDB,
            q_vec: Any,
            embed_ms: float,
        ) -> SearchOutput:
            total = _backend_count(backend)
            limit = fetch_k
            if total is not None:
                limit = min(limit, total)
            search_ms = 0.0
            filter_push_ms = 0.0
            hydrate_meta_ms = 0.0
            filter_post_ms = 0.0
            raw: list[tuple[str, float]] = []
            candidate_rids: list[str] = []
            meta_batch: dict[str, Metadata] = {}
            kept: list[tuple[str, float]] = []

            while True:
                t0 = _perf()
                raw = backend.search(q_vec, limit)
                search_ms += _ms_since(t0)
                raw_count = len(raw)
                candidate_rids = [rid for rid, _score in raw if rid]

                if normed_filters:
                    surviving, meta_batch, meta_timing = (
                        col_db.filter_meta_batch(
                            candidate_rids,
                            normed_filters,
                        )
                    )
                    raw = [
                        (rid, score)
                        for rid, score in raw
                        if rid in surviving
                    ]
                    candidate_rids = [rid for rid, _score in raw if rid]
                    filter_push_ms += meta_timing["filter_ms"]
                    hydrate_meta_ms += meta_timing["hydrate_ms"]
                else:
                    t0 = _perf()
                    meta_batch = self._read_meta_batch_safe(
                        tenant,
                        collection,
                        candidate_rids,
                    )
                    hydrate_meta_ms += _ms_since(t0)

                kept = []
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
                filter_post_ms += _ms_since(t0)

                if not normed_filters or len(kept) >= kk:
                    break
                exhausted_by_total = total is not None and limit >= total
                exhausted_by_result = raw_count < limit
                if exhausted_by_total or exhausted_by_result:
                    break

                next_limit = max(limit * 2, limit + kk)
                if total is not None:
                    next_limit = min(next_limit, total)
                if next_limit <= limit:
                    break
                log.debug(
                    f"SEARCH-FILTER-EXPAND: hits={len(kept)} "
                    f"k={kk} fetch_k={limit}->{next_limit}"
                )
                limit = next_limit

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
                    "search_ms": round(search_ms, 2),
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

            if query_vector is not None:
                # BYOV: caller supplied a raw query vector; skip the embed.
                q_vec = np.asarray(query_vector, dtype=np.float32)
                embed_ms = 0.0
            else:
                t0 = _perf()
                q_vec, query_cache_hit = self._encode_query_cached(
                    tenant,
                    collection,
                    embedder,
                    query,
                )
                embed_ms = 0.0 if query_cache_hit else _ms_since(t0)

            with self._collection_read_lock(tenant, collection):
                backend = self._emb.get(key)
                col_db = self._dbs.get(key)
                current_embedder = self._collection_embedders.get(key)
                if (
                    backend is not None
                    and col_db is not None
                    and current_embedder is embedder
                ):
                    self._touch_cache_entry(self._emb, key)
                    self._touch_cache_entry(self._dbs, key)
                    return _search_loaded(
                        backend,
                        col_db,
                        q_vec,
                        embed_ms,
                    )
            # Caches drained, or a delete+recreate swapped the embedder between
            # encode and search (a different vector space, so q_vec would have
            # the wrong dimension for the new backend). Reload and retry, which
            # re-encodes against the current embedder. R3-23.
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

    def _flush_caches(self) -> None:
        with self._state_lock.write_lock():
            with self._catalog_bootstrap_guard:
                old_dbs = list(self._dbs.values())
                old_backends = list(self._emb.items())
                with self._catalog_guard:
                    old_catalog = self._catalog
                    self._catalog = self._catalog_factory()
                    self._catalog_path = None
                    self._catalog_bootstrapped = False
                if old_catalog._conn is not None:
                    try:
                        old_catalog.close()
                    except Exception:
                        pass
            self._dbs.clear()
            self._emb.clear()
            self._collection_embedders.clear()
            self._drop_query_vector_cache()

            if not old_dbs and not old_backends:
                return

            def _close_all() -> None:
                for db in old_dbs:
                    try:
                        db.close()
                    except Exception:
                        pass
                for (tenant, collection), backend in old_backends:
                    try:
                        backend.close()
                        self._clear_index_dirty(tenant, collection)
                    except Exception:
                        pass

            _close_all()

    def close(self) -> None:
        """Close open collection handles and cached runtime embedders."""
        if self._closed:
            return
        self._flush_caches()
        embedders = list(self._embedder_cache.values())
        if all(embedder is not self._embedder for embedder in embedders):
            embedders.append(self._embedder)
        self._embedder_cache.clear()

        seen: set[int] = set()
        for embedder in embedders:
            marker = id(embedder)
            if marker in seen:
                continue
            seen.add(marker)
            close = getattr(embedder, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        self._closed = True

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
            if rel_path.name.endswith(("-wal", "-shm")):
                raise ValueError(f"archive contains SQLite sidecar: {name}")

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

    def _replace_archive_tree(
        self,
        data_dir: Path,
        staged_dir: Path,
    ) -> None:
        backup_dir = Path(
            tempfile.mkdtemp(prefix=_RESTORE_BACKUP_PREFIX, dir=data_dir)
        )
        old_entries = [
            entry for entry in data_dir.iterdir() if entry != backup_dir
        ]
        moved_old: list[str] = []
        installed: list[str] = []
        try:
            for entry in old_entries:
                shutil.move(str(entry), str(backup_dir / entry.name))
                moved_old.append(entry.name)
            for entry in staged_dir.iterdir():
                installed.append(entry.name)
                shutil.move(str(entry), str(data_dir / entry.name))
            self._remove_path(backup_dir)
        except Exception as exc:
            rollback_errors: list[Exception] = []
            for name in reversed(installed):
                try:
                    self._remove_path(data_dir / name)
                except Exception as rollback_exc:
                    rollback_errors.append(rollback_exc)
            for name in moved_old:
                backup_entry = backup_dir / name
                try:
                    if not (backup_entry.exists() or backup_entry.is_symlink()):
                        raise FileNotFoundError(backup_entry)
                    shutil.move(str(backup_entry), str(data_dir / name))
                except Exception as rollback_exc:
                    rollback_errors.append(rollback_exc)
            if not rollback_errors:
                try:
                    self._remove_path(backup_dir)
                except Exception as rollback_exc:
                    rollback_errors.append(rollback_exc)
            if rollback_errors:
                if not backup_dir.exists():
                    with suppress(OSError):
                        backup_dir.mkdir()
                raise RuntimeError(
                    "archive restore failed and rollback is incomplete"
                ) from exc
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
            self._flush_caches()
            self._preflight_archive(data_dir_path)

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

            with tempfile.TemporaryDirectory(prefix="pavedb_import_") as tmp_dir:
                tmp_path = Path(tmp_dir)
                archive_path = tmp_path / "pavedb-data.zip"
                extract_dir = tmp_path / "extracted"
                archive_path.write_bytes(archive_bytes)
                extract_dir.mkdir()

                try:
                    with zipfile.ZipFile(archive_path, "r") as zf:
                        self._validate_zip_members(zf)
                        zf.extractall(extract_dir)
                except zipfile.BadZipFile as exc:
                    raise ValueError("invalid archive") from exc

                self._preflight_archive(extract_dir, validate_destination=True)
                self._assert_no_restore_backup(data_dir_path)
                data_dir_path.mkdir(parents=True, exist_ok=True)
                self._flush_caches()
                self._replace_archive_tree(data_dir_path, extract_dir)
                self._ensure_catalog().rotate_metrics_generation()
