# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""On-disk store implementation.

`LocalStore` is the default `BaseStore` for single-instance
deployments. It owns the data directory layout
(`{data_dir}/{tenant}/{collection}/{meta.db, index/}`),
the per-collection read/write lock lattice, the catalog
(`pave.metadb.CatalogDB`) bootstrap pass, and the runtime embedder cache.
Collections persist a vector-space identity; instance routing stays in config.

See `docs/reference/protocols.md` for the frozen v1.0 contract on
the embedder and vector-backend seams this store composes.
"""

from __future__ import annotations
import errno
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack, contextmanager, suppress
from datetime import datetime, date, timezone as tz
from pathlib import Path
from threading import Condition, Lock, Thread, current_thread, get_ident
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
    COLLECTION_SCHEMA_VERSION,
    CatalogDB,
    ChunkLimitError,
    CollectionConfigRecoveryError,
    CollectionDB,
    RateLimitDecision,
    ReindexJobStateError,
    UnsupportedSchemaVersionError,
    validate_collection_config,
)
from pave.stores.base import (
    BaseStore,
    ChunkInfo,
    CollectionInfo,
    ContentFilterSpec,
    DocumentInfo,
    FilterSpec,
    IndexResult,
    Metadata,
    MetadataValidationError,
    QueryHomeEntry,
    QueryLogEntry,
    Record,
    SearchMode,
    SearchOutput,
    SearchResult,
)
from pave.config import get_cfg, get_logger
from pave.data_dir_lease import DATA_DIR_LOCK_NAME
from pave.metrics import inc_labeled as m_inc_labeled
from pave.version import VERSION

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
_COLLECTION_ARCHIVE_FORMAT = "pavedb.collection"
_COLLECTION_ARCHIVE_VERSION = 1
_COLLECTION_ARCHIVE_MANIFEST = "manifest.json"
_COLLECTION_RESTORE_BACKUP_PREFIX = ".pavedb-collection-restore-backup-"
_REINDEX_DIR = ".pavedb-reindex"
_REINDEX_BATCH_SIZE = 32

# P1-07: exact-token boost tokenizer. Word-boundary matching only, no
# stemming/normalization beyond casefolding -- the boost is a narrow nudge
# inside the vector candidate window, not a lexical search engine.
_EXACT_TOKEN_RE = re.compile(r"\w+")
_RRF_K = 60


def _tokenize_exact(text: str | None) -> frozenset[str]:
    if not text:
        return frozenset()
    return frozenset(m.group(0).casefold() for m in _EXACT_TOKEN_RE.finditer(text))


def _reciprocal_rank_fusion(
    vector: list[tuple[str, float]],
    lexical: list[tuple[str, float]],
) -> list[tuple[str, float, int | None, int | None]]:
    """Fuse independent result ranks without mixing their raw score scales."""
    vector_ranks: dict[str, int] = {}
    lexical_ranks: dict[str, int] = {}
    for rank, (rid, _score) in enumerate(vector, start=1):
        if rid:
            vector_ranks.setdefault(rid, rank)
    for rank, (rid, _score) in enumerate(lexical, start=1):
        if rid:
            lexical_ranks.setdefault(rid, rank)

    fused: list[tuple[str, float, int | None, int | None]] = []
    for rid in vector_ranks.keys() | lexical_ranks.keys():
        vector_rank = vector_ranks.get(rid)
        lexical_rank = lexical_ranks.get(rid)
        score = sum(
            1.0 / (_RRF_K + rank)
            for rank in (vector_rank, lexical_rank)
            if rank is not None
        )
        fused.append((rid, score, vector_rank, lexical_rank))
    fused.sort(
        key=lambda item: (
            -item[1],
            item[2] if item[2] is not None else float("inf"),
            item[0],
        )
    )
    return fused


def _assert_writable_rid(rid: str) -> None:
    """Reject control characters and invalid Unicode in a chunk id.

    Lone surrogates cannot be encoded by the JSON, SQLite, and archive
    boundaries a record crosses; reject them before a write starts.
    """
    for ch in rid:
        code = ord(ch)
        if code < 0x20 or code == 0x7F or 0xD800 <= code <= 0xDFFF:
            raise ValueError(f"invalid chunk id: {rid!r}")


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

    Each collection maps to a directory holding a SQLite metadata/text DB
    and a FAISS index. Collection lifecycle and
    document writes acquire a per-collection write lock; reads take
    a shared read lock and pin cached handles against concurrent
    writers. The embedder cache is keyed by runtime spec, while compatible
    collection vector spaces are assigned according to the configured routing
    policy.
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
        self._embedder_route_offsets: dict[str, int] = {}
        self._embedder_route_guard = Lock()
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
        self._reindex_threads: dict[str, Thread] = {}
        self._reindex_threads_lock = Lock()
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
        routing_policy = embedder_factory_mod.embedder_routing_policy()
        log.info(
            "Default embedder resolved selector=%s vector_key=%s "
            "instances=%s routing=%s",
            default_resolution.selector,
            default_resolution.vector_key,
            ",".join(instance.key for instance in default_resolution.instances),
            routing_policy,
        )
        default_key = embedder_factory_mod.embedder_cache_key(
            default_resolution.instances[0].spec
        )
        self._embedder_cache[default_key] = embedder
        self._ensure_catalog()
        self._recover_reindex_jobs()

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
        *,
        expected_dim: int | None = None,
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
                or (expected_dim is not None and int(index.d) != expected_dim)
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
        scratch_dir: bool = False,
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
                query_home_columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(query_home)")
                }
        except sqlite3.Error:
            raise ValueError("invalid archive catalog") from None
        if schema_version == 0:
            raise ValueError("invalid archive catalog")
        if not (
            {"query_id", "tenant", "collection", "created_at"}
            <= query_home_columns
        ):
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
                    metadata.open(metadata_path)
                    self._migrate_chunk_text_sidecars_in(
                        collection_dir,
                        metadata,
                    )
                    metadata.verify_text_indexes()
                    with metadata._reader() as conn:
                        required_columns = {
                            "documents": {
                                "docid", "version", "ingested_at", "meta_json"
                            },
                            "chunks": {
                                "docid", "rid", "chunk_path", "meta_json",
                                "ingested_at", "text",
                            },
                            "chunk_text_fts": {"text"},
                            "chunk_text_trigram": {"text"},
                            "chunk_meta": {"rid", "key", "value"},
                            "document_meta": {"docid", "key", "value"},
                            "query_log": {
                                "query_id", "tenant", "collection", "actor",
                                "query_text", "k", "filters_json", "include_common",
                                "content_filter_json", "mode",
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
                        collection_schema_version = CollectionDB._schema_version(
                            conn
                        )
                    metadata.checkpoint_wal()
                except Exception:
                    raise ValueError(
                        "invalid archive collection metadata database: "
                        f"{tenant}/{collection}"
                    ) from None
                finally:
                    metadata.close()
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
                        # Recovered from an orphaned collection dir with no
                        # catalog row; the collection's own meta.db knows its
                        # schema version, but not the PaveDB build that wrote
                        # it, so pavedb_version stays unset (P2-25).
                        collection_schema_version=collection_schema_version,
                    )
                    config = sidecar
                elif catalog_config != sidecar:
                    raise ValueError(
                        "archive catalog and sidecar conflict: "
                        f"{tenant}/{collection}"
                    )
                else:
                    config = catalog_config
                # Only embedder types that pin `dim` into their vector-space
                # config (identity, openai, ollama) are checkable here;
                # others (e.g. sbert) only know their dimension once the
                # model loads, which preflight avoids.
                self._validate_archive_index(
                    collection_dir,
                    tenant,
                    collection,
                    rids,
                    expected_dim=(
                        int(config["embedder_config"]["dim"])
                        if "dim" in config["embedder_config"]
                        else None
                    ),
                )
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
            if scratch_dir:
                for suffix in ("-wal", "-shm"):
                    with suppress(FileNotFoundError):
                        (data_dir / f"catalog.db{suffix}").unlink()

    def _resolve_collection_config(
        self,
        *,
        embedder: str | None = None,
        embedder_type: str | None = None,
        embed_model: str | None = None,
        embedder_config: EmbedderConfig | None = None,
    ) -> CollectionConfig:
        cfg = get_cfg()
        backend_type = str(cfg.get("vector_store.type")).lower()
        embedder_spec = embedder_factory_mod.resolve_embedder_spec(
            cfg,
            selector=embedder,
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
        max_collections: int = 0,
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
                pavedb_version=VERSION,
                collection_schema_version=COLLECTION_SCHEMA_VERSION,
                max_collections=max_collections,
            ),
        )

    @staticmethod
    def _tenant_limit(tenant: str, field: str) -> int:
        """Per-tenant cap named *field*; tenants.yml override, else the
        tenants.default_<field> global, 0 = unlimited."""
        tenants_cfg = get_cfg().get("tenants") or {}
        if isinstance(tenants_cfg, dict):
            tenant_cfg = tenants_cfg.get(tenant)
            if isinstance(tenant_cfg, dict):
                override = tenant_cfg.get(field)
                if override is not None:
                    return int(override)
        raw = get_cfg().get(f"tenants.default_{field}")
        return int(raw) if raw is not None else 0

    def _tenant_max_collections(self, tenant: str) -> int:
        """Collection-count cap. Enforced only on explicit create, never on
        catalog bootstrap, rename, or archive restore."""
        return self._tenant_limit(tenant, "max_collections")

    def charge_rate_limit(
        self,
        tenant: str,
        operation: str,
        limits: dict[int, int],
        *,
        now: float | None = None,
    ) -> RateLimitDecision:
        """Charge persistent request-rate windows in the global catalog."""
        return self._ensure_catalog().charge_rate_limit(
            tenant,
            operation,
            limits,
            now=now,
        )

    def charge_operation_window(
        self,
        tenant: str,
        op: str,
        field: str,
        window_seconds: int,
    ):
        """Charge one use of an expensive op against its tenant window.

        The cap comes from the one number-resolution path
        (`tenants.<name>.<field>` override, else `tenants.default_<field>`,
        0 = unlimited → None); the charge goes through the one persistent
        rate engine. Returns the decision, or None when unlimited.
        """
        cap = self._tenant_limit(tenant, field)
        if cap <= 0:
            return None
        return self.charge_rate_limit(tenant, op, {window_seconds: cap})

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
            # No row means this call is what brings the collection into
            # existence, so the per-tenant cap applies exactly as it does on
            # an explicit create. It used to take the 0 = unlimited default,
            # which is how read paths minted collections past the quota (B33).
            self._register_catalog_collection(
                tenant,
                collection,
                cfg,
                max_collections=self._tenant_max_collections(tenant),
            )
        return cfg

    def _get_embedder_for_config(
        self,
        cfg: CollectionConfig,
        *,
        selector: str | None = None,
    ) -> Embedder:
        if selector is not None:
            # Per-operation override: the caller named a configured instance
            # (validated against this collection's vector space by the
            # factory). One candidate, no failover probe — a bad selector
            # must fail loudly, not silently route elsewhere.
            runtime = embedder_factory_mod.runtime_embedder_spec_for_selector(
                get_cfg(),
                cfg,
                selector,
            )
            key = embedder_factory_mod.embedder_cache_key(runtime)
            embedder = self._embedder_cache.get(key)
            if embedder is None:
                embedder = self._embedder_factory(
                    str(runtime["embedder_type"]),
                    str(runtime["embed_model"]),
                    dict(runtime.get("embedder_config") or {}),
                )
                self._embedder_cache[key] = embedder
            else:
                self._touch_cache_entry(self._embedder_cache, key)
            return embedder
        try:
            policy = embedder_factory_mod.embedder_routing_policy(get_cfg())
            route_index = 0
            if policy in {"round_robin", "weighted"}:
                vector_key = embedder_factory_mod.vector_space_key(cfg)
                with self._embedder_route_guard:
                    route_index = self._embedder_route_offsets.get(vector_key, 0)
                    self._embedder_route_offsets[vector_key] = route_index + 1
            runtimes = embedder_factory_mod.runtime_embedder_specs_for_vector(
                get_cfg(),
                cfg,
                route_index=route_index,
            )
        except EmbedderUnavailableError:
            raise
        except Exception as exc:
            raise EmbedderUnavailableError(
                f"embedder {cfg.get('embedder_type')!s}:"
                f"{cfg.get('embed_model')!s} could not be routed: "
                f"{exc}"
            ) from exc

        last_error: Exception | None = None
        for runtime in runtimes:
            key = embedder_factory_mod.embedder_cache_key(runtime)
            embedder = self._embedder_cache.get(key)
            try:
                if embedder is None:
                    embedder = self._embedder_factory(
                        str(runtime["embedder_type"]),
                        str(runtime["embed_model"]),
                        dict(runtime.get("embedder_config") or {}),
                    )
                    self._embedder_cache[key] = embedder
                else:
                    self._touch_cache_entry(self._embedder_cache, key)
                if policy == "failover" and len(runtimes) > 1:
                    int(embedder.dim)
                return embedder
            except Exception as exc:
                last_error = exc
                if embedder is not None:
                    self._drop_cached_embedder(embedder)
                if policy != "failover" or len(runtimes) == 1:
                    if isinstance(exc, EmbedderUnavailableError):
                        raise
                    raise EmbedderUnavailableError(
                        f"embedder {cfg.get('embedder_type')!s}:"
                        f"{cfg.get('embed_model')!s} could not be "
                        f"initialised: {exc}"
                    ) from exc

        raise EmbedderUnavailableError(
            f"all compatible embedder instances failed for "
            f"{embedder_factory_mod.vector_space_key(cfg)}: {last_error}"
        ) from last_error

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
    def _encode_query_embedder(embedder: Embedder, texts: list[str]) -> Any:
        """Query-side encode: uses the optional `encode_queries`
        capability (task-prefixed models) when the embedder offers it."""
        encode = getattr(embedder, "encode_queries", None)
        if encode is None:
            return LocalStore._encode_embedder(embedder, texts)
        try:
            return encode(texts)
        except EmbedderUnavailableError:
            raise
        except Exception as exc:
            raise EmbedderUnavailableError(
                f"embedder encode failed: {exc}"
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
    def _backend_vector_for_rid(
        backend: FaissBackend,
        rid: str,
        dim: int,
    ) -> Any | None:
        """Read one chunk's stored vector back out of the live index."""
        get_vector = getattr(backend, "get_vector", None)
        if not callable(get_vector):
            return None
        try:
            vector = get_vector(rid)
        except Exception:
            return None
        if vector is None:
            return None
        vector = np.asarray(vector, dtype=np.float32).reshape(-1)
        # A vector from an index of another width cannot be re-added, and
        # letting FAISS raise here would fail the whole collection load.
        if int(vector.shape[0]) != int(dim):
            return None
        return vector

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
            chunk_rids = col_db.get_chunk_rids()
        except Exception:
            return False
        return backend.rids() != chunk_rids

    def _rebuild_index_from_chunks(
        self,
        tenant: str,
        collection: str,
        backend: FaissBackend,
        col_db: CollectionDB,
        embedder: Embedder,
    ) -> FaissBackend:
        dim = int(embedder.dim)
        rids: list[str] = []
        texts: list[str] = []
        carried_rids: list[str] = []
        carried: list[Any] = []
        lost: list[str] = []
        for document in col_db.list_documents():
            docid = str(document.get("docid") or "")
            if not docid:
                continue
            for chunk in col_db.list_chunks(docid):
                rid = str(chunk.get("rid") or "")
                if not rid:
                    continue
                text = self._load_chunk_text(
                    tenant,
                    collection,
                    rid,
                    col_db=col_db,
                )
                if text is not None:
                    rids.append(rid)
                    texts.append(text)
                    continue
                # BYOV chunks keep NULL text, so the live index is
                # the only place their vector still exists; carry it over
                # instead of dropping the record from the rebuild.
                vector = self._backend_vector_for_rid(backend, rid, dim)
                if vector is None:
                    lost.append(rid)
                    continue
                carried_rids.append(rid)
                carried.append(vector)

        if lost:
            self._mark_index_dirty(tenant, collection)
            raise RuntimeError(
                "FAISS rebuild has unreconstructible chunks for "
                f"{tenant}/{collection}; refusing to publish a partial index"
            )

        rebuilt = FaissBackend(
            dim,
            storage_dir=self._index_dir(tenant, collection),
        )
        if rids:
            rebuilt.add(rids, self._encode_embedder(embedder, texts))
        if carried_rids:
            rebuilt.add(carried_rids, np.vstack(carried))
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

        coll_cfg = collection_config or self._collection_config_for_load(
            tenant,
            collection,
        )
        # After the catalog registration, not before: a registration refused
        # by the collection quota must not leave a directory behind (B33).
        os.makedirs(self._base_path(tenant, collection), exist_ok=True)
        embedder = self._get_embedder_for_config(coll_cfg)
        embedder_dim = self._embedder_dim_for_config(coll_cfg, embedder)
        idx_dir = self._index_dir(tenant, collection)

        backend = self._emb.get(key)
        if backend is None:
            backend = FaissBackend(
                embedder_dim,
                storage_dir=idx_dir,
            )

            index_path = idx_dir / "faiss.index"
            map_path = idx_dir / "id_map.json"
            try:
                backend.recover_interrupted_flush()
                if index_path.is_file() and not map_path.is_file():
                    raise ValueError("id_map.json is missing")
                backend.initialize()
            except Exception as exc:
                if index_path.is_file():
                    raise RuntimeError(
                        "Persisted FAISS state for "
                        f"{tenant}/{collection} is incomplete or invalid; "
                        "refusing to rebuild over faiss.index"
                    ) from exc
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
        try:
            if col_db is None:
                col_db = CollectionDB()
                col_db.open(self._db_path(tenant, collection))
                self._migrate_chunk_text_sidecars_in(
                    Path(self._base_path(tenant, collection)),
                    col_db,
                )
            if self._index_needs_rebuild(
                tenant,
                collection,
                backend,
                col_db,
            ):
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
        except Exception:
            if key not in self._dbs:
                col_db.close()
            raise

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

        vector = self._encode_query_embedder(embedder, [query])[0]
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
        embedder: str | None = None,
        embedder_type: str | None = None,
        embed_model: str | None = None,
        embedder_config: EmbedderConfig | None = None,
        display_name: str | None = None,
    ) -> CollectionInfo:
        requested = self._resolve_collection_config(
            embedder=embedder,
            embedder_type=embedder_type,
            embed_model=embed_model,
            embedder_config=embedder_config,
        )
        explicit = (
            embedder is not None
            or embedder_type is not None
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
                if stored is not None:
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
                    max_collections=self._tenant_max_collections(tenant),
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
            purged = self._with_catalog_retry(
                "purge_reindex_jobs_for_collection",
                lambda catalog: catalog.purge_reindex_jobs_for_collection(
                    tenant,
                    collection,
                ),
            )
            for job_id in purged:
                with suppress(Exception):
                    self._cleanup_reindex_job_dir(job_id)

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
            orphaned = self._with_catalog_retry(
                "move_reindex_jobs_for_collection",
                lambda catalog: catalog.move_reindex_jobs_for_collection(
                    tenant,
                    old_name,
                    new_name,
                ),
            )
            for job_id in orphaned:
                with suppress(Exception):
                    self._cleanup_reindex_job_dir(job_id)

    def _reindex_job_dir(self, job_id: str) -> Path:
        try:
            canonical = str(uuid.UUID(job_id))
        except (ValueError, AttributeError) as exc:
            raise ValueError("invalid reindex job id") from exc
        return Path(self._data_dir).resolve() / _REINDEX_DIR / canonical

    @staticmethod
    def _public_reindex_job(job: dict[str, Any]) -> dict[str, Any]:
        target = dict(job.get("target_config") or {})
        return {
            "job_id": job["job_id"],
            "tenant": job["tenant"],
            "collection": job["collection"],
            "status": job["status"],
            "target": {
                "embedder_type": target.get("embedder_type"),
                "embed_model": target.get("embed_model"),
                "embedder_config": dict(target.get("embedder_config") or {}),
            },
            "total_chunks": int(job.get("total_chunks") or 0),
            "completed_chunks": int(job.get("completed_chunks") or 0),
            "error": job.get("error"),
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
        }

    def _get_reindex_job_internal(self, job_id: str) -> dict[str, Any]:
        job = self._with_catalog_retry(
            "get_reindex_job",
            lambda catalog: catalog.get_reindex_job(job_id),
        )
        if job is None:
            raise FileNotFoundError(f"reindex job '{job_id}' does not exist")
        return job

    def get_reindex_job(self, job_id: str) -> dict[str, Any]:
        """Return durable progress for one collection reindex job."""
        return self._public_reindex_job(
            self._get_reindex_job_internal(job_id)
        )

    def start_reindex(
        self,
        tenant: str,
        collection: str,
        *,
        embedder_type: str | None = None,
        embed_model: str | None = None,
        embedder_config: EmbedderConfig | None = None,
    ) -> dict[str, Any]:
        """Create and launch a persistent, resumable collection reindex."""
        if self._closed:
            raise RuntimeError("store is closed")
        with self._collection_write_lock(tenant, collection):
            stored = self._with_catalog_retry(
                "get_collection_config",
                lambda catalog: catalog.get_collection_config(
                    tenant,
                    collection,
                ),
            )
            if stored is None or not os.path.isdir(
                self._base_path(tenant, collection)
            ):
                raise FileNotFoundError(
                    f"collection '{tenant}/{collection}' does not exist"
                )
            source = self._effective_collection_config(stored)
            if str(source["embedder_type"]).lower() == "identity":
                raise ValueError(
                    "identity/BYOV collections have no source text to reindex"
                )
            requested = self._resolve_collection_config(
                embedder_type=embedder_type,
                embed_model=embed_model,
                embedder_config=embedder_config,
            )
            target: CollectionConfig = {
                "backend_type": source["backend_type"],
                "backend_config": dict(source.get("backend_config") or {}),
                "embedder_type": requested["embedder_type"],
                "embed_model": requested["embed_model"],
                "embedder_config": dict(
                    requested.get("embedder_config") or {}
                ),
            }
            if str(target["embedder_type"]).lower() == "identity":
                raise ValueError(
                    "text collections cannot be reindexed to identity/BYOV"
                )
            if (
                embedder_factory_mod.vector_space_key(source)
                == embedder_factory_mod.vector_space_key(target)
            ):
                raise ValueError("target embedder matches the current vector space")
            self._validate_embedder_for_config(target)
            job_id = str(uuid.uuid4())
            job = self._with_catalog_retry(
                "create_reindex_job",
                lambda catalog: catalog.create_reindex_job(
                    job_id,
                    tenant,
                    collection,
                    source,
                    target,
                ),
            )
        try:
            self._launch_reindex_job(job_id)
        except Exception as exc:
            self._with_catalog_retry(
                "cancel_unlaunched_reindex_job",
                lambda catalog: catalog.update_reindex_job(
                    job_id,
                    expected_statuses=("queued",),
                    status="cancelled",
                    error=str(exc),
                    set_finished=True,
                ),
            )
            raise
        latest = self._with_catalog_retry(
            "get_reindex_job",
            lambda catalog: catalog.get_reindex_job(job_id),
        )
        return self._public_reindex_job(latest or job)

    def pause_reindex(self, job_id: str) -> dict[str, Any]:
        """Request a cooperative pause at the next persisted batch."""
        for _attempt in range(3):
            job = self._get_reindex_job_internal(job_id)
            status = str(job["status"])
            if status in {"paused", "pausing"}:
                return self._public_reindex_job(job)
            if status == "queued":
                next_status = "paused"
            elif status == "running":
                next_status = "pausing"
            else:
                raise ReindexJobStateError(
                    f"reindex job cannot be paused from status '{status}'"
                )
            changed = self._with_catalog_retry(
                "pause_reindex_job",
                lambda catalog: catalog.update_reindex_job(
                    job_id,
                    expected_statuses=(status,),
                    status=next_status,
                ),
            )
            if changed:
                if status == "queued":
                    self._join_reindex_thread(job_id)
                return self.get_reindex_job(job_id)
        raise ReindexJobStateError("reindex job state changed while pausing")

    def resume_reindex(self, job_id: str) -> dict[str, Any]:
        """Resume a paused or retryable failed job from its staged index."""
        for _attempt in range(3):
            job = self._get_reindex_job_internal(job_id)
            status = str(job["status"])
            if status in {"queued", "running"}:
                return self._public_reindex_job(job)
            if status not in {"paused", "failed"}:
                raise ReindexJobStateError(
                    f"reindex job cannot be resumed from status '{status}'"
                )
            changed = self._with_catalog_retry(
                "resume_reindex_job",
                lambda catalog: catalog.update_reindex_job(
                    job_id,
                    expected_statuses=(status,),
                    status="queued",
                    clear_error=True,
                    clear_finished=True,
                ),
            )
            if changed:
                try:
                    self._launch_reindex_job(job_id)
                except Exception as exc:
                    self._with_catalog_retry(
                        "fail_unlaunched_reindex_job",
                        lambda catalog: catalog.update_reindex_job(
                            job_id,
                            expected_statuses=("queued",),
                            status="failed",
                            error=str(exc),
                            set_finished=True,
                        ),
                    )
                    raise
                return self.get_reindex_job(job_id)
        raise ReindexJobStateError("reindex job state changed while resuming")

    def cancel_reindex(self, job_id: str) -> dict[str, Any]:
        """Cancel a job, preserving the collection's current live index."""
        for _attempt in range(3):
            job = self._get_reindex_job_internal(job_id)
            status = str(job["status"])
            if status in {"cancelled", "cancelling"}:
                return self._public_reindex_job(job)
            idle = status in {"queued", "paused", "failed"}
            if not idle and status not in {"running", "pausing"}:
                raise ReindexJobStateError(
                    f"reindex job cannot be cancelled from status '{status}'"
                )
            changed = self._with_catalog_retry(
                "cancel_reindex_job",
                lambda catalog: catalog.update_reindex_job(
                    job_id,
                    expected_statuses=(status,),
                    status="cancelling",
                ),
            )
            if not changed:
                continue
            if idle:
                try:
                    self._cleanup_reindex_job_dir(job_id)
                except Exception as exc:
                    self._with_catalog_retry(
                        "fail_cancel_reindex_job",
                        lambda catalog: catalog.update_reindex_job(
                            job_id,
                            expected_statuses=("cancelling",),
                            status="failed",
                            error=str(exc),
                            set_finished=True,
                        ),
                    )
                    raise
                self._with_catalog_retry(
                    "finish_cancel_reindex_job",
                    lambda catalog: catalog.update_reindex_job(
                        job_id,
                        expected_statuses=("cancelling",),
                        status="cancelled",
                        set_finished=True,
                    ),
                )
            return self.get_reindex_job(job_id)
        raise ReindexJobStateError("reindex job state changed while cancelling")

    def _launch_reindex_job(self, job_id: str) -> None:
        with self._reindex_threads_lock:
            if self._closed:
                raise RuntimeError("store is closed")
            prior = self._reindex_threads.get(job_id)
            if prior is not None and prior.is_alive():
                return
            worker = Thread(
                target=self._run_reindex_job,
                args=(job_id,),
                name=f"pavedb-reindex-{job_id[:8]}",
                daemon=True,
            )
            self._reindex_threads[job_id] = worker
            try:
                worker.start()
            except Exception:
                self._reindex_threads.pop(job_id, None)
                raise

    def _join_reindex_thread(self, job_id: str) -> None:
        with self._reindex_threads_lock:
            worker = self._reindex_threads.get(job_id)
        if worker is not None and worker is not current_thread():
            worker.join()

    def _discard_reindex_thread(self, job_id: str) -> None:
        with self._reindex_threads_lock:
            worker = self._reindex_threads.get(job_id)
            if worker is current_thread() or (
                worker is not None and not worker.is_alive()
            ):
                self._reindex_threads.pop(job_id, None)

    def _cleanup_reindex_job_dir(self, job_id: str) -> None:
        with suppress(ValueError):
            self._remove_path(self._reindex_job_dir(job_id))

    def _reindex_manifest_locked(
        self,
        tenant: str,
        collection: str,
        source_config: CollectionConfig,
    ) -> tuple[list[str], str]:
        self._load_or_init(
            tenant,
            collection,
            collection_config=source_config,
        )
        col_db = self._dbs[(tenant, collection)]
        digest = hashlib.sha256()
        digest.update(
            json.dumps(source_config, sort_keys=True).encode("utf-8")
        )
        rids: list[str] = []
        for document in col_db.list_documents():
            docid = str(document.get("docid") or "")
            for chunk in col_db.list_chunks(docid):
                rid = str(chunk.get("rid") or "")
                if not rid:
                    continue
                text = self._load_chunk_text(tenant, collection, rid)
                if text is None:
                    raise ValueError(
                        "collection contains chunks without source text; "
                        "BYOV chunks cannot be reindexed"
                    )
                rid_bytes = rid.encode("utf-8")
                text_bytes = text.encode("utf-8")
                digest.update(len(rid_bytes).to_bytes(8, "big"))
                digest.update(rid_bytes)
                digest.update(len(text_bytes).to_bytes(8, "big"))
                digest.update(text_bytes)
                rids.append(rid)
        return rids, digest.hexdigest()

    def _reindex_batch_texts(
        self,
        tenant: str,
        collection: str,
        rids: list[str],
    ) -> list[str]:
        texts: list[str] = []
        for rid in rids:
            text = self._load_chunk_text(tenant, collection, rid)
            if text is None:
                raise ValueError(
                    f"source text disappeared during reindex: {rid}"
                )
            texts.append(text)
        return texts

    def _settle_reindex_interrupt(
        self,
        job_id: str,
        status: str,
    ) -> bool:
        if status == "pausing":
            self._discard_reindex_thread(job_id)
            self._with_catalog_retry(
                "pause_reindex_job",
                lambda catalog: catalog.update_reindex_job(
                    job_id,
                    expected_statuses=("pausing",),
                    status="paused",
                ),
            )
            return True
        if status == "cancelling":
            self._discard_reindex_thread(job_id)
            self._with_catalog_retry(
                "cancel_reindex_job",
                lambda catalog: catalog.update_reindex_job(
                    job_id,
                    expected_statuses=("cancelling",),
                    status="cancelled",
                    set_finished=True,
                ),
            )
            return True
        return False

    def _reindex_stop_requested(self, job_id: str) -> bool:
        status = str(self._get_reindex_job_internal(job_id)["status"])
        if status == "cancelling":
            self._cleanup_reindex_job_dir(job_id)
        if self._settle_reindex_interrupt(job_id, status):
            return True
        return status != "running"

    def _run_reindex_job(self, job_id: str) -> None:
        staged: FaissBackend | None = None
        try:
            claimed = self._with_catalog_retry(
                "run_reindex_job",
                lambda catalog: catalog.update_reindex_job(
                    job_id,
                    expected_statuses=("queued",),
                    status="running",
                    set_started=True,
                ),
            )
            if not claimed:
                return
            job = self._get_reindex_job_internal(job_id)
            tenant = str(job["tenant"])
            collection = str(job["collection"])
            source = validate_collection_config(job["source_config"])
            target = validate_collection_config(job["target_config"])
            with self._collection_write_lock(tenant, collection):
                current = self._with_catalog_retry(
                    "get_collection_config",
                    lambda catalog: catalog.get_collection_config(
                        tenant,
                        collection,
                    ),
                )
                if current is None:
                    raise FileNotFoundError(
                        f"collection '{tenant}/{collection}' does not exist"
                    )
                current_config = self._effective_collection_config(current)
                if current_config != source:
                    raise ValueError(
                        "collection embedder changed since the job was created"
                    )
                if self._reindex_stop_requested(job_id):
                    return

                rids, source_digest = self._reindex_manifest_locked(
                    tenant,
                    collection,
                    source,
                )
                previous_digest = job.get("source_digest")
                if previous_digest and previous_digest != source_digest:
                    raise ValueError(
                        "collection contents changed while the job was paused"
                    )
                if self._reindex_stop_requested(job_id):
                    return

                maintenance_selector = None
                if get_cfg().get("embedder.routing.priority.maintenance"):
                    maintenance_selector = (
                        embedder_factory_mod.priority_embedder_selector(
                            get_cfg(),
                            target,
                            "maintenance",
                        )
                    )
                target_embedder = self._get_embedder_for_config(
                    target,
                    selector=maintenance_selector,
                )
                target_dim = self._embedder_dim_for_config(
                    target,
                    target_embedder,
                )
                if self._reindex_stop_requested(job_id):
                    return
                stage_dir = self._reindex_job_dir(job_id) / "index"
                stage_dir.mkdir(parents=True, exist_ok=True)
                staged = FaissBackend(target_dim, storage_dir=stage_dir)
                try:
                    staged.initialize()
                    staged_rids = staged.rids()
                    completed = len(staged_rids)
                    if staged_rids != set(rids[:completed]):
                        raise ValueError(
                            "staged reindex progress is inconsistent"
                        )
                except Exception:
                    staged.discard()
                    with suppress(Exception):
                        staged.close()
                    self._remove_path(stage_dir)
                    stage_dir.mkdir(parents=True, exist_ok=True)
                    staged = FaissBackend(
                        target_dim,
                        storage_dir=stage_dir,
                    )
                    staged.initialize()
                    staged_rids = set()
                completed = len(staged_rids)
                self._with_catalog_retry(
                    "update_reindex_progress",
                    lambda catalog: catalog.update_reindex_job(
                        job_id,
                        expected_statuses=(
                            "running",
                            "pausing",
                            "cancelling",
                        ),
                        source_digest=source_digest,
                        total_chunks=len(rids),
                        completed_chunks=completed,
                    ),
                )

                while completed < len(rids):
                    status = str(
                        self._get_reindex_job_internal(job_id)["status"]
                    )
                    if status in {"pausing", "cancelling"}:
                        staged.close()
                        staged = None
                        if status == "cancelling":
                            self._cleanup_reindex_job_dir(job_id)
                        self._settle_reindex_interrupt(job_id, status)
                        return
                    if status != "running":
                        return
                    batch_rids = rids[
                        completed : completed + _REINDEX_BATCH_SIZE
                    ]
                    texts = self._reindex_batch_texts(
                        tenant,
                        collection,
                        batch_rids,
                    )
                    vectors = self._encode_embedder(target_embedder, texts)
                    staged.add(batch_rids, vectors)
                    staged.flush()
                    completed += len(batch_rids)
                    self._with_catalog_retry(
                        "update_reindex_progress",
                        lambda catalog: catalog.update_reindex_job(
                            job_id,
                            expected_statuses=(
                                "running",
                                "pausing",
                                "cancelling",
                            ),
                            completed_chunks=completed,
                        ),
                    )

                status = str(self._get_reindex_job_internal(job_id)["status"])
                if status in {"pausing", "cancelling"}:
                    staged.close()
                    staged = None
                    if status == "cancelling":
                        self._cleanup_reindex_job_dir(job_id)
                    self._settle_reindex_interrupt(job_id, status)
                    return
                if status != "running":
                    return
                committing = self._with_catalog_retry(
                    "commit_reindex_job",
                    lambda catalog: catalog.update_reindex_job(
                        job_id,
                        expected_statuses=("running",),
                        status="committing",
                    ),
                )
                if not committing:
                    status = str(
                        self._get_reindex_job_internal(job_id)["status"]
                    )
                    staged.close()
                    staged = None
                    if status == "cancelling":
                        self._cleanup_reindex_job_dir(job_id)
                    self._settle_reindex_interrupt(job_id, status)
                    return
                staged.close()
                staged = None
                self._install_reindex(
                    job_id,
                    tenant,
                    collection,
                    source,
                    target,
                    len(rids),
                )
        except Exception as exc:
            self._fail_reindex_job(job_id, exc)
        finally:
            if staged is not None:
                with suppress(Exception):
                    staged.close()
            self._discard_reindex_thread(job_id)

    def _set_collection_embedder_config(
        self,
        tenant: str,
        collection: str,
        config: CollectionConfig,
    ) -> None:
        self._write_collection_config(tenant, collection, config)
        self._with_catalog_retry(
            "update_collection_embedder",
            lambda catalog: catalog.update_collection_embedder(
                tenant,
                collection,
                embedder_type=str(config["embedder_type"]),
                embed_model=str(config["embed_model"]),
                embed_config=dict(config.get("embedder_config") or {}),
            ),
        )

    def _install_reindex(
        self,
        job_id: str,
        tenant: str,
        collection: str,
        source: CollectionConfig,
        target: CollectionConfig,
        total_chunks: int,
    ) -> None:
        key = (tenant, collection)
        job_dir = self._reindex_job_dir(job_id)
        staged_index = job_dir / "index"
        backup_index = job_dir / "previous-index"
        live_index = self._index_dir(tenant, collection)
        if not staged_index.is_dir():
            raise RuntimeError("completed reindex stage is missing")

        old_backend = self._emb.pop(key, None)
        if old_backend is not None:
            old_backend.close()
        self._drop_collection_embedder(key)
        self._drop_query_vector_cache(tenant, collection)

        try:
            if backup_index.exists():
                self._remove_path(backup_index)
            if live_index.exists():
                os.rename(live_index, backup_index)
            os.rename(staged_index, live_index)
            self._set_collection_embedder_config(
                tenant,
                collection,
                target,
            )
            self._load_or_init(
                tenant,
                collection,
                collection_config=target,
            )
            changed = self._with_catalog_retry(
                "finish_reindex_job",
                lambda catalog: catalog.update_reindex_job(
                    job_id,
                    expected_statuses=("committing",),
                    status="succeeded",
                    completed_chunks=total_chunks,
                    clear_error=True,
                    set_finished=True,
                ),
            )
            if not changed:
                raise RuntimeError("reindex job lost its commit claim")
        except Exception as exc:
            target_backend = self._emb.pop(key, None)
            if target_backend is not None:
                discard = getattr(target_backend, "discard", None)
                if callable(discard):
                    discard()
                with suppress(Exception):
                    target_backend.close()
            self._drop_collection_embedder(key)
            self._drop_query_vector_cache(tenant, collection)
            if live_index.exists() and not staged_index.exists():
                os.rename(live_index, staged_index)
            if backup_index.exists():
                os.rename(backup_index, live_index)
            self._set_collection_embedder_config(
                tenant,
                collection,
                source,
            )
            self._load_or_init(
                tenant,
                collection,
                collection_config=source,
            )
            self._discard_reindex_thread(job_id)
            rolled_back = self._with_catalog_retry(
                "rollback_reindex_job",
                lambda catalog: catalog.update_reindex_job(
                    job_id,
                    expected_statuses=("committing",),
                    status="paused",
                    error=str(exc),
                ),
            )
            if rolled_back:
                return
            raise
        self._remove_path(job_dir)

    def _fail_reindex_job(self, job_id: str, exc: Exception) -> None:
        with suppress(Exception):
            job = self._get_reindex_job_internal(job_id)
            status = str(job["status"])
            reported_exc = exc
            if status == "cancelling":
                try:
                    self._cleanup_reindex_job_dir(job_id)
                except Exception as cleanup_exc:
                    reported_exc = cleanup_exc
                    next_status = "failed"
                else:
                    next_status = "cancelled"
            elif status == "pausing":
                next_status = "paused"
            elif status == "committing":
                next_status = "committing"
            elif status in {"cancelled", "succeeded", "paused"}:
                return
            else:
                next_status = "failed"
            self._discard_reindex_thread(job_id)
            self._with_catalog_retry(
                "fail_reindex_job",
                lambda catalog: catalog.update_reindex_job(
                    job_id,
                    expected_statuses=(status,),
                    status=next_status,
                    error=(
                        None
                        if next_status == "cancelled"
                        else str(reported_exc)
                    ),
                    clear_error=next_status == "cancelled",
                    set_finished=next_status in {"failed", "cancelled"},
                ),
            )

    @staticmethod
    def _valid_reindex_index(path: Path, expected_count: int) -> bool:
        index_path = path / "faiss.index"
        map_path = path / "id_map.json"
        if not index_path.is_file() or not map_path.is_file():
            return False
        try:
            index = faiss.read_index(str(index_path))
            mapping = json.loads(map_path.read_text(encoding="utf-8"))
            rid_to_id = {
                str(rid): int(index_id)
                for rid, index_id in mapping["rid_to_id"].items()
            }
            id_to_rid = {
                int(index_id): str(rid)
                for index_id, rid in mapping["id_to_rid"].items()
            }
            index_ids = {
                int(index_id)
                for index_id in faiss.vector_to_array(index.id_map)
            }
            next_id = int(mapping["next_id"])
            return (
                index.metric_type == faiss.METRIC_INNER_PRODUCT
                and int(index.ntotal) == expected_count
                and len(rid_to_id) == expected_count
                and len(id_to_rid) == expected_count
                and {index_id: rid for rid, index_id in rid_to_id.items()}
                == id_to_rid
                and index_ids == set(id_to_rid)
                and next_id >= 0
                and next_id > max(id_to_rid, default=-1)
            )
        except Exception:
            return False

    def _recover_reindex_jobs(self) -> None:
        committing = self._with_catalog_retry(
            "list_committing_reindex_jobs",
            lambda catalog: catalog.list_reindex_jobs(("committing",)),
        )
        for job in committing:
            job_id = str(job["job_id"])
            tenant = str(job["tenant"])
            collection = str(job["collection"])
            job_dir = self._reindex_job_dir(job_id)
            staged = job_dir / "index"
            backup = job_dir / "previous-index"
            live = self._index_dir(tenant, collection)
            total = int(job.get("total_chunks") or 0)
            try:
                if staged.is_dir() and not backup.exists():
                    source = validate_collection_config(job["source_config"])
                    self._set_collection_embedder_config(
                        tenant,
                        collection,
                        source,
                    )
                    self._with_catalog_retry(
                        "recover_reindex_job",
                        lambda catalog: catalog.update_reindex_job(
                            job_id,
                            expected_statuses=("committing",),
                            status="paused",
                            error="interrupted before index installation",
                        ),
                    )
                    continue
                if backup.is_dir() and self._valid_reindex_index(live, total):
                    target = validate_collection_config(job["target_config"])
                    self._set_collection_embedder_config(
                        tenant,
                        collection,
                        target,
                    )
                    self._with_catalog_retry(
                        "recover_reindex_job",
                        lambda catalog: catalog.update_reindex_job(
                            job_id,
                            expected_statuses=("committing",),
                            status="succeeded",
                            completed_chunks=total,
                            clear_error=True,
                            set_finished=True,
                        ),
                    )
                    self._remove_path(job_dir)
                    continue
                if backup.is_dir():
                    resume_chunks = total if staged.is_dir() else 0
                    if live.exists():
                        self._remove_path(live)
                    os.rename(backup, live)
                    source = validate_collection_config(job["source_config"])
                    self._set_collection_embedder_config(
                        tenant,
                        collection,
                        source,
                    )
                    self._with_catalog_retry(
                        "recover_reindex_job",
                        lambda catalog: catalog.update_reindex_job(
                            job_id,
                            expected_statuses=("committing",),
                            status="paused",
                            completed_chunks=resume_chunks,
                            error="interrupted index installation was rolled back",
                        ),
                    )
                    continue
                self._with_catalog_retry(
                    "recover_reindex_job",
                    lambda catalog: catalog.update_reindex_job(
                        job_id,
                        expected_statuses=("committing",),
                        status="failed",
                        error="could not recover interrupted index installation",
                        set_finished=True,
                    ),
                )
            except Exception as exc:
                with suppress(Exception):
                    self._with_catalog_retry(
                        "recover_reindex_job",
                        lambda catalog: catalog.update_reindex_job(
                            job_id,
                            expected_statuses=("committing",),
                            error=f"reindex recovery failed: {exc}",
                        ),
                    )
                raise RuntimeError(
                    f"reindex recovery failed for job {job_id}"
                ) from exc
        self._with_catalog_retry(
            "interrupt_reindex_jobs",
            lambda catalog: catalog.interrupt_reindex_jobs(),
        )
        terminal = self._with_catalog_retry(
            "list_terminal_reindex_jobs",
            lambda catalog: catalog.list_reindex_jobs(
                ("cancelled", "succeeded"),
            ),
        )
        for job in terminal:
            self._cleanup_reindex_job_dir(str(job["job_id"]))

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
            "pavedb_version": cfg.get("pavedb_version"),
            "schema_version": cfg.get("collection_schema_version"),
            "doc_count": doc_count,
            "chunk_count": chunk_count,
        }

    def _cache_estimated_bytes(self) -> int:
        total = 0
        for backend in list(self._emb.values()):
            dim = getattr(backend, "_dimension", None)
            if dim is None:
                continue
            try:
                total += backend.count() * int(dim) * 4
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
        if not os.path.isdir(self._base_path(tenant, collection)):
            # Probing dim is a read: it must not create the collection (B33).
            # Answer from the default config rather than declining, because a
            # None here skips the caller's dim check and turns a clean 400
            # dim_mismatch into a backend blow-up.
            default_cfg = self._default_collection_config()
            return self._embedder_dim_for_config(
                default_cfg,
                self._get_embedder_for_config(default_cfg),
            )
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
        content_filter: ContentFilterSpec | None = None,
        mode: SearchMode = "vector",
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
                content_filter=content_filter,
                mode=mode,
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
                # Logging must not conjure the collection it logs into (B33).
                # A search of a name that does not exist answers empty; there
                # is no collection to hold its query log, so there is nothing
                # to write and nothing to create.
                if not os.path.isdir(self._base_path(tenant, collection)):
                    return
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
        if not os.path.isdir(self._base_path(tenant, collection)):
            # Deleting from a collection that does not exist stays the
            # documented idempotent no-op, but must not conjure it: loading
            # here created a directory and a catalog row on a read path, past
            # the per-tenant collection quota (B33).
            return 0
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
                # and searches hydrate text from the collection database.
                pass

        return len(ids)

    def _chunks_dir(self, tenant: str, collection: str) -> str:
        return os.path.join(self._base_path(tenant, collection), "chunks")

    @staticmethod
    def _migrate_chunk_text_sidecars_in(
        collection_dir: Path,
        col_db: CollectionDB,
    ) -> None:
        """Copy legacy sidecars into SQLite, verify, then remove the files."""
        rows = col_db.legacy_chunk_text_rows()
        if not rows:
            return

        base = collection_dir.resolve()
        expected: dict[str, str] = {}
        sidecars: set[Path] = set()
        for rid, chunk_path, current_text in rows:
            relative = Path(chunk_path)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"invalid legacy chunk path for {rid}")
            sidecar = base / relative
            if not sidecar.resolve().is_relative_to(base):
                raise ValueError(f"invalid legacy chunk path for {rid}")
            if sidecar.is_file():
                text = sidecar.read_bytes().decode("utf-8")
                if current_text is not None and current_text != text:
                    raise ValueError(
                        f"legacy chunk text does not match SQLite for {rid}"
                    )
                expected[rid] = text
                sidecars.add(sidecar)
            elif current_text is None:
                raise FileNotFoundError(
                    f"legacy chunk text is missing for {rid}: {chunk_path}"
                )
            else:
                expected[rid] = current_text

        pending = {
            rid: expected[rid]
            for rid, _chunk_path, current_text in rows
            if current_text is None
        }
        col_db.migrate_chunk_texts(pending)
        stored = col_db.get_chunk_texts(list(expected))
        if stored != expected:
            raise RuntimeError("legacy chunk-text migration verification failed")
        col_db.rebuild_text_indexes()
        col_db.verify_text_indexes()

        for sidecar in sorted(sidecars):
            sidecar.unlink()
        col_db.clear_chunk_paths(list(expected))
        with suppress(OSError):
            (base / "chunks").rmdir()

    def _urid_to_fname(self, urid: str) -> str:
        return (
            urid.replace("/", "_").replace("\\", "_").replace(":", "_") + ".txt"
        )

    def _load_chunk_text(
        self,
        tenant: str,
        collection: str,
        urid: str,
        *,
        col_db: CollectionDB | None = None,
    ) -> str | None:
        db = col_db or self._dbs.get((tenant, collection))
        if db is not None:
            try:
                text = db.get_chunk_text(urid)
            except Exception as exc:
                if not self._is_transient_db_read_error(exc):
                    raise
                # The cached db was closed underneath the search (a delete /
                # move / restore race). Recover the text through a fresh
                # read-only handle, the same fallback get_document takes via
                # _read_collection_db, instead of failing the whole search.
                text = None
                db_path = self._db_path(tenant, collection)
                if db_path.exists():
                    fallback = CollectionDB()
                    try:
                        fallback.open(db_path, read_only=True)
                        text = fallback.get_chunk_text(urid)
                    except Exception as fallback_exc:
                        if not self._is_transient_db_read_error(fallback_exc):
                            raise
                        text = None
                    finally:
                        fallback.close()
            if text is not None:
                return text

        # Compatibility path for an interrupted v6 -> v7 migration. Normal
        # v7 writes never create this file, and successful migration removes
        # it only after SQLite content and both FTS indexes verify.
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
            # Both callers prepare before they purge or commit, so rejecting an
            # invalid identifier here fails the whole request cleanly.
            # Checked mid-loop it would destroy the previous version of the
            # document (index_records) or leave earlier documents committed
            # without vectors (index_documents).
            _assert_writable_rid(rid)

            chunk_rows.append((rid, None, safe_meta))
            rids_to_add.append(rid)
            texts_to_encode.append(txt)

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
        embedder_selector: str | None = None,
    ) -> IndexResult:
        """
        Ingests records as (rid, text, meta). Guarantees non-null text, coerces
        dict-records, updates SQLite metadata, saves index. Thread critical.

        BYOV: when ``vectors`` is given (one raw vector per record) the embed
        step is skipped and SQLite stores NULL text; the vectors go straight
        to the backend and hydration returns no text for those records.

        ``embedder_selector`` names the configured instance that executes
        the embed (same vector space, enforced by the factory) — a batch
        can run on a GPU instance while serving stays on the default.
        """
        byov = vectors is not None
        chunk_count = 0
        purged = 0
        key = (tenant, collection)
        sel_space: str | None = None
        with self._collection_write_lock(tenant, collection):
            self._load_or_init(tenant, collection)
            embedder = self._collection_embedders[key]
        bound_embedder = embedder
        if embedder_selector is not None and not byov:
            sel_cfg = self._collection_config_for_load(tenant, collection)
            embedder = self._get_embedder_for_config(
                sel_cfg,
                selector=embedder_selector,
            )
            sel_space = embedder_factory_mod.vector_space_key(sel_cfg)

        rids_to_add, texts_to_encode, chunk_rows, safe_doc_meta = (
            self._prepare_index_records_locked(
                tenant,
                collection,
                docid,
                records,
                doc_meta,
            )
        )
        if byov:
            vectors = np.asarray(vectors, dtype=np.float32)
        elif not rids_to_add:
            vectors = None
        else:
            try:
                vectors = self._encode_embedder(embedder, texts_to_encode)
            except EmbedderUnavailableError:
                # Per-op preference: fall back to the collection's routed
                # binding when the preferred instance cannot serve.
                if embedder is bound_embedder:
                    raise
                self._drop_cached_embedder(embedder)
                embedder = bound_embedder
                sel_space = None
                vectors = self._encode_embedder(embedder, texts_to_encode)

        with self._collection_write_lock(tenant, collection):
            self._load_or_init(tenant, collection)
            col_db = self._dbs[key]
            backend = self._emb[key]
            current_embedder = self._collection_embedders[key]
            # Checked before the purge so a rejected re-ingest cannot drop
            # the already-indexed document version. The collection write
            # lock serializes all chunk writers, so the count cannot be
            # raced past a billing-hard limit.
            max_chunks = self._tenant_limit(tenant, "max_chunks_per_collection")
            if max_chunks > 0 and rids_to_add:
                total, replacing = col_db.chunk_counts(docid)
                if total - replacing + len(rids_to_add) > max_chunks:
                    raise ChunkLimitError(
                        f"collection '{tenant}/{collection}' would exceed "
                        f"its chunk limit ({max_chunks})"
                    )
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
                if embedder_selector is not None and sel_space is not None:
                    # Selector mode: the vectors stay valid while the
                    # collection's vector space is unchanged (the selector
                    # embedder was validated against it). A delete+recreate
                    # into another space re-resolves the selector — which
                    # raises if it no longer fits — and re-encodes.
                    now_cfg = self._collection_config_for_load(
                        tenant,
                        collection,
                    )
                    now_space = embedder_factory_mod.vector_space_key(now_cfg)
                    if now_space != sel_space:
                        embedder = self._get_embedder_for_config(
                            now_cfg,
                            selector=embedder_selector,
                        )
                        vectors = self._encode_embedder(
                            embedder,
                            texts_to_encode,
                        )
                elif current_embedder is not embedder:
                    vectors = self._encode_embedder(
                        current_embedder,
                        texts_to_encode,
                    )
            assert vectors is not None

            # Write metadata to SQLite (inside collection_lock)
            col_db.upsert_chunks(
                docid,
                chunk_rows,
                doc_meta=safe_doc_meta,
                texts=(
                    None
                    if byov
                    else dict(zip(rids_to_add, texts_to_encode, strict=False))
                ),
            )
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
            # Checked before the purge so a rejected batch cannot drop the
            # already-indexed document versions. The whole batch is one
            # projection: partially admitting it would leave callers with
            # a half-applied result they never asked for.
            max_chunks = self._tenant_limit(
                tenant, "max_chunks_per_collection"
            )
            if max_chunks > 0 and all_rids:
                _docs, total = col_db.get_doc_chunk_counts()
                replacing = sum(
                    col_db.chunk_counts(docid)[1]
                    for docid in {doc[0] for doc in docs_to_commit}
                )
                if total - replacing + len(all_rids) > max_chunks:
                    raise ChunkLimitError(
                        f"collection '{tenant}/{collection}' would exceed "
                        f"its chunk limit ({max_chunks})"
                    )

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

                results.append(
                    IndexResult(indexed_chunks=len(rids), purged_chunks=purged)
                )
                if not rids:
                    continue

                col_db.upsert_chunks(
                    docid,
                    chunk_rows,
                    doc_meta=safe_doc_meta,
                    texts=dict(zip(rids, texts, strict=False)),
                )

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
               *, content_filter: ContentFilterSpec | None = None,
               mode: SearchMode = "vector",
               query_vector: list[float] | None = None,
               embedder_selector: str | None = None) -> SearchOutput:
        """
        Queries the FAISS backend for top-k and keeps overfetch inside the
        store.

        Search holds the shared collection read lock while touching cached
        backend, metadata, and chunk text. Collection mutation/delete/restore
        take the write side, so same-collection searches can overlap without
        racing cache close or data-dir replacement.
        """
        if mode not in {"vector", "hybrid"}:
            raise ValueError(f"unsupported search mode: {mode!r}")
        if mode == "hybrid" and query_vector is not None:
            raise ValueError("hybrid search requires a text query")
        _fspec_str = "N/A" if filters is None else json.dumps(dict(filters))
        _content_str = (
            "N/A" if content_filter is None else json.dumps(content_filter)
        )
        log.debug(f"SEARCH-IN: tenant> {tenant} coll> {collection} query> {query} \
                  k> {k} mode> {mode} filters> {_fspec_str} content> {_content_str}")
        _perf = _time.perf_counter

        def _ms_since(t0: float) -> float:
            return round((_perf() - t0) * 1000, 2)

        kk = max(1, int(k))

        fetch_k = max(50, kk * 5)
        boost_weight = 0.0
        query_tokens: frozenset[str] = frozenset()
        if mode == "vector" and get_cfg().get("search.exact_token_boost"):
            # A null or non-numeric weight must not take every search down
            # with it -- read it behind the toggle and treat a bad value as
            # the documented "0 disables the boost".
            try:
                boost_weight = float(
                    get_cfg().get("search.exact_token_boost_weight")
                )
            except (TypeError, ValueError):
                boost_weight = 0.0
            if boost_weight > 0:
                query_tokens = _tokenize_exact(query)
        normed_filters: dict[str, list[Any]] = {}
        for key, vals in (filters or {}).items():
            safe_key = sanit_field(key)
            if not safe_key:
                continue
            if isinstance(vals, list):
                normed_filters[safe_key] = vals
            else:
                normed_filters[safe_key] = [vals]
        requires_expansion = bool(normed_filters or content_filter)

        def _has_metadata_prefilter() -> bool:
            """Whether SQLite can narrow at least one metadata key."""
            skip_prefixes = ("!", ">=", "<=", ">", "<")
            for values in normed_filters.values():
                has_exact = False
                mixed_semantics = False
                for value in values:
                    if not isinstance(value, str):
                        continue
                    if "*" in value or value.startswith(skip_prefixes):
                        mixed_semantics = True
                        break
                    has_exact = True
                if has_exact and not mixed_semantics:
                    return True
            return False

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
            boost_ms = 0.0
            vector_search_ms = 0.0
            lexical_search_ms = 0.0
            raw: list[tuple[str, float]] = []
            candidate_rids: list[str] = []
            meta_batch: dict[str, Metadata] = {}
            kept: list[tuple[str, float]] = []
            # rid -> similarity before the exact-token boost, for the
            # candidates it lifted. Empty unless P1-07 is enabled.
            exact_hits: dict[str, float] = {}
            exact_chunk_tokens: dict[str, frozenset[str]] = {}
            hybrid_ranks: dict[str, tuple[int | None, int | None]] = {}

            filtered_search = getattr(backend, "search_filtered", None)
            # Exact-token boost is defined over the vector-first fetch window.
            # Keep that path unchanged so enabling a backend capability cannot
            # change its reranking population.
            can_prefilter = (
                callable(filtered_search)
                and not query_tokens
                and (bool(content_filter) or _has_metadata_prefilter())
            )

            if mode == "hybrid":
                t0 = _perf()
                vector = backend.search(q_vec, limit)
                vector_search_ms = _ms_since(t0)

                t0 = _perf()
                lexical = col_db.lexical_candidates(query, fetch_k)
                lexical_search_ms = _ms_since(t0)
                search_ms = vector_search_ms + lexical_search_ms

                fused = _reciprocal_rank_fusion(vector, lexical)
                hybrid_ranks = {
                    rid: (vector_rank, lexical_rank)
                    for rid, _score, vector_rank, lexical_rank in fused
                }
                candidate_rids = [
                    rid
                    for rid, _score, _vector_rank, _lexical_rank in fused
                ]

                if content_filter:
                    t0 = _perf()
                    content_rids = col_db.filter_by_content(
                        candidate_rids,
                        content_filter,
                    )
                    filter_push_ms += _ms_since(t0)
                    candidate_rids = [
                        rid for rid in candidate_rids if rid in content_rids
                    ]

                if normed_filters:
                    surviving, meta_batch, meta_timing = (
                        col_db.filter_meta_batch(
                            candidate_rids,
                            normed_filters,
                        )
                    )
                    candidate_rids = [
                        rid for rid in candidate_rids if rid in surviving
                    ]
                    filter_push_ms += meta_timing["filter_ms"]
                    hydrate_meta_ms += meta_timing["hydrate_ms"]
                    log.debug(f"SEARCH-FILTER-POST: {normed_filters}")
                else:
                    t0 = _perf()
                    meta_batch = col_db.get_meta_batch(candidate_rids)
                    hydrate_meta_ms += _ms_since(t0)

                candidate_set = set(candidate_rids)
                t0 = _perf()
                kept = [
                    (rid, score)
                    for rid, score, _vector_rank, _lexical_rank in fused
                    if rid in candidate_set
                    and matches_filters(
                        meta_batch.get(rid, {}),
                        normed_filters,
                    )
                ][:kk]
                filter_post_ms += _ms_since(t0)
            elif can_prefilter:
                # Select a SQL-safe superset first. Metadata is hydrated from
                # the same read transaction as its SQL filter, then canonical
                # Python semantics remove any unsupported-filter survivors.
                t0 = _perf()
                selected_rids = list(col_db.get_chunk_rids())
                if content_filter:
                    content_rids = col_db.filter_by_content(
                        selected_rids,
                        content_filter,
                    )
                    selected_rids = [
                        rid for rid in selected_rids if rid in content_rids
                    ]
                filter_push_ms += _ms_since(t0)

                if normed_filters:
                    surviving, meta_batch, meta_timing = (
                        col_db.filter_meta_batch(
                            selected_rids,
                            normed_filters,
                        )
                    )
                    selected_rids = [
                        rid for rid in selected_rids if rid in surviving
                    ]
                    filter_push_ms += meta_timing["filter_ms"]
                    hydrate_meta_ms += meta_timing["hydrate_ms"]
                    log.debug(f"SEARCH-FILTER-POST: {normed_filters}")
                else:
                    t0 = _perf()
                    meta_batch = col_db.get_meta_batch(selected_rids)
                    hydrate_meta_ms += _ms_since(t0)

                t0 = _perf()
                allowed_rids = {
                    rid
                    for rid in selected_rids
                    if matches_filters(
                        meta_batch.get(rid, {}),
                        normed_filters,
                    )
                }
                filter_post_ms += _ms_since(t0)

                # Keep the normal FAISS path when every indexed record is
                # eligible. Otherwise the optional capability scores only the
                # selected IDs. Canonical filtering is already complete, so
                # the backend only needs to return k records.
                use_filtered = (
                    total is None or len(allowed_rids) != total
                )
                vector_limit = min(len(allowed_rids), kk)
                if vector_limit > 0:
                    t0 = _perf()
                    if use_filtered:
                        raw = filtered_search(
                            q_vec,
                            vector_limit,
                            allowed_rids,
                        )
                    else:
                        raw = backend.search(q_vec, vector_limit)
                    search_ms += _ms_since(t0)

                kept = raw[:kk]
                log.debug(
                    "SEARCH-FILTER-PREFILTER: eligible=%s indexed=%s "
                    "backend_filtered=%s",
                    len(allowed_rids),
                    total,
                    use_filtered,
                )
            else:
                while True:
                    t0 = _perf()
                    raw = backend.search(q_vec, limit)
                    search_ms += _ms_since(t0)
                    raw_count = len(raw)

                    if query_tokens:
                        t0 = _perf()
                        raw, exact_hits = self._apply_exact_token_boost(
                            tenant, collection, raw, query_tokens, boost_weight,
                            token_cache=exact_chunk_tokens,
                        )
                        boost_ms += _ms_since(t0)

                    candidate_rids = [rid for rid, _score in raw if rid]

                    if content_filter:
                        t0 = _perf()
                        content_rids = col_db.filter_by_content(
                            candidate_rids,
                            content_filter,
                        )
                        filter_push_ms += _ms_since(t0)
                        raw = [
                            (rid, score)
                            for rid, score in raw
                            if rid in content_rids
                        ]
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

                    if not requires_expansion or len(kept) >= kk:
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
                        # The boost is named on its own below; folding it
                        # into the percentage would report a lexical nudge
                        # as similarity, and can read past 100%.
                        query, exact_hits.get(rid, score), filters, rid_meta,
                        content_filter=content_filter,
                        exact_token_hit=rid in exact_hits,
                        vector_rank=hybrid_ranks.get(rid, (None, None))[0],
                        lexical_rank=hybrid_ranks.get(rid, (None, None))[1],
                    ),
                ))
            hydrate_text_ms = _ms_since(t0)
            _hits = [(r.id, round(r.score, 3)) for r in out[:3]]
            _sfx = " ..." if len(out) > 3 else ""
            log.debug(f"SEARCH-OUT: {len(out)} hits {_hits}{_sfx}")
            timing = {
                "embed_ms": embed_ms,
                "search_ms": round(search_ms, 2),
                "filter_ms": round(filter_push_ms + filter_post_ms, 2),
                # Exact-token boost reads chunk text inside the fetch loop,
                # same SQLite work as hydration -- folded into hydrate_ms so
                # vector mode keeps its established four-field contract.
                "hydrate_ms": round(
                    hydrate_meta_ms + hydrate_text_ms + boost_ms, 2
                ),
            }
            if mode == "hybrid":
                timing.update(
                    {
                        "vector_search_ms": round(vector_search_ms, 2),
                        "lexical_search_ms": round(lexical_search_ms, 2),
                    }
                )
            return SearchOutput(matches=out, timing=timing)

        key = (tenant, collection)
        collection_path = self._base_path(tenant, collection)
        with self._collection_read_lock(tenant, collection):
            existed_at_start = os.path.isdir(collection_path)

        def _try_search() -> SearchOutput | None:
            """One search attempt against the cached collection state.

            Returns None if the caches aren't loaded (caller reloads and
            retries). The embed runs *outside* the collection lock: it only
            needs the query and the stable per-collection embedder, touching
            neither the FAISS backend nor col_db. Keeping it out
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
                # Per-operation selector: encode with the caller-named
                # instance (same vector space, enforced by the factory).
                # The query cache keys by embedder object, so cached
                # vectors never cross executors. The swap guard below
                # still compares the COLLECTION binding — a re-created
                # collection retries, re-resolving the selector.
                encode_embedder = embedder
                if embedder_selector is not None:
                    sel_cfg = self._collection_config_for_load(
                        tenant,
                        collection,
                    )
                    encode_embedder = self._get_embedder_for_config(
                        sel_cfg,
                        selector=embedder_selector,
                    )
                t0 = _perf()
                try:
                    q_vec, query_cache_hit = self._encode_query_cached(
                        tenant,
                        collection,
                        encode_embedder,
                        query,
                    )
                except EmbedderUnavailableError:
                    # The priority preference is exactly that: when its
                    # instance cannot serve, the collection's own routed
                    # binding answers instead of failing the search.
                    if encode_embedder is embedder:
                        raise
                    self._drop_cached_embedder(encode_embedder)
                    encode_embedder = embedder
                    q_vec, query_cache_hit = self._encode_query_cached(
                        tenant,
                        collection,
                        encode_embedder,
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
                # A search must not bring a collection into existence (B33):
                # that is how invented names got past the per-tenant quota.
                # It answers empty rather than not-found, which is what it
                # always answered -- only the side effect was the bug. The
                # ValueError below stays for the different case it was
                # written for: a collection that WAS there and was deleted
                # or moved mid-search, which must not read as "no matches".
                if not os.path.isdir(collection_path):
                    if existed_at_start:
                        raise ValueError(
                            f"collection '{collection}' does not exist"
                        )
                    timing = {
                        "embed_ms": 0.0,
                        "search_ms": 0.0,
                        "filter_ms": 0.0,
                        "hydrate_ms": 0.0,
                    }
                    if mode == "hybrid":
                        timing.update(
                            {
                                "vector_search_ms": 0.0,
                                "lexical_search_ms": 0.0,
                            }
                        )
                    return SearchOutput(
                        matches=[],
                        timing=timing,
                    )
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

    def _apply_exact_token_boost(
        self,
        tenant: str,
        collection: str,
        raw: list[tuple[str, float]],
        query_tokens: frozenset[str],
        weight: float,
        token_cache: dict[str, frozenset[str]] | None = None,
    ) -> tuple[list[tuple[str, float]], dict[str, float]]:
        """Re-rank candidates already inside the vector window (P1-07).

        Boosts a candidate's score by `weight * matched_fraction`, where
        `matched_fraction` is the share of query tokens found verbatim
        (casefolded) in the chunk text. This narrows and re-sorts within the
        existing fetch window; it never adds candidates FAISS did not already
        return.

        Returns the re-sorted window plus, for every boosted candidate, the
        score it carried before the boost. `token_cache` holds chunk tokens
        across the filter-expansion rounds of a single search, so a
        candidate's text is read once however far the window grows.
        """
        cache = {} if token_cache is None else token_cache
        boosted: list[tuple[str, float]] = []
        pre_boost: dict[str, float] = {}
        for rid, score in raw:
            if not rid:
                boosted.append((rid, score))
                continue
            chunk_tokens = cache.get(rid)
            if chunk_tokens is None:
                chunk_tokens = _tokenize_exact(
                    self._load_chunk_text(tenant, collection, rid)
                )
                cache[rid] = chunk_tokens
            matched = query_tokens & chunk_tokens
            if matched:
                pre_boost[rid] = score
                boosted.append(
                    (rid, score + weight * len(matched) / len(query_tokens))
                )
            else:
                boosted.append((rid, score))
        boosted.sort(key=lambda item: item[1], reverse=True)
        return boosted, pre_boost

    def _build_match_reason(self, query: str, score: float,
                            filters: FilterSpec | None,
                            meta: Metadata | None,
                            *,
                            content_filter: ContentFilterSpec | None = None,
                            exact_token_hit: bool = False,
                            vector_rank: int | None = None,
                            lexical_rank: int | None = None) -> str:
        """Build a human-readable explanation of why a result matched.

        `score` is the similarity the index reported: callers pass the
        pre-boost value so the reported percentage stays a similarity.
        """
        parts = []

        # Similarity component
        if vector_rank is not None or lexical_rank is not None:
            if vector_rank is not None:
                parts.append(f"vector rank {vector_rank}")
            if lexical_rank is not None:
                parts.append(f"lexical rank {lexical_rank}")
        elif query:
            pct = int(score * 100)
            parts.append(f"semantic similarity {pct}%")
        if exact_token_hit:
            parts.append("exact token match")

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
        if content_filter:
            parts.append(f"content {content_filter['op']} match")

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
        with self._reindex_threads_lock:
            if self._closed:
                return
            self._closed = True
            reindex_workers = list(self._reindex_threads.items())
        for job_id, worker in reindex_workers:
            if not worker.is_alive():
                continue
            with suppress(FileNotFoundError, ReindexJobStateError):
                self.pause_reindex(job_id)
        for _job_id, worker in reindex_workers:
            if worker is not current_thread():
                worker.join()
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
                    if rel_name == Path(DATA_DIR_LOCK_NAME):
                        continue
                    try:
                        zf.write(file_path, rel_name.as_posix())
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        if exc.errno == errno.ENOENT:
                            continue
                        raise

    def _validate_zip_members(self, zf: zipfile.ZipFile) -> None:
        seen: set[str] = set()
        for member in zf.infolist():
            name = member.filename
            if not name:
                continue
            if name in seen:
                raise ValueError(f"duplicate archive member: {name}")
            seen.add(name)
            rel_path = Path(name)
            if rel_path.is_absolute() or ".." in rel_path.parts:
                raise ValueError(f"invalid archive member: {name}")
            if name.startswith(("/", "\\")):
                raise ValueError(f"invalid archive member: {name}")
            if rel_path.name.endswith(("-wal", "-shm")):
                raise ValueError(f"archive contains SQLite sidecar: {name}")
            if rel_path.parts and rel_path.parts[0] == DATA_DIR_LOCK_NAME:
                raise ValueError(
                    f"archive contains data-directory ownership lock: {name}"
                )

    @staticmethod
    def _collection_archive_db_info(
        collection_dir: Path,
        tenant: str,
        collection: str,
    ) -> tuple[int, set[str], list[tuple[str, str]]]:
        metadata = CollectionDB()
        try:
            metadata.open(
                collection_dir / "meta.db",
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
                        "ingested_at", "text",
                    },
                    "chunk_text_fts": {"text"},
                    "chunk_text_trigram": {"text"},
                    "chunk_meta": {"rid", "key", "value"},
                    "document_meta": {"docid", "key", "value"},
                    "query_log": {
                        "query_id", "tenant", "collection", "actor",
                        "query_text", "k", "filters_json", "include_common",
                        "content_filter_json", "mode",
                        "common_tenant", "common_collection", "result_ids_json",
                        "result_count", "latency_ms", "timing_json",
                        "request_id", "replay_of", "executed_at",
                    },
                }
                for table, columns in required_columns.items():
                    found = {
                        row[1]
                        for row in conn.execute(f"PRAGMA table_info({table})")
                    }
                    if not columns <= found:
                        raise ValueError("incomplete archive metadata schema")
                schema_version = CollectionDB._schema_version(conn)
                rids = {
                    str(row[0]) for row in conn.execute("SELECT rid FROM chunks")
                }
                query_homes = [
                    (str(row[0]), str(row[1]))
                    for row in conn.execute(
                        "SELECT query_id, executed_at FROM query_log"
                    )
                ]
        except Exception:
            raise ValueError(
                "invalid archive collection metadata database: "
                f"{tenant}/{collection}"
            ) from None
        finally:
            metadata.close()
        return schema_version, rids, query_homes

    def _preflight_collection_archive(
        self,
        collection_dir: Path,
    ) -> tuple[dict[str, Any], CollectionConfig, list[tuple[str, str]]]:
        allowed_roots = {
            _COLLECTION_ARCHIVE_MANIFEST,
            COLLECTION_CONFIG_FILENAME,
            "meta.db",
            "index",
            "chunks",
        }
        unexpected = sorted(
            entry.name
            for entry in collection_dir.iterdir()
            if entry.name not in allowed_roots
        )
        if unexpected:
            raise ValueError(
                "unexpected collection archive member: "
                f"{unexpected[0]}"
            )
        manifest_path = collection_dir / _COLLECTION_ARCHIVE_MANIFEST
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError(
                "collection archive is missing a valid manifest.json"
            ) from None
        if not isinstance(manifest, dict):
            raise ValueError("collection archive manifest must be an object")
        if manifest.get("archive_format") != _COLLECTION_ARCHIVE_FORMAT:
            raise ValueError("unsupported collection archive format")
        if manifest.get("archive_version") != _COLLECTION_ARCHIVE_VERSION:
            raise ValueError("unsupported collection archive format version")
        archive_pavedb_version = manifest.get("pavedb_version")
        if archive_pavedb_version != VERSION:
            raise ValueError(
                "collection archive PaveDB version mismatch: "
                f"expected {VERSION}, found {archive_pavedb_version!r}"
            )

        source = manifest.get("source")
        if not isinstance(source, dict):
            raise ValueError("collection archive source is invalid")
        source_tenant = source.get("tenant")
        source_collection = source.get("collection")
        if not isinstance(source_tenant, str) or not source_tenant:
            raise ValueError("collection archive source tenant is invalid")
        if not isinstance(source_collection, str) or not source_collection:
            raise ValueError("collection archive source collection is invalid")

        backend_spec = manifest.get("backend_spec")
        embedder_spec = manifest.get("embedder_spec")
        if not isinstance(backend_spec, dict) or not isinstance(embedder_spec, dict):
            raise ValueError("collection archive runtime specification is invalid")
        config = self._validate_archive_collection_config(
            {
                "backend_type": backend_spec.get("backend_type"),
                "backend_config": backend_spec.get("backend_config"),
                "embedder_type": embedder_spec.get("embedder_type"),
                "embed_model": embedder_spec.get("embed_model"),
                "embedder_config": embedder_spec.get("embedder_config"),
            },
            source_tenant,
            source_collection,
        )
        if config["backend_type"] != "faiss":
            raise ValueError(
                "unsupported collection archive backend: "
                f"{config['backend_type']}"
            )
        sidecar = self._archive_sidecar_config(
            collection_dir,
            source_tenant,
            source_collection,
        )
        if sidecar is None:
            raise ValueError("collection archive is missing collection.json")
        if sidecar != config:
            raise ValueError(
                "collection archive manifest and sidecar conflict: "
                f"{source_tenant}/{source_collection}"
            )

        metadata = CollectionDB()
        try:
            metadata.open(collection_dir / "meta.db")
            metadata.verify_text_indexes()
            metadata.checkpoint_wal()
        except Exception:
            raise ValueError(
                "invalid archive collection text index: "
                f"{source_tenant}/{source_collection}"
            ) from None
        finally:
            metadata.close()

        schema_version, rids, query_homes = self._collection_archive_db_info(
            collection_dir,
            source_tenant,
            source_collection,
        )
        manifest_schema = manifest.get("schema_version")
        if not isinstance(manifest_schema, int) or isinstance(manifest_schema, bool):
            raise ValueError("collection archive schema version is invalid")
        if manifest_schema != schema_version:
            raise ValueError(
                "collection archive schema version mismatch: "
                f"manifest {manifest_schema}, metadata {schema_version}"
            )
        self._validate_archive_index(
            collection_dir,
            source_tenant,
            source_collection,
            rids,
            expected_dim=(
                int(config["embedder_config"]["dim"])
                if "dim" in config["embedder_config"]
                else None
            ),
        )
        try:
            embedder_factory_mod.runtime_embedder_spec_for_vector(
                get_cfg(),
                config,
            )
        except Exception:
            vector_key = embedder_factory_mod.vector_space_key(config)
            raise ValueError(
                "no compatible destination embedder for vector space "
                f"{vector_key}"
            ) from None

        collection_meta = manifest.get("collection")
        if not isinstance(collection_meta, dict):
            raise ValueError("collection archive metadata is invalid")
        if not isinstance(collection_meta.get("meta", {}), dict):
            raise ValueError("collection archive catalog metadata is invalid")
        if collection_meta.get("display_name") is not None and not isinstance(
            collection_meta.get("display_name"),
            str,
        ):
            raise ValueError("collection archive display name is invalid")
        if collection_meta.get("created_at") is not None and not isinstance(
            collection_meta.get("created_at"),
            str,
        ):
            raise ValueError("collection archive creation time is invalid")
        return manifest, config, query_homes

    def _close_collection_for_restore(
        self,
        tenant: str,
        collection: str,
    ) -> None:
        key = (tenant, collection)
        backend = self._emb.pop(key, None)
        if backend is not None:
            backend.flush()
            backend.close()
            self._clear_index_dirty(tenant, collection)
        col_db = self._dbs.pop(key, None)
        if col_db is not None:
            col_db.close()
        self._drop_collection_embedder(key)
        self._drop_query_vector_cache(tenant, collection)

    @staticmethod
    def _assert_no_collection_restore_backup(target: Path) -> None:
        if not target.parent.is_dir():
            return
        prefix = f"{_COLLECTION_RESTORE_BACKUP_PREFIX}{target.name}-"
        marker = next(
            (
                entry.name
                for entry in target.parent.iterdir()
                if entry.name.startswith(prefix)
            ),
            None,
        )
        if marker is not None:
            raise RuntimeError(
                "interrupted collection archive restore detected; recover or "
                f"remove {marker} before starting"
            )

    def _install_collection_archive(
        self,
        target: Path,
        staged: Path,
        publish: Callable[[], None],
    ) -> None:
        backup_root: Path | None = None
        backup_entry: Path | None = None
        installed = False
        moved_old = False
        try:
            if target.exists() or target.is_symlink():
                backup_root = Path(
                    tempfile.mkdtemp(
                        prefix=(
                            f"{_COLLECTION_RESTORE_BACKUP_PREFIX}{target.name}-"
                        ),
                        dir=target.parent,
                    )
                )
                backup_entry = backup_root / "collection"
                os.rename(target, backup_entry)
                moved_old = True
            os.rename(staged, target)
            installed = True
            publish()
        except Exception as exc:
            rollback_errors: list[Exception] = []
            if installed:
                try:
                    self._remove_path(target)
                except Exception as rollback_exc:
                    rollback_errors.append(rollback_exc)
            if moved_old and backup_entry is not None:
                try:
                    os.rename(backup_entry, target)
                except Exception as rollback_exc:
                    rollback_errors.append(rollback_exc)
            if rollback_errors:
                raise RuntimeError(
                    "collection archive restore failed and rollback is incomplete"
                ) from exc
            if backup_root is not None:
                with suppress(OSError):
                    self._remove_path(backup_root)
            raise
        finally:
            with suppress(OSError):
                self._remove_path(staged.parent)
        if backup_root is not None:
            try:
                self._remove_path(backup_root)
            except OSError:
                log.warning(
                    "Failed to remove collection archive backup %s",
                    backup_root,
                    exc_info=True,
                )

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
            entry
            for entry in data_dir.iterdir()
            if entry != backup_dir and entry.name != DATA_DIR_LOCK_NAME
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
            self._ensure_catalog().checkpoint_wal()
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

                self._preflight_archive(
                    extract_dir,
                    validate_destination=True,
                    scratch_dir=True,
                )
                self._assert_no_restore_backup(data_dir_path)
                data_dir_path.mkdir(parents=True, exist_ok=True)
                self._flush_caches()
                self._replace_archive_tree(data_dir_path, extract_dir)
                self._ensure_catalog().rotate_metrics_generation()
                self._recover_reindex_jobs()

    def dump_collection_archive(
        self,
        tenant: str,
        collection: str,
        output_path: str | os.PathLike[str] | None = None,
    ) -> tuple[str, str | None]:
        """Export one collection with a versioned, credential-free manifest."""
        with self._collection_write_lock(tenant, collection):
            collection_dir = Path(self._base_path(tenant, collection)).resolve()
            data_dir = Path(self._data_dir).resolve()
            if not collection_dir.is_relative_to(data_dir):
                raise ValueError("invalid collection archive target")
            self._assert_no_collection_restore_backup(collection_dir)
            catalog_config = self._with_catalog_retry(
                "dump_collection_archive",
                lambda catalog: catalog.get_collection_config(tenant, collection),
            )
            if catalog_config is None or not collection_dir.is_dir():
                raise FileNotFoundError(
                    f"collection '{tenant}/{collection}' does not exist"
                )
            config = self._validate_archive_collection_config(
                catalog_config,
                tenant,
                collection,
            )
            sidecar = self._archive_sidecar_config(
                collection_dir,
                tenant,
                collection,
            )
            if sidecar is None:
                self._write_collection_config(tenant, collection, config)
            elif sidecar != config:
                raise ValueError(
                    "catalog and collection sidecar conflict: "
                    f"{tenant}/{collection}"
                )

            self._save(tenant, collection)
            key = (tenant, collection)
            metadata = self._dbs.get(key)
            temporary_metadata = metadata is None
            if metadata is None:
                if not self._db_path(tenant, collection).is_file():
                    raise ValueError(
                        "collection metadata database is missing: "
                        f"{tenant}/{collection}"
                    )
                metadata = CollectionDB()
                metadata.open(self._db_path(tenant, collection))
            try:
                self._migrate_chunk_text_sidecars_in(
                    collection_dir,
                    metadata,
                )
                metadata.verify_text_indexes()
                metadata.checkpoint_wal()
            finally:
                if temporary_metadata:
                    metadata.close()

            schema_version, rids, _query_homes = self._collection_archive_db_info(
                collection_dir,
                tenant,
                collection,
            )
            self._validate_archive_index(
                collection_dir,
                tenant,
                collection,
                rids,
                expected_dim=(
                    int(config["embedder_config"]["dim"])
                    if "dim" in config["embedder_config"]
                    else None
                ),
            )
            manifest = {
                "archive_format": _COLLECTION_ARCHIVE_FORMAT,
                "archive_version": _COLLECTION_ARCHIVE_VERSION,
                "pavedb_version": VERSION,
                "schema_version": schema_version,
                "source": {
                    "tenant": tenant,
                    "collection": collection,
                },
                "collection": {
                    "display_name": catalog_config.get("display_name"),
                    "meta": catalog_config.get("meta") or {},
                    "created_at": catalog_config.get("created_at"),
                },
                "backend_spec": {
                    "backend_type": config["backend_type"],
                    "backend_config": config["backend_config"],
                },
                "embedder_spec": {
                    "embedder_type": config["embedder_type"],
                    "embed_model": config["embed_model"],
                    "embedder_config": config["embedder_config"],
                },
            }

            export_root = Path(
                tempfile.mkdtemp(prefix="pavedb_collection_export_")
            )
            staged = export_root / "collection"
            try:
                shutil.copytree(
                    collection_dir,
                    staged,
                    ignore=shutil.ignore_patterns(
                        "*-wal",
                        "*-shm",
                        _COLLECTION_ARCHIVE_MANIFEST,
                    ),
                )
                (staged / _COLLECTION_ARCHIVE_MANIFEST).write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
                    + "\n",
                    encoding="utf-8",
                )
                if output_path is not None:
                    archive_path = Path(output_path).resolve()
                    self._write_zip(staged, archive_path)
                    self._remove_path(export_root)
                    return str(archive_path), None

                timestamp = datetime.now(tz.utc).strftime("%Y%m%dT%H%M%SZ")
                archive_path = export_root / (
                    f"pavedb-collection-{timestamp}.zip"
                )
                self._write_zip(staged, archive_path)
                return str(archive_path), str(export_root)
            except Exception:
                with suppress(OSError):
                    self._remove_path(export_root)
                raise

    def restore_collection_archive(
        self,
        tenant: str,
        collection: str,
        archive_bytes: bytes,
    ) -> None:
        """Replace one collection after complete same-version preflight."""
        with tempfile.TemporaryDirectory(
            prefix="pavedb_collection_import_"
        ) as tmp_dir:
            extract_dir = Path(tmp_dir) / "extracted"
            extract_dir.mkdir()
            try:
                with zipfile.ZipFile(
                    io.BytesIO(archive_bytes),
                    "r",
                ) as archive:
                    self._validate_zip_members(archive)
                    archive.extractall(extract_dir)
            except zipfile.BadZipFile as exc:
                raise ValueError("invalid collection archive") from exc

            manifest, config, query_homes = self._preflight_collection_archive(
                extract_dir
            )
            collection_meta = manifest["collection"]

            with self._collection_write_lock(tenant, collection):
                data_dir = Path(self._data_dir).resolve()
                target = Path(self._base_path(tenant, collection)).resolve()
                if not target.is_relative_to(data_dir):
                    raise ValueError("invalid collection archive target")
                target.parent.mkdir(parents=True, exist_ok=True)
                self._assert_no_collection_restore_backup(target)
                install_root = Path(
                    tempfile.mkdtemp(
                        prefix=".pavedb-collection-install-",
                        dir=target.parent,
                    )
                )
                staged = install_root / "collection"
                try:
                    shutil.copytree(extract_dir, staged)
                    (staged / _COLLECTION_ARCHIVE_MANIFEST).unlink()
                    with sqlite3.connect(staged / "meta.db") as metadata:
                        metadata.execute("PRAGMA journal_mode=DELETE")
                        metadata.execute(
                            "UPDATE query_log SET tenant=?, collection=?",
                            (tenant, collection),
                        )
                except Exception:
                    with suppress(OSError):
                        self._remove_path(install_root)
                    raise

                try:
                    self._close_collection_for_restore(tenant, collection)
                except Exception:
                    with suppress(OSError):
                        self._remove_path(install_root)
                    raise

                def publish() -> None:
                    self._with_catalog_retry(
                        "restore_collection_archive",
                        lambda catalog: catalog.restore_collection(
                            tenant,
                            collection,
                            display_name=collection_meta.get("display_name"),
                            meta=collection_meta.get("meta") or {},
                            backend_type=config["backend_type"],
                            backend_config=config["backend_config"],
                            embedder_type=config["embedder_type"],
                            embed_model=config["embed_model"],
                            embed_config=config["embedder_config"],
                            created_at=collection_meta.get("created_at"),
                            # Preflight already proved both tags match
                            # this instance, so the restored row carries
                            # them instead of reading as untagged (P2-25).
                            pavedb_version=manifest["pavedb_version"],
                            collection_schema_version=manifest[
                                "schema_version"
                            ],
                            query_homes=query_homes,
                        ),
                    )

                self._install_collection_archive(
                    target,
                    staged,
                    publish,
                )
                # The archive replaces the corpus this collection's
                # reindex jobs were built against, so their staged vectors
                # and progress no longer describe anything on disk.
                replaced = self._with_catalog_retry(
                    "purge_reindex_jobs_for_collection",
                    lambda catalog: catalog.purge_reindex_jobs_for_collection(
                        tenant,
                        collection,
                    ),
                )
                for job_id in replaced:
                    with suppress(Exception):
                        self._cleanup_reindex_job_dir(job_id)
                with self._catalog_metrics_lock:
                    self._catalog_metrics_cache = None
                with self._chunk_drift_metrics_lock:
                    self._chunk_drift_metrics_cache = None
                with self._query_log_gauge_metrics_lock:
                    self._query_log_gauge_metrics_cache = None
