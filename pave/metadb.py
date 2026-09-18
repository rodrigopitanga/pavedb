# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""SQLite metadata stores.

Two stores live here:

- `CollectionDB`: per-collection SQLite that holds documents,
  canonical chunk text, the `chunk_meta` k/v filter index, FTS5 text
  indexes, and the query log. Uses a read/write connection split (both
  `check_same_thread=False`) serialized by separate locks.
- `CatalogDB`: global SQLite catalog mapping tenants/collections to
  their configured embedder spec and display label, plus persistent
  operational state such as request-rate buckets and reindex jobs. Source of
  truth for collection listings; bootstraps itself from the data directory
  layout on first open.

Both stores enable WAL mode and run their own schema migrations.
The store layer (`pave.stores.local`) opens them; nothing else
touches their connection state.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone as tz
from pathlib import Path
from typing import Any, Iterator

from pave.filters import sanit_sql


class LegacyMetadataError(RuntimeError):
    pass


class UnsupportedSchemaVersionError(RuntimeError):
    pass


class SchemaMigrationError(RuntimeError):
    pass


class CollectionConfigRecoveryError(RuntimeError):
    pass


class CollectionLimitError(RuntimeError):
    pass


class ChunkLimitError(RuntimeError):
    pass


class ReindexJobConflictError(RuntimeError):
    pass


class ReindexJobStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int | None = None
    remaining: int | None = None
    retry_after: int | None = None
    window_seconds: int | None = None


_COLLECTION_BASELINE_SCHEMA_VERSION = 6
_CATALOG_BASELINE_SCHEMA_VERSION = 5
_COLLECTION_SCHEMA_VERSION = 8
_CATALOG_SCHEMA_VERSION = 8
# Public alias: the collection schema version a freshly created collection is
# tagged with (P2-25); callers outside this module use this, not the
# underscore-prefixed constant.
COLLECTION_SCHEMA_VERSION = _COLLECTION_SCHEMA_VERSION
_COLLECTION_MIGRATIONS: dict[int, list[str]] = {
    7: [
        "ALTER TABLE chunks ADD COLUMN text TEXT",
        "ALTER TABLE query_log ADD COLUMN content_filter_json TEXT",
        """
        CREATE VIRTUAL TABLE chunk_text_fts USING fts5(
            text,
            content='chunks',
            content_rowid='rowid',
            tokenize='unicode61'
        )
        """,
        """
        CREATE VIRTUAL TABLE chunk_text_trigram USING fts5(
            text,
            content='chunks',
            content_rowid='rowid',
            tokenize='trigram'
        )
        """,
        # Both indexes are external-content: their integrity check requires
        # every chunks row to be indexed, so text-less (BYOV / identity)
        # chunks index as '' -- no tokens, still unsearchable. Updates use
        # one trigger, delete then insert: SQLite does not order sibling
        # triggers, and an insert landing before its delete drops the row.
        """
        CREATE TRIGGER chunks_text_ai AFTER INSERT ON chunks
        BEGIN
            INSERT INTO chunk_text_fts(rowid, text)
                VALUES (new.rowid, COALESCE(new.text, ''));
            INSERT INTO chunk_text_trigram(rowid, text)
                VALUES (new.rowid, COALESCE(new.text, ''));
        END
        """,
        """
        CREATE TRIGGER chunks_text_ad AFTER DELETE ON chunks
        BEGIN
            INSERT INTO chunk_text_fts(chunk_text_fts, rowid, text)
                VALUES ('delete', old.rowid, COALESCE(old.text, ''));
            INSERT INTO chunk_text_trigram(chunk_text_trigram, rowid, text)
                VALUES ('delete', old.rowid, COALESCE(old.text, ''));
        END
        """,
        """
        CREATE TRIGGER chunks_text_au AFTER UPDATE OF text ON chunks
        BEGIN
            INSERT INTO chunk_text_fts(chunk_text_fts, rowid, text)
                VALUES ('delete', old.rowid, COALESCE(old.text, ''));
            INSERT INTO chunk_text_trigram(chunk_text_trigram, rowid, text)
                VALUES ('delete', old.rowid, COALESCE(old.text, ''));
            INSERT INTO chunk_text_fts(rowid, text)
                VALUES (new.rowid, COALESCE(new.text, ''));
            INSERT INTO chunk_text_trigram(rowid, text)
                VALUES (new.rowid, COALESCE(new.text, ''));
        END
        """,
        # Rows that predate the text column (upgraded v6 collections) are
        # indexed here, so the migration leaves both indexes consistent.
        "INSERT INTO chunk_text_fts(chunk_text_fts) VALUES ('rebuild')",
        "INSERT INTO chunk_text_trigram(chunk_text_trigram) VALUES ('rebuild')",
    ],
    8: [
        "ALTER TABLE query_log ADD COLUMN mode TEXT NOT NULL DEFAULT 'vector' "
        "CHECK (mode IN ('vector', 'hybrid'))",
    ],
}
_CATALOG_MIGRATIONS: dict[int, list[str]] = {
    6: [
        """
        CREATE TABLE IF NOT EXISTS rate_limit_buckets (
            tenant       TEXT NOT NULL,
            operation    TEXT NOT NULL,
            window_start TEXT NOT NULL,
            count        INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (tenant, operation, window_start)
        )
        """,
    ],
    7: [
        """
        CREATE TABLE IF NOT EXISTS reindex_jobs (
            job_id             TEXT PRIMARY KEY,
            tenant             TEXT NOT NULL,
            collection         TEXT NOT NULL,
            status             TEXT NOT NULL,
            source_config_json TEXT NOT NULL,
            target_config_json TEXT NOT NULL,
            source_digest      TEXT,
            total_chunks       INTEGER NOT NULL DEFAULT 0,
            completed_chunks   INTEGER NOT NULL DEFAULT 0,
            error              TEXT,
            created_at         TEXT NOT NULL DEFAULT (
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            ),
            updated_at         TEXT NOT NULL DEFAULT (
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            ),
            started_at         TEXT,
            finished_at        TEXT,
            CHECK (status IN (
                'queued', 'running', 'pausing', 'paused', 'committing',
                'cancelling', 'cancelled', 'failed', 'succeeded'
            )),
            CHECK (total_chunks >= 0),
            CHECK (completed_chunks >= 0),
            CHECK (completed_chunks <= total_chunks)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_reindex_jobs_collection
            ON reindex_jobs (tenant, collection, created_at DESC)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_reindex_jobs_active
            ON reindex_jobs (tenant, collection)
            WHERE status IN (
                'queued', 'running', 'pausing', 'paused', 'committing',
                'cancelling', 'failed'
            )
        """,
    ],
    8: [
        "ALTER TABLE collections ADD COLUMN pavedb_version TEXT",
        "ALTER TABLE collections ADD COLUMN collection_schema_version INTEGER",
    ],
}
COLLECTION_CONFIG_FILENAME = "collection.json"
COLLECTION_CONFIG_FIELDS = (
    "backend_type",
    "backend_config",
    "embedder_type",
    "embed_model",
    "embedder_config",
)


def validate_collection_config(config: Any) -> dict[str, Any]:
    """Return the complete persisted collection configuration."""
    if not isinstance(config, dict) or any(
        key not in config for key in COLLECTION_CONFIG_FIELDS
    ):
        raise ValueError("collection config is incomplete")
    if not (
        isinstance(config["backend_type"], str)
        and config["backend_type"]
        and isinstance(config["backend_config"], dict)
        and isinstance(config["embedder_type"], str)
        and config["embedder_type"]
        and isinstance(config["embed_model"], str)
        and isinstance(config["embedder_config"], dict)
    ):
        raise ValueError("collection config is invalid")
    return {key: config[key] for key in COLLECTION_CONFIG_FIELDS}

_COLLECTION_BASELINE_SQL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS documents (
        docid       TEXT PRIMARY KEY,
        version     INTEGER NOT NULL DEFAULT 1,
        ingested_at TEXT NOT NULL DEFAULT (
            strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
        ),
        meta_json   TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chunks (
        docid       TEXT NOT NULL,
        rid         TEXT PRIMARY KEY,
        chunk_path  TEXT,
        meta_json   TEXT NOT NULL DEFAULT '{}',
        ingested_at TEXT NOT NULL DEFAULT (
            strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
        )
    )
    """,
    "CREATE INDEX IF NOT EXISTS chunks_docid ON chunks (docid)",
    """
    CREATE TABLE IF NOT EXISTS chunk_meta (
        rid   TEXT NOT NULL,
        key   TEXT NOT NULL,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS chunk_meta_rid
        ON chunk_meta (rid)
    """,
    """
    CREATE INDEX IF NOT EXISTS chunk_meta_kv
        ON chunk_meta (key, value)
    """,
    """
    CREATE TABLE IF NOT EXISTS document_meta (
        docid TEXT NOT NULL,
        key   TEXT NOT NULL,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS document_meta_docid
        ON document_meta (docid)
    """,
    """
    CREATE INDEX IF NOT EXISTS document_meta_kv
        ON document_meta (key, value)
    """,
    """
    CREATE TABLE IF NOT EXISTS query_log (
        query_id             TEXT PRIMARY KEY,
        tenant               TEXT NOT NULL,
        collection           TEXT NOT NULL,
        actor                TEXT NOT NULL,
        query_text           TEXT NOT NULL,
        k                    INTEGER NOT NULL,
        filters_json         TEXT,
        include_common       INTEGER NOT NULL DEFAULT 0,
        common_tenant        TEXT,
        common_collection    TEXT,
        result_ids_json      TEXT,
        result_count         INTEGER NOT NULL DEFAULT 0,
        latency_ms           REAL,
        timing_json          TEXT,
        request_id           TEXT,
        replay_of            TEXT,
        executed_at          TEXT NOT NULL DEFAULT (
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_query_log_executed_at
        ON query_log (executed_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_query_log_replay_of
        ON query_log (replay_of)
    """,
]

_CATALOG_BASELINE_SQL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS collections (
        tenant              TEXT NOT NULL,
        name                TEXT NOT NULL,
        display_name        TEXT,
        meta_json           TEXT,
        backend_type        TEXT,
        backend_config_json TEXT,
        embedder_type       TEXT,
        embed_model         TEXT,
        embed_config_json   TEXT,
        created_at          TEXT NOT NULL DEFAULT (
            strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
        ),
        PRIMARY KEY (tenant, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS query_home (
        query_id    TEXT PRIMARY KEY,
        tenant      TEXT NOT NULL,
        collection  TEXT NOT NULL,
        created_at  TEXT NOT NULL DEFAULT (
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_query_home_tenant_coll
        ON query_home (tenant, collection)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_query_home_created_at
        ON query_home (created_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS metrics_state (
        singleton     INTEGER PRIMARY KEY CHECK (singleton = 1),
        generation    TEXT NOT NULL,
        last_error    TEXT,
        last_error_at REAL
    )
    """,
    """
    INSERT OR IGNORE INTO metrics_state (singleton, generation)
    VALUES (1, lower(hex(randomblob(16))))
    """,
    """
    CREATE TABLE IF NOT EXISTS metrics_counters (
        name        TEXT NOT NULL,
        labels_json TEXT NOT NULL,
        value       REAL NOT NULL,
        PRIMARY KEY (name, labels_json)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS metrics_latency_samples (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        operation TEXT NOT NULL,
        value     REAL NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_metrics_latency_operation
        ON metrics_latency_samples (operation, id DESC)
    """,
]


def _schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if row is None:
        return 0
    version_row = conn.execute(
        "SELECT MAX(version) FROM schema_migrations"
    ).fetchone()
    return int(version_row[0] or 0)


def _schema_version_error(
    kind: str,
    current: int,
    baseline: int,
    target: int,
) -> UnsupportedSchemaVersionError:
    if current > target:
        return UnsupportedSchemaVersionError(
            f"{kind} schema version {current} is newer than supported "
            f"version {target}; upgrade PaveDB"
        )
    if current < baseline:
        return UnsupportedSchemaVersionError(
            f"unsupported pre-v0.9.5 {kind} schema version {current}; "
            "recreate the data dir"
        )
    return UnsupportedSchemaVersionError(
        f"{kind} schema version {current} requires migration to {target}; "
        "open it read-write first"
    )


def _apply_schema_migrations(
    conn: sqlite3.Connection,
    *,
    kind: str,
    baseline: int,
    target: int,
    baseline_sql: list[str],
    migrations: dict[int, list[str]],
) -> None:
    if target < baseline:
        raise RuntimeError(f"{kind} schema target precedes its baseline")
    current = _schema_version(conn)
    if current == target:
        return
    conn.execute("BEGIN IMMEDIATE")
    current = _schema_version(conn)
    if current == target:
        return
    if current > target or (current != 0 and current < baseline):
        raise _schema_version_error(kind, current, baseline, target)

    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    if current == 0:
        for stmt in baseline_sql:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (baseline, datetime.now(tz.utc).isoformat(timespec="seconds")),
        )
        current = baseline

    for version in range(current + 1, target + 1):
        try:
            statements = migrations[version]
        except KeyError:
            raise RuntimeError(
                f"missing {kind} schema migration {version}"
            ) from None
        for stmt in statements:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, datetime.now(tz.utc).isoformat(timespec="seconds")),
        )


class CollectionDB:
    """Per-collection SQLite metadata and chunk-text store (impl2).

    Three persistent connections with check_same_thread=False:
      _rconn: used for reads (WAL, protected by _read_lock)
      _wconn: used for writes (protected by _write_lock and _writer)
      _log_conn: used for query_log writes (protected by _log_lock)
    """

    def __init__(self) -> None:
        self.path: Path | None = None
        self._rconn: sqlite3.Connection | None = None
        self._wconn: sqlite3.Connection | None = None
        self._log_conn: sqlite3.Connection | None = None
        self._read_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._state_cv = threading.Condition()
        self._active_readers = 0
        self._active_writers = 0
        self._closing = False

    def _open_conn(
        self,
        path: Path,
        *,
        read_only: bool = False,
        immutable: bool = False,
    ) -> sqlite3.Connection:
        """Open a single sqlite3 connection with standard pragmas."""
        if read_only:
            uri_options = "mode=ro&immutable=1" if immutable else "mode=ro"
            conn = sqlite3.connect(
                f"file:{path.as_posix()}?{uri_options}",
                uri=True,
                check_same_thread=False,
            )
        else:
            conn = sqlite3.connect(str(path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def open(
        self,
        path: Path,
        *,
        read_only: bool = False,
        immutable: bool = False,
    ) -> None:
        """Open (or create) the meta.db at *path*.

        When *read_only* is True only the read connection is opened
        and migrations are skipped.  Use this for fallback reads
        (``get_document``, ``catalog_metrics``, ``_read_meta_batch_safe``)
        where a write connection is unnecessary.

        Raises LegacyMetadataError if catalog.json or meta.json exist
        alongside the database file.
        """
        path = path.resolve()
        parent = path.parent
        if parent.exists():
            if ((parent / "catalog.json").exists()
                    or (parent / "meta.json").exists()):
                raise LegacyMetadataError(
                    f"Legacy catalog.json/meta.json detected in {parent}; "
                    "migration not supported — remove JSON files first."
                )
        if not read_only:
            parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._state_cv:
            self._rconn = self._open_conn(
                path,
                read_only=read_only,
                immutable=immutable,
            )
            self._wconn = None
            self._log_conn = None
            if not read_only:
                self._wconn = self._open_conn(path)
                self._log_conn = self._open_conn(path)
            self._active_readers = 0
            self._active_writers = 0
            self._closing = False
        try:
            if read_only:
                current = _schema_version(self._require_rconn())
                if current != _COLLECTION_SCHEMA_VERSION:
                    raise _schema_version_error(
                        "collection",
                        current,
                        _COLLECTION_BASELINE_SCHEMA_VERSION,
                        _COLLECTION_SCHEMA_VERSION,
                    )
            else:
                self._apply_migrations()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        with self._state_cv:
            if (
                self._rconn is None
                and self._wconn is None
                and self._log_conn is None
            ):
                self._closing = False
                return
            self._closing = True
            while self._active_readers > 0 or self._active_writers > 0:
                self._state_cv.wait(timeout=0.05)
            rconn = self._rconn
            wconn = self._wconn
            log_conn = self._log_conn
            self._rconn = None
            self._wconn = None
            self._log_conn = None
            self._closing = False

        if rconn is not None:
            rconn.close()
        if wconn is not None:
            wconn.close()
        if log_conn is not None:
            log_conn.close()

    def checkpoint_wal(self) -> None:
        """Make the collection database self-contained for an archive dump."""
        with self._writer() as conn:
            result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result is None or result[0]:
            raise sqlite3.OperationalError("collection WAL checkpoint is busy")

    @property
    def _conn(self) -> sqlite3.Connection | None:
        """Return read connection (for test introspection)."""
        return self._rconn

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_rconn(self) -> sqlite3.Connection:
        if self._rconn is None:
            raise RuntimeError("CollectionDB not opened; call open() first.")
        return self._rconn

    def _require_wconn(self) -> sqlite3.Connection:
        if self._wconn is None:
            raise RuntimeError("CollectionDB not opened; call open() first.")
        return self._wconn

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        with self._state_cv:
            if self._rconn is None:
                raise RuntimeError("CollectionDB not opened; call open() first.")
            if self._closing:
                raise RuntimeError("CollectionDB is closing.")
            self._active_readers += 1
            conn = self._rconn
        self._read_lock.acquire()
        try:
            yield conn
        finally:
            self._read_lock.release()
            with self._state_cv:
                if self._active_readers > 0:
                    self._active_readers -= 1
                if self._closing and self._active_readers == 0:
                    self._state_cv.notify_all()

    @contextmanager
    def _writer_for(
        self,
        lock,
        attr: str,
    ) -> Iterator[sqlite3.Connection]:
        lock.acquire()
        try:
            with self._state_cv:
                conn = getattr(self, attr)
                if conn is None:
                    raise RuntimeError(
                        "CollectionDB not opened; call open() first."
                    )
                if self._closing:
                    raise RuntimeError("CollectionDB is closing.")
                self._active_writers += 1
            try:
                yield conn
            finally:
                with self._state_cv:
                    if self._active_writers > 0:
                        self._active_writers -= 1
                    if (
                        self._closing
                        and self._active_readers == 0
                        and self._active_writers == 0
                    ):
                        self._state_cv.notify_all()
        finally:
            lock.release()

    @contextmanager
    def _writer(self) -> Iterator[sqlite3.Connection]:
        with self._writer_for(self._write_lock, "_wconn") as conn:
            yield conn

    @contextmanager
    def _query_log_writer(self) -> Iterator[sqlite3.Connection]:
        with self._writer_for(self._log_lock, "_log_conn") as conn:
            yield conn

    def _apply_migrations(self) -> None:
        with self._writer() as conn, conn:
            try:
                _apply_schema_migrations(
                    conn,
                    kind="collection",
                    baseline=_COLLECTION_BASELINE_SCHEMA_VERSION,
                    target=_COLLECTION_SCHEMA_VERSION,
                    baseline_sql=_COLLECTION_BASELINE_SQL,
                    migrations=_COLLECTION_MIGRATIONS,
                )
            except sqlite3.Error as exc:
                raise SchemaMigrationError(
                    "collection schema migration failed; "
                    f"database unchanged: {exc}"
                ) from exc
            conn.commit()

    @staticmethod
    def _schema_version(conn: sqlite3.Connection) -> int:
        return _schema_version(conn)

    @staticmethod
    def _dump_json(payload: dict[str, Any] | None) -> str | None:
        if payload is None:
            return None
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _load_json(payload: str | None) -> dict[str, Any]:
        if not payload:
            return {}
        try:
            loaded = json.loads(payload)
        except Exception:
            return {}
        return loaded if isinstance(loaded, dict) else {}

    @staticmethod
    def _normalize_utc_timestamp(ts: str | None) -> str | None:
        if isinstance(ts, str) and ts.endswith("+00:00"):
            return ts.replace("+00:00", "Z")
        return ts

    @staticmethod
    def _meta_kv_rows(
        owner: str,
        meta: dict[str, Any],
    ) -> list[tuple[str, str, str]]:
        return [
            (owner, str(key), str(item))
            for key, value in meta.items()
            for item in (
                value if isinstance(value, (list, tuple, set)) else (value,)
            )
        ]

    # ------------------------------------------------------------------
    # Write operations — serialised by _write_lock, use _wconn
    # ------------------------------------------------------------------

    def upsert_chunks(
        self,
        docid: str,
        chunks: list[tuple[str, str | None, dict[str, Any]]],
        doc_meta: dict[str, Any] | None = None,
        *,
        texts: dict[str, str | None] | None = None,
    ) -> None:
        """Insert/replace chunk rows and upsert the document row.

        All writes happen in a single transaction.
        Must be called inside collection_lock.
        """
        doc_meta_dict = doc_meta or {}
        doc_meta_json = json.dumps(doc_meta_dict, ensure_ascii=False)
        now = datetime.now(tz.utc).isoformat(timespec="seconds")
        rows = []
        for rid, chunk_path, meta in chunks:
            meta_json = json.dumps(meta, ensure_ascii=False)
            text = texts.get(rid) if texts is not None else None
            rows.append((docid, rid, chunk_path, text, meta_json, now))
        with self._writer() as conn, conn:
            conn.execute(
                """
                INSERT INTO documents (docid, version, ingested_at, meta_json)
                VALUES (
                    ?,
                    COALESCE(
                        (SELECT version FROM documents WHERE docid=?), 0
                    ) + 1,
                    ?,
                    ?
                )
                ON CONFLICT(docid) DO UPDATE SET
                    version=excluded.version,
                    ingested_at=excluded.ingested_at,
                    meta_json=excluded.meta_json
                """,
                (docid, docid, now, doc_meta_json),
            )
            conn.execute(
                "DELETE FROM document_meta WHERE docid=?",
                (docid,),
            )
            doc_kv_rows = self._meta_kv_rows(docid, doc_meta_dict)
            if doc_kv_rows:
                conn.executemany(
                    "INSERT INTO document_meta "
                    "(docid, key, value) VALUES (?, ?, ?)",
                    doc_kv_rows,
                )
            conn.executemany(
                """
                INSERT INTO chunks
                (docid, rid, chunk_path, text, meta_json, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(rid) DO UPDATE SET
                    docid=excluded.docid,
                    chunk_path=excluded.chunk_path,
                    text=excluded.text,
                    meta_json=excluded.meta_json,
                    ingested_at=excluded.ingested_at
                """,
                rows,
            )
            if chunks:
                rids = [rid for rid, _chunk_path, _meta in chunks]
                for i in range(0, len(rids), 999):
                    batch = rids[i : i + 999]
                    placeholders = ",".join(["?"] * len(batch))
                    conn.execute(
                        f"DELETE FROM chunk_meta "
                        f"WHERE rid IN ({placeholders})",
                        batch,
                    )
            kv_rows: list[tuple[str, str, str]] = []
            for rid, _chunk_path, meta in chunks:
                kv_rows.extend(self._meta_kv_rows(rid, meta))
            if kv_rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO chunk_meta "
                    "(rid, key, value) VALUES (?, ?, ?)",
                    kv_rows,
                )

    def legacy_chunk_text_rows(self) -> list[tuple[str, str, str | None]]:
        """Return rows still pointing at a pre-v1.2 text sidecar."""
        with self._reader() as conn:
            rows = conn.execute(
                "SELECT rid, chunk_path, text FROM chunks "
                "WHERE chunk_path IS NOT NULL ORDER BY rowid"
            ).fetchall()
        return [
            (str(rid), str(chunk_path), text)
            for rid, chunk_path, text in rows
        ]

    def migrate_chunk_texts(self, texts: dict[str, str]) -> None:
        """Copy verified legacy sidecar text into canonical SQLite rows."""
        if not texts:
            return
        with self._writer() as conn, conn:
            conn.executemany(
                "UPDATE chunks SET text=? WHERE rid=?",
                [(text, rid) for rid, text in texts.items()],
            )

    def clear_chunk_paths(self, rids: list[str]) -> None:
        """Clear obsolete sidecar pointers after their files are removed."""
        if not rids:
            return
        with self._writer() as conn, conn:
            for i in range(0, len(rids), 999):
                batch = rids[i : i + 999]
                placeholders = ",".join(["?"] * len(batch))
                conn.execute(
                    f"UPDATE chunks SET chunk_path=NULL "
                    f"WHERE rid IN ({placeholders})",
                    batch,
                )

    def get_chunk_text(self, rid: str) -> str | None:
        with self._reader() as conn:
            row = conn.execute(
                "SELECT text FROM chunks WHERE rid=?",
                (rid,),
            ).fetchone()
        return None if row is None else row[0]

    def get_chunk_texts(self, rids: list[str]) -> dict[str, str | None]:
        if not rids:
            return {}
        out: dict[str, str | None] = {}
        with self._reader() as conn:
            for i in range(0, len(rids), 999):
                batch = rids[i : i + 999]
                placeholders = ",".join(["?"] * len(batch))
                rows = conn.execute(
                    f"SELECT rid, text FROM chunks "
                    f"WHERE rid IN ({placeholders})",
                    batch,
                ).fetchall()
                out.update((str(rid), text) for rid, text in rows)
        return out

    def rebuild_text_indexes(self) -> None:
        """Rebuild both FTS5 indexes from canonical non-null chunk text."""
        with self._writer() as conn, conn:
            conn.execute(
                "INSERT INTO chunk_text_fts(chunk_text_fts) VALUES ('rebuild')"
            )
            conn.execute(
                "INSERT INTO chunk_text_trigram(chunk_text_trigram) "
                "VALUES ('rebuild')"
            )

    def verify_text_indexes(self) -> None:
        """Verify both external-content indexes against canonical text."""
        with self._writer() as conn, conn:
            conn.execute(
                "INSERT INTO chunk_text_fts(chunk_text_fts, rank) "
                "VALUES ('integrity-check', 1)"
            )
            conn.execute(
                "INSERT INTO chunk_text_trigram(chunk_text_trigram, rank) "
                "VALUES ('integrity-check', 1)"
            )

    def delete_doc(self, docid: str) -> list[str]:
        """Delete all chunks and the document row for *docid*.

        Returns the list of rids that were deleted.
        Must be called inside collection_lock.
        """
        # Read rids using _rconn (no lock needed)
        with self._reader() as rconn:
            cur = rconn.execute(
                "SELECT rid FROM chunks WHERE docid=?", (docid,)
            )
            rids = [row[0] for row in cur.fetchall()]
        # Write using _wconn
        with self._writer() as conn, conn:
            if rids:
                for i in range(0, len(rids), 999):
                    batch = rids[i : i + 999]
                    placeholders = ",".join(["?"] * len(batch))
                    conn.execute(
                        f"DELETE FROM chunk_meta "
                        f"WHERE rid IN ({placeholders})",
                        batch,
                    )
            conn.execute("DELETE FROM document_meta WHERE docid=?", (docid,))
            conn.execute("DELETE FROM chunks WHERE docid=?", (docid,))
            conn.execute("DELETE FROM documents WHERE docid=?", (docid,))
        return rids

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
        content_filter: dict[str, str] | None = None,
        mode: str = "vector",
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
        with self._query_log_writer() as conn, conn:
            conn.execute(
                "INSERT INTO query_log "
                "(query_id, tenant, collection, actor, query_text, k, "
                "filters_json, content_filter_json, mode, include_common, "
                "common_tenant, common_collection, "
                "result_ids_json, result_count, latency_ms, timing_json, "
                "request_id, replay_of) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    query_id,
                    tenant,
                    collection,
                    actor,
                    query_text,
                    k,
                    self._dump_json(filters),
                    self._dump_json(content_filter),
                    mode,
                    1 if include_common else 0,
                    common_tenant,
                    common_collection,
                    json.dumps(result_ids, ensure_ascii=False)
                    if result_ids else None,
                    result_count,
                    latency_ms,
                    self._dump_json(timing),
                    request_id,
                    replay_of,
                ),
            )

    def get_query_log_entry(
        self,
        query_id: str,
    ) -> dict[str, Any] | None:
        with self._reader() as conn:
            row = conn.execute(
                "SELECT query_id, tenant, collection, actor, "
                "query_text, k, filters_json, content_filter_json, "
                "mode, include_common, common_tenant, common_collection, "
                "result_ids_json, result_count, latency_ms, timing_json, "
                "request_id, replay_of, executed_at "
                "FROM query_log WHERE query_id = ?",
                (query_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "query_id": row[0],
            "tenant": row[1],
            "collection": row[2],
            "actor": row[3],
            "query_text": row[4],
            "k": row[5],
            "filters": self._load_json(row[6]),
            "content_filter": self._load_json(row[7]) if row[7] else None,
            "mode": row[8],
            "include_common": bool(row[9]),
            "common_tenant": row[10],
            "common_collection": row[11],
            "result_ids": json.loads(row[12]) if row[12] else [],
            "result_count": row[13],
            "latency_ms": row[14],
            "timing": self._load_json(row[15]),
            "request_id": row[16],
            "replay_of": row[17],
            "executed_at": row[18],
        }

    def list_query_logs(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self._reader() as conn:
            rows = conn.execute(
                "SELECT query_id, query_text, k, result_count, "
                "latency_ms, request_id, replay_of, executed_at "
                "FROM query_log "
                "ORDER BY executed_at DESC "
                "LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [
            {
                "query_id": row[0],
                "query_text": row[1],
                "k": row[2],
                "result_count": row[3],
                "latency_ms": row[4],
                "request_id": row[5],
                "replay_of": row[6],
                "executed_at": row[7],
            }
            for row in rows
        ]

    def query_log_gauge_stats(self) -> tuple[int, float | None]:
        with self._reader() as conn:
            row = conn.execute(
                "SELECT COUNT(*), MIN(strftime('%s', executed_at)) "
                "FROM query_log"
            ).fetchone()
        return int(row[0]), None if row[1] is None else float(row[1])

    def chunk_paths(self) -> set[str]:
        with self._reader() as conn:
            rows = conn.execute(
                "SELECT chunk_path FROM chunks "
                "WHERE text IS NULL AND chunk_path IS NOT NULL"
            ).fetchall()
        return {str(row[0]) for row in rows}

    # ------------------------------------------------------------------
    # Read operations — use _rconn serialized by _read_lock (WAL)
    # ------------------------------------------------------------------

    def has_doc(self, docid: str) -> bool:
        """Return True if *docid* has at least one chunk row."""
        with self._reader() as conn:
            cur = conn.execute(
                "SELECT 1 FROM chunks WHERE docid=? LIMIT 1", (docid,)
            )
            return cur.fetchone() is not None

    def get_rids_for_doc(self, docid: str) -> list[str]:
        with self._reader() as conn:
            cur = conn.execute(
                "SELECT rid FROM chunks WHERE docid=?", (docid,)
            )
            return [row[0] for row in cur.fetchall()]

    def get_doc_version(self, docid: str) -> int | None:
        with self._reader() as conn:
            cur = conn.execute(
                "SELECT version FROM documents WHERE docid=?", (docid,)
            )
            row = cur.fetchone()
            return int(row[0]) if row else None

    def get_document(self, docid: str) -> dict[str, Any] | None:
        """Return document metadata and chunk ids for *docid*."""
        with self._reader() as conn:
            cur = conn.execute(
                "SELECT version, ingested_at, meta_json "
                "FROM documents WHERE docid=?",
                (docid,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            version, ingested_at, meta_json = row
            ingested_at = self._normalize_utc_timestamp(ingested_at)
            metadata: dict[str, Any] = {}
            try:
                loaded = json.loads(meta_json) if meta_json else {}
            except Exception:
                loaded = {}
            if isinstance(loaded, dict):
                metadata = loaded

            chunk_cur = conn.execute(
                "SELECT rid FROM chunks WHERE docid=? ORDER BY rowid",
                (docid,),
            )
            chunk_ids = [str(chunk_row[0]) for chunk_row in chunk_cur.fetchall()]
            return {
                "docid": docid,
                "version": int(version or 0),
                "ingested_at": ingested_at,
                "metadata": metadata,
                "chunk_ids": chunk_ids,
                "chunk_count": len(chunk_ids),
            }

    def list_chunks(self, docid: str) -> list[dict[str, Any]]:
        with self._reader() as conn:
            rows = conn.execute(
                "SELECT rid, chunk_path, meta_json, ingested_at "
                "FROM chunks WHERE docid=? "
                "ORDER BY rowid",
                (docid,),
            ).fetchall()
        return [
            {
                "rid": row[0],
                "chunk_path": row[1],
                "meta": self._load_json(row[2]),
                "ingested_at": self._normalize_utc_timestamp(row[3]),
            }
            for row in rows
        ]

    def get_chunk(self, rid: str) -> dict[str, Any] | None:
        with self._reader() as conn:
            row = conn.execute(
                "SELECT docid, rid, chunk_path, meta_json, ingested_at "
                "FROM chunks WHERE rid=?",
                (rid,),
            ).fetchone()
        if row is None:
            return None
        return {
            "docid": row[0],
            "rid": row[1],
            "chunk_path": row[2],
            "meta": self._load_json(row[3]),
            "ingested_at": self._normalize_utc_timestamp(row[4]),
        }

    def list_documents(self) -> list[dict[str, Any]]:
        """Return summary rows for all documents in this collection."""
        with self._reader() as conn:
            # Count chunks in the same query so listings avoid an N+1 scan
            # across the chunks table for each document row.
            cur = conn.execute(
                "SELECT d.docid, d.version, d.ingested_at, "
                "       COUNT(c.rid) AS chunk_count "
                "FROM documents AS d "
                "LEFT JOIN chunks AS c ON c.docid = d.docid "
                "GROUP BY d.docid "
                "ORDER BY d.rowid"
            )
            docs: list[dict[str, Any]] = []
            for docid, version, ingested_at, chunk_count in cur.fetchall():
                ingested_at = self._normalize_utc_timestamp(ingested_at)
                docs.append(
                    {
                        "docid": docid,
                        "version": int(version or 0),
                        "ingested_at": ingested_at,
                        "chunk_count": int(chunk_count or 0),
                    }
                )
            return docs

    def chunk_counts(self, docid: str) -> tuple[int, int]:
        """Return (total chunks, chunks of *docid*) for this collection."""
        with self._reader() as conn:
            cur = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(docid=?), 0) FROM chunks",
                (docid,),
            )
            row = cur.fetchone()
            if row is None:
                return (0, 0)
            return (int(row[0] or 0), int(row[1] or 0))

    def get_doc_chunk_counts(self) -> tuple[int, int]:
        """Return (doc_count, chunk_count) for this collection."""
        with self._reader() as conn:
            cur = conn.execute(
                "SELECT COUNT(DISTINCT docid), COUNT(*) FROM chunks"
            )
            row = cur.fetchone()
            if row is None:
                return (0, 0)
            return (int(row[0] or 0), int(row[1] or 0))

    def get_chunk_rids(self) -> set[str]:
        with self._reader() as conn:
            return {
                str(row[0])
                for row in conn.execute("SELECT rid FROM chunks")
            }

    @staticmethod
    def _fts5_quote(value: str) -> str:
        """Return one literal FTS5 phrase without exposing query syntax."""
        return '"' + value.replace('"', '""') + '"'

    def filter_by_content(
        self,
        candidate_rids: list[str],
        content_filter: dict[str, str],
    ) -> set[str]:
        """Reduce vector candidates with exact or indexed text matching."""
        if not candidate_rids:
            return set()
        op = str(content_filter.get("op") or "")
        value = content_filter.get("value")
        if op not in {"exact", "phrase", "prefix", "contains"}:
            raise ValueError(f"unsupported content filter operation: {op!r}")
        if not isinstance(value, str) or not value:
            raise ValueError("content filter value must be a non-empty string")
        if op == "contains" and len(value) < 3:
            raise ValueError(
                "contains content filter requires at least three characters"
            )

        matches: set[str] = set()
        if op == "exact":
            table = None
            query = value
        elif op == "contains":
            table = "chunk_text_trigram"
            query = self._fts5_quote(value)
        else:
            table = "chunk_text_fts"
            query = self._fts5_quote(value)
            if op == "prefix":
                # FTS5 recognizes a trailing star outside the quoted phrase
                # as a prefix marker for the phrase's final token.
                query += " *"

        with self._reader() as conn:
            for i in range(0, len(candidate_rids), 998):
                batch = candidate_rids[i : i + 998]
                placeholders = ",".join(["?"] * len(batch))
                if table is None:
                    rows = conn.execute(
                        f"SELECT rid FROM chunks WHERE text=? "
                        f"AND rid IN ({placeholders})",
                        [query, *batch],
                    ).fetchall()
                else:
                    rows = conn.execute(
                        f"SELECT c.rid FROM chunks AS c "
                        f"JOIN {table} ON {table}.rowid=c.rowid "
                        f"WHERE {table} MATCH ? "
                        f"AND c.rid IN ({placeholders})",
                        [query, *batch],
                    ).fetchall()
                matches.update(str(row[0]) for row in rows)
        return matches

    def lexical_candidates(
        self,
        query: str,
        limit: int,
    ) -> list[tuple[str, float]]:
        """Return independent FTS5/BM25 candidates for future hybrid search."""
        tokens = [
            match.group(0)
            for match in re.finditer(r"\w+", query, flags=re.UNICODE)
        ]
        if not tokens or limit <= 0:
            return []
        fts_query = " OR ".join(self._fts5_quote(token) for token in tokens)
        with self._reader() as conn:
            rows = conn.execute(
                "SELECT c.rid, bm25(chunk_text_fts) AS score "
                "FROM chunk_text_fts "
                "JOIN chunks AS c ON c.rowid=chunk_text_fts.rowid "
                "WHERE chunk_text_fts MATCH ? "
                "ORDER BY score, c.rid LIMIT ?",
                (fts_query, int(limit)),
            ).fetchall()
        return [(str(rid), float(score)) for rid, score in rows]

    def _chunk_meta_matches(
        self,
        conn: sqlite3.Connection,
        candidate_rids: list[str],
        key: str,
        value: str,
    ) -> set[str]:
        matches: set[str] = set()
        for i in range(0, len(candidate_rids), 999):
            batch = candidate_rids[i : i + 999]
            placeholders = ",".join(["?"] * len(batch))
            cur = conn.execute(
                f"SELECT rid FROM chunk_meta "
                f"WHERE key=? AND value=? "
                f"AND rid IN ({placeholders})",
                [key, value, *batch],
            )
            matches.update(row[0] for row in cur.fetchall())
        return matches

    def _document_meta_matches(
        self,
        conn: sqlite3.Connection,
        candidate_rids: list[str],
        key: str,
        value: str,
    ) -> set[str]:
        matches: set[str] = set()
        for i in range(0, len(candidate_rids), 999):
            batch = candidate_rids[i : i + 999]
            placeholders = ",".join(["?"] * len(batch))
            cur = conn.execute(
                f"SELECT c.rid FROM chunks AS c "
                f"JOIN document_meta AS dm ON dm.docid = c.docid "
                f"WHERE dm.key=? AND dm.value=? "
                f"AND NOT EXISTS ("
                f"SELECT 1 FROM chunk_meta AS cm "
                f"WHERE cm.rid=c.rid AND cm.key=dm.key"
                f") "
                f"AND c.rid IN ({placeholders})",
                [key, value, *batch],
            )
            matches.update(row[0] for row in cur.fetchall())
        return matches

    def _docid_matches(
        self,
        conn: sqlite3.Connection,
        candidate_rids: list[str],
        value: str,
    ) -> set[str]:
        matches: set[str] = set()
        for i in range(0, len(candidate_rids), 999):
            batch = candidate_rids[i : i + 999]
            placeholders = ",".join(["?"] * len(batch))
            cur = conn.execute(
                f"SELECT rid FROM chunks "
                f"WHERE docid=? AND rid IN ({placeholders})",
                [value, *batch],
            )
            matches.update(row[0] for row in cur.fetchall())
        return matches

    def filter_by_meta(
        self,
        candidate_rids: list[str],
        filters: dict[str, list[Any]],
    ) -> set[str]:
        """Reduce candidates via SQL on chunk/document metadata.

        Handles exact-match only.
        Negation (!value), values with *, >, <, >=, <= are skipped (left for
        caller's canonical post-filter). If a single key mixes
        pushdown-able and non-pushdown values, that key is skipped
        entirely to preserve OR semantics before the canonical
        post-filter runs.
        Returns subset of candidate_rids passing all
        pushdown-able conditions.
        """
        if not candidate_rids:
            return set()
        if not filters:
            return set(candidate_rids)

        current = set(candidate_rids)
        skip_prefixes = ("!", ">=", "<=", ">", "<")

        with self._reader() as conn:
            for key, values in filters.items():
                if not current:
                    break

                normalized_values: list[str] = []
                mixed_semantics = False
                for raw_value in values:
                    if not isinstance(raw_value, str):
                        continue
                    value = sanit_sql(raw_value)
                    if "*" in value or value.startswith(skip_prefixes):
                        mixed_semantics = True
                        break
                    normalized_values.append(value)

                # OR within a key means any non-pushdown value can still admit
                # additional matches, so narrowing by the pushdown subset would
                # change canonical search semantics.
                if mixed_semantics or not normalized_values:
                    continue

                current_batch = list(current)
                key_matches: set[str] = set()

                for value in normalized_values:
                    if key == "docid":
                        matched = self._docid_matches(
                            conn,
                            current_batch,
                            value,
                        )
                    else:
                        matched = self._chunk_meta_matches(
                            conn,
                            current_batch,
                            key,
                            value,
                        )
                        matched.update(
                            self._document_meta_matches(
                                conn,
                                current_batch,
                                key,
                                value,
                            )
                        )
                    key_matches.update(matched)

                current.intersection_update(key_matches)

        return current

    def get_meta_batch(self, rids: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch merged document + chunk metadata for *rids*.

        Called OUTSIDE collection_lock. WAL keeps this compatible with writers,
        but Python sqlite access to the shared read connection is serialized.
        Chunks the rid list into groups of 999 (SQLite variable limit).
        """
        if not rids:
            return {}
        with self._reader() as conn:
            out: dict[str, dict[str, Any]] = {}
            chunk_size = 999
            for i in range(0, len(rids), chunk_size):
                batch = rids[i : i + chunk_size]
                placeholders = ",".join(["?"] * len(batch))
                cur = conn.execute(
                    f"SELECT c.rid, c.meta_json, d.meta_json "
                    f"FROM chunks AS c "
                    f"LEFT JOIN documents AS d ON d.docid = c.docid "
                    f"WHERE c.rid IN ({placeholders})",
                    batch,
                )
                for rid, chunk_meta_json, doc_meta_json in cur.fetchall():
                    merged: dict[str, Any] = {}
                    try:
                        doc_meta = (
                            json.loads(doc_meta_json) if doc_meta_json else {}
                        )
                    except Exception:
                        doc_meta = {}
                    if isinstance(doc_meta, dict):
                        merged.update(doc_meta)
                    try:
                        chunk_meta = (
                            json.loads(chunk_meta_json)
                            if chunk_meta_json else {}
                        )
                    except Exception:
                        chunk_meta = {}
                    if isinstance(chunk_meta, dict):
                        merged.update(chunk_meta)
                    out[rid] = merged
        return out

    def filter_meta_batch(
        self,
        candidate_rids: list[str],
        filters: dict[str, list[Any]],
    ) -> tuple[set[str], dict[str, dict[str, Any]], dict[str, float]]:
        """Filter and hydrate candidates from one read transaction."""
        with self._reader() as conn:
            conn.execute("BEGIN")
            try:
                t0 = time.perf_counter()
                surviving = self.filter_by_meta(candidate_rids, filters)
                filter_ms = (time.perf_counter() - t0) * 1000.0
                rids = [rid for rid in candidate_rids if rid in surviving]
                t0 = time.perf_counter()
                meta = self.get_meta_batch(rids)
                hydrate_ms = (time.perf_counter() - t0) * 1000.0
                return surviving, meta, {
                    "filter_ms": filter_ms,
                    "hydrate_ms": hydrate_ms,
                }
            finally:
                conn.execute("ROLLBACK")


class CatalogDB:
    """Global SQLite catalog for collection listing and config."""

    def __init__(self) -> None:
        self.path: Path | None = None
        self._rconn: sqlite3.Connection | None = None
        self._wconn: sqlite3.Connection | None = None
        self._read_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._state_cv = threading.Condition()
        self._active_readers = 0
        self._active_writers = 0
        self._closing = False

    def _open_conn(self, path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def open(self, path: Path) -> None:
        path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._state_cv:
            self._rconn = self._open_conn(path)
            self._wconn = self._open_conn(path)
            self._active_readers = 0
            self._active_writers = 0
            self._closing = False
        try:
            self._apply_migrations()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        with self._state_cv:
            if self._rconn is None and self._wconn is None:
                self._closing = False
                return
            self._closing = True
            while self._active_readers > 0 or self._active_writers > 0:
                self._state_cv.wait(timeout=0.05)
            rconn = self._rconn
            wconn = self._wconn
            self._rconn = None
            self._wconn = None
            self._closing = False

        if rconn is not None:
            rconn.close()
        if wconn is not None:
            wconn.close()

    def checkpoint_wal(self) -> None:
        """Make the catalog database self-contained for an archive dump."""
        with self._writer() as conn:
            result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result is None or result[0]:
            raise sqlite3.OperationalError("catalog WAL checkpoint is busy")

    @property
    def _conn(self) -> sqlite3.Connection | None:
        """Return read connection (for test introspection)."""
        return self._rconn

    def _require_rconn(self) -> sqlite3.Connection:
        if self._rconn is None:
            raise RuntimeError("CatalogDB not opened; call open() first.")
        return self._rconn

    def _require_wconn(self) -> sqlite3.Connection:
        if self._wconn is None:
            raise RuntimeError("CatalogDB not opened; call open() first.")
        return self._wconn

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        with self._state_cv:
            if self._rconn is None:
                raise RuntimeError("CatalogDB not opened; call open() first.")
            if self._closing:
                raise RuntimeError("CatalogDB is closing.")
            self._active_readers += 1
            conn = self._rconn
        self._read_lock.acquire()
        try:
            yield conn
        finally:
            self._read_lock.release()
            with self._state_cv:
                if self._active_readers > 0:
                    self._active_readers -= 1
                if self._closing and self._active_readers == 0:
                    self._state_cv.notify_all()

    @contextmanager
    def _writer(self) -> Iterator[sqlite3.Connection]:
        self._write_lock.acquire()
        try:
            with self._state_cv:
                if self._wconn is None:
                    raise RuntimeError(
                        "CatalogDB not opened; call open() first."
                    )
                if self._closing:
                    raise RuntimeError("CatalogDB is closing.")
                self._active_writers += 1
                conn = self._wconn
            try:
                yield conn
            finally:
                with self._state_cv:
                    if self._active_writers > 0:
                        self._active_writers -= 1
                    if (
                        self._closing
                        and self._active_readers == 0
                        and self._active_writers == 0
                    ):
                        self._state_cv.notify_all()
        finally:
            self._write_lock.release()

    def _apply_migrations(self) -> None:
        with self._writer() as conn, conn:
            try:
                _apply_schema_migrations(
                    conn,
                    kind="catalog",
                    baseline=_CATALOG_BASELINE_SCHEMA_VERSION,
                    target=_CATALOG_SCHEMA_VERSION,
                    baseline_sql=_CATALOG_BASELINE_SQL,
                    migrations=_CATALOG_MIGRATIONS,
                )
            except sqlite3.Error as exc:
                raise SchemaMigrationError(
                    "catalog schema migration failed; "
                    f"database unchanged: {exc}"
                ) from exc
            conn.commit()

    @staticmethod
    def _schema_version(conn: sqlite3.Connection) -> int:
        return _schema_version(conn)

    @staticmethod
    def _dump_json(payload: dict[str, Any] | None) -> str | None:
        if payload is None:
            return None
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _load_json(payload: str | None) -> dict[str, Any]:
        if not payload:
            return {}
        try:
            loaded = json.loads(payload)
        except Exception:
            return {}
        return loaded if isinstance(loaded, dict) else {}

    @staticmethod
    def _reindex_job_from_row(row: sqlite3.Row | tuple) -> dict[str, Any]:
        return {
            "job_id": str(row[0]),
            "tenant": str(row[1]),
            "collection": str(row[2]),
            "status": str(row[3]),
            "source_config": CatalogDB._load_json(str(row[4])),
            "target_config": CatalogDB._load_json(str(row[5])),
            "source_digest": row[6],
            "total_chunks": int(row[7] or 0),
            "completed_chunks": int(row[8] or 0),
            "error": row[9],
            "created_at": str(row[10]),
            "updated_at": str(row[11]),
            "started_at": row[12],
            "finished_at": row[13],
        }

    @staticmethod
    def _reindex_job_select() -> str:
        return (
            "SELECT job_id, tenant, collection, status, "
            "source_config_json, target_config_json, source_digest, "
            "total_chunks, completed_chunks, error, created_at, updated_at, "
            "started_at, finished_at FROM reindex_jobs"
        )

    def create_reindex_job(
        self,
        job_id: str,
        tenant: str,
        collection: str,
        source_config: dict[str, Any],
        target_config: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist one queued reindex job, rejecting overlapping work."""
        with self._writer() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                active = conn.execute(
                    "SELECT job_id FROM reindex_jobs "
                    "WHERE tenant=? AND collection=? AND status IN ("
                    "'queued', 'running', 'pausing', 'paused', 'committing', "
                    "'cancelling', 'failed') LIMIT 1",
                    (tenant, collection),
                ).fetchone()
                if active is not None:
                    if str(active[0]) != job_id:
                        raise ReindexJobConflictError(
                            "collection already has an unfinished reindex job: "
                            f"{active[0]}"
                        )
                else:
                    conn.execute(
                        "INSERT INTO reindex_jobs ("
                        "job_id, tenant, collection, status, "
                        "source_config_json, target_config_json) "
                        "VALUES (?, ?, ?, 'queued', ?, ?)",
                        (
                            job_id,
                            tenant,
                            collection,
                            json.dumps(source_config, sort_keys=True),
                            json.dumps(target_config, sort_keys=True),
                        ),
                    )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        job = self.get_reindex_job(job_id)
        if job is None:
            raise RuntimeError(f"reindex job disappeared after create: {job_id}")
        return job

    def get_reindex_job(self, job_id: str) -> dict[str, Any] | None:
        with self._reader() as conn:
            row = conn.execute(
                self._reindex_job_select() + " WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return None if row is None else self._reindex_job_from_row(row)

    def list_reindex_jobs(
        self,
        statuses: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        query = self._reindex_job_select()
        params: list[str] = []
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            query += f" WHERE status IN ({placeholders})"
            params.extend(statuses)
        query += " ORDER BY created_at"
        with self._reader() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._reindex_job_from_row(row) for row in rows]

    def update_reindex_job(
        self,
        job_id: str,
        *,
        expected_statuses: tuple[str, ...] | None = None,
        status: str | None = None,
        source_digest: str | None = None,
        total_chunks: int | None = None,
        completed_chunks: int | None = None,
        error: str | None = None,
        clear_error: bool = False,
        set_started: bool = False,
        set_finished: bool = False,
        clear_finished: bool = False,
    ) -> bool:
        """Conditionally update a job and report whether the CAS matched."""
        assignments = [
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
        ]
        params: list[Any] = []
        if status is not None:
            assignments.append("status=?")
            params.append(status)
        if source_digest is not None:
            assignments.append("source_digest=?")
            params.append(source_digest)
        if total_chunks is not None:
            assignments.append("total_chunks=?")
            params.append(int(total_chunks))
        if completed_chunks is not None:
            assignments.append("completed_chunks=?")
            params.append(int(completed_chunks))
        if error is not None:
            assignments.append("error=?")
            params.append(error)
        elif clear_error:
            assignments.append("error=NULL")
        if set_started:
            assignments.append(
                "started_at=COALESCE(started_at, "
                "strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            )
        if set_finished:
            assignments.append(
                "finished_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
            )
        elif clear_finished:
            assignments.append("finished_at=NULL")

        where = "job_id=?"
        params.append(job_id)
        if expected_statuses:
            placeholders = ",".join("?" for _ in expected_statuses)
            where += f" AND status IN ({placeholders})"
            params.extend(expected_statuses)
        with self._writer() as conn, conn:
            cur = conn.execute(
                "UPDATE reindex_jobs SET " + ", ".join(assignments)
                + " WHERE " + where,
                params,
            )
        return cur.rowcount > 0

    def interrupt_reindex_jobs(self) -> None:
        """Make jobs abandoned by a prior process explicitly resumable."""
        with self._writer() as conn, conn:
            conn.execute(
                "UPDATE reindex_jobs SET status='paused', "
                "error='interrupted by server shutdown', "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                "WHERE status IN ('queued', 'running', 'pausing')"
            )
            conn.execute(
                "UPDATE reindex_jobs SET status='cancelled', "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), "
                "finished_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                "WHERE status='cancelling'"
            )

    def purge_reindex_jobs_for_collection(
        self,
        tenant: str,
        collection: str,
    ) -> list[str]:
        """Drop a deleted collection's reindex jobs, returning their ids.

        Job rows are per-collection catalog state: left behind, an
        unfinished one keeps holding the collection's single active-job
        slot, so a collection recreated under the same slug can never
        start a reindex again.
        """
        with self._writer() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    "SELECT job_id FROM reindex_jobs "
                    "WHERE tenant=? AND collection=?",
                    (tenant, collection),
                ).fetchall()
                conn.execute(
                    "DELETE FROM reindex_jobs WHERE tenant=? AND collection=?",
                    (tenant, collection),
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return [str(row[0]) for row in rows]

    def move_reindex_jobs_for_collection(
        self,
        tenant: str,
        old: str,
        new: str,
    ) -> list[str]:
        """Carry reindex jobs to a renamed collection.

        A rename keeps the chunk corpus and the vector-space config, so a
        paused job stays valid under the new slug. Returns the ids of any
        destination rows dropped first: the caller has already established
        that `new` has no collection, so a job row there is stale state
        that would otherwise collide with the active-job index.
        """
        with self._writer() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    "SELECT job_id FROM reindex_jobs "
                    "WHERE tenant=? AND collection=?",
                    (tenant, new),
                ).fetchall()
                conn.execute(
                    "DELETE FROM reindex_jobs WHERE tenant=? AND collection=?",
                    (tenant, new),
                )
                conn.execute(
                    "UPDATE reindex_jobs SET collection=? "
                    "WHERE tenant=? AND collection=?",
                    (new, tenant, old),
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return [str(row[0]) for row in rows]

    @staticmethod
    def _rate_bucket_start(timestamp: float, window_seconds: int) -> int:
        return int(timestamp // window_seconds) * window_seconds

    @staticmethod
    def _rate_bucket_key(timestamp: int) -> str:
        return datetime.fromtimestamp(timestamp, tz.utc).isoformat(
            timespec="seconds"
        )

    @staticmethod
    def _rate_retry_after(
        *,
        current: int,
        previous: int,
        limit: int,
        elapsed: float,
        window_seconds: int,
    ) -> int:
        remaining_window = window_seconds - elapsed
        if previous > 0 and current < limit:
            target_weight = (limit - current) / previous
            wait = remaining_window - (target_weight * window_seconds)
            return max(1, math.ceil(wait + 1e-9))
        # A full current bucket begins decaying at the next boundary.
        return max(1, math.ceil(remaining_window))

    def charge_rate_limit(
        self,
        tenant: str,
        operation: str,
        limits: dict[int, int],
        *,
        now: float | None = None,
    ) -> RateLimitDecision:
        """Atomically charge one request against moving time windows.

        Each configured window uses the current and previous fixed SQLite
        buckets. The previous bucket is weighted by the fraction of the
        current window that remains, which avoids the boundary burst allowed
        by a plain fixed-window counter while keeping persistent state small.
        A rejected request is not charged against any window.
        """
        enabled = sorted(
            (int(window), int(limit))
            for window, limit in limits.items()
            if int(window) > 0 and int(limit) > 0
        )
        if not enabled:
            return RateLimitDecision(allowed=True)

        timestamp = time.time() if now is None else float(now)
        if not math.isfinite(timestamp):
            raise ValueError("rate-limit timestamp must be finite")

        states: list[dict[str, int | float | str]] = []
        with self._writer() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for window_seconds, limit in enabled:
                    bucket_operation = f"{operation}:{window_seconds}"
                    start = self._rate_bucket_start(timestamp, window_seconds)
                    previous_start = start - window_seconds
                    start_key = self._rate_bucket_key(start)
                    previous_key = self._rate_bucket_key(previous_start)
                    conn.execute(
                        "DELETE FROM rate_limit_buckets "
                        "WHERE tenant=? AND operation=? AND window_start<?",
                        (tenant, bucket_operation, previous_key),
                    )
                    rows = dict(
                        conn.execute(
                            "SELECT window_start, count "
                            "FROM rate_limit_buckets "
                            "WHERE tenant=? AND operation=? "
                            "AND window_start IN (?, ?)",
                            (
                                tenant,
                                bucket_operation,
                                previous_key,
                                start_key,
                            ),
                        ).fetchall()
                    )
                    current = int(rows.get(start_key, 0))
                    previous = int(rows.get(previous_key, 0))
                    elapsed = timestamp - start
                    weighted = current + previous * (
                        (window_seconds - elapsed) / window_seconds
                    )
                    remaining = max(
                        0,
                        math.floor(limit - (weighted + 1)),
                    )
                    states.append(
                        {
                            "window": window_seconds,
                            "limit": limit,
                            "operation": bucket_operation,
                            "start": start_key,
                            "current": current,
                            "previous": previous,
                            "elapsed": elapsed,
                            "weighted": weighted,
                            "remaining": remaining,
                        }
                    )

                rejected = [
                    state
                    for state in states
                    if float(state["weighted"]) >= int(state["limit"])
                ]
                if rejected:
                    decisions = [
                        (
                            self._rate_retry_after(
                                current=int(state["current"]),
                                previous=int(state["previous"]),
                                limit=int(state["limit"]),
                                elapsed=float(state["elapsed"]),
                                window_seconds=int(state["window"]),
                            ),
                            state,
                        )
                        for state in rejected
                    ]
                    retry_after, binding = max(
                        decisions,
                        key=lambda item: item[0],
                    )
                    conn.rollback()
                    return RateLimitDecision(
                        allowed=False,
                        limit=int(binding["limit"]),
                        remaining=0,
                        retry_after=retry_after,
                        window_seconds=int(binding["window"]),
                    )

                for state in states:
                    conn.execute(
                        """
                        INSERT INTO rate_limit_buckets (
                            tenant, operation, window_start, count
                        ) VALUES (?, ?, ?, 1)
                        ON CONFLICT(tenant, operation, window_start) DO UPDATE
                        SET count=rate_limit_buckets.count + 1
                        """,
                        (tenant, state["operation"], state["start"]),
                    )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

        binding = min(
            states,
            key=lambda state: (
                int(state["remaining"]) / int(state["limit"]),
                int(state["window"]),
            ),
        )
        return RateLimitDecision(
            allowed=True,
            limit=int(binding["limit"]),
            remaining=int(binding["remaining"]),
            retry_after=0,
            window_seconds=int(binding["window"]),
        )

    def metrics_generation(self) -> str:
        with self._reader() as conn:
            row = conn.execute(
                "SELECT generation FROM metrics_state WHERE singleton=1"
            ).fetchone()
        if row is None:
            raise RuntimeError("metrics state is missing")
        return str(row[0])

    def read_metrics(self) -> dict[str, Any]:
        with self._reader() as conn:
            conn.execute("BEGIN")
            try:
                state = conn.execute(
                    "SELECT generation, last_error, last_error_at "
                    "FROM metrics_state WHERE singleton=1"
                ).fetchone()
                counters = conn.execute(
                    "SELECT name, labels_json, value "
                    "FROM metrics_counters ORDER BY name, labels_json"
                ).fetchall()
                samples = conn.execute(
                    "SELECT operation, value FROM metrics_latency_samples "
                    "ORDER BY id"
                ).fetchall()
            finally:
                conn.execute("ROLLBACK")

        if state is None:
            raise RuntimeError("metrics state is missing")
        metrics: dict[str, list[dict[str, Any]]] = {}
        for name, labels_json, value in counters:
            labels = self._load_json(str(labels_json))
            metrics.setdefault(str(name), []).append(
                {"labels": labels, "value": float(value)}
            )
        latencies: dict[str, list[float]] = {}
        for operation, value in samples:
            latencies.setdefault(str(operation), []).append(float(value))
        return {
            "generation": str(state[0]),
            "metrics": metrics,
            "last_error": state[1],
            "last_error_at": state[2],
            "latencies": latencies,
        }

    def flush_metrics_delta(
        self,
        generation: str,
        counters: list[tuple[str, str, float]],
        latencies: dict[str, list[float]],
        last_error: tuple[str, float] | None,
        sample_limit: int,
    ) -> tuple[str, bool]:
        """Add one worker's deltas if its generation is still current."""
        with self._writer() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT generation FROM metrics_state "
                    "WHERE singleton=1"
                ).fetchone()
                if row is None:
                    raise RuntimeError("metrics state is missing")
                current = str(row[0])
                if current != generation:
                    conn.rollback()
                    return current, False

                conn.executemany(
                    """
                    INSERT INTO metrics_counters (name, labels_json, value)
                    VALUES (?, ?, ?)
                    ON CONFLICT(name, labels_json) DO UPDATE
                    SET value=metrics_counters.value + excluded.value
                    """,
                    counters,
                )
                for operation, values in latencies.items():
                    conn.executemany(
                        "INSERT INTO metrics_latency_samples "
                        "(operation, value) VALUES (?, ?)",
                        ((operation, value) for value in values),
                    )
                    conn.execute(
                        """
                        DELETE FROM metrics_latency_samples
                        WHERE operation=? AND id NOT IN (
                            SELECT id FROM metrics_latency_samples
                            WHERE operation=? ORDER BY id DESC LIMIT ?
                        )
                        """,
                        (operation, operation, sample_limit),
                    )
                if last_error is not None:
                    message, changed_at = last_error
                    conn.execute(
                        """
                        UPDATE metrics_state
                        SET last_error=?, last_error_at=?
                        WHERE singleton=1 AND (
                            last_error_at IS NULL OR last_error_at <= ?
                        )
                        """,
                        (message, changed_at, changed_at),
                    )
                conn.commit()
                return current, True
            except BaseException:
                conn.rollback()
                raise

    def rotate_metrics_generation(self) -> str:
        with self._writer() as conn, conn:
            conn.execute(
                "UPDATE metrics_state "
                "SET generation=lower(hex(randomblob(16))) "
                "WHERE singleton=1"
            )
            row = conn.execute(
                "SELECT generation FROM metrics_state WHERE singleton=1"
            ).fetchone()
        if row is None:
            raise RuntimeError("metrics state is missing")
        return str(row[0])

    def reset_metrics(self) -> str:
        with self._writer() as conn, conn:
            conn.execute(
                "UPDATE metrics_state SET "
                "generation=lower(hex(randomblob(16))), "
                "last_error=NULL, last_error_at=NULL "
                "WHERE singleton=1"
            )
            conn.execute("DELETE FROM metrics_counters")
            conn.execute("DELETE FROM metrics_latency_samples")
            row = conn.execute(
                "SELECT generation FROM metrics_state WHERE singleton=1"
            ).fetchone()
        if row is None:
            raise RuntimeError("metrics state is missing")
        return str(row[0])

    @staticmethod
    def _recovery_config(collection_dir: Path) -> dict[str, Any] | None:
        path = collection_dir / COLLECTION_CONFIG_FILENAME
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise CollectionConfigRecoveryError(
                f"catalog recovery requires collection config: {path}"
            ) from exc
        if not isinstance(config, dict):
            raise CollectionConfigRecoveryError(
                f"catalog recovery requires collection config: {path}"
            )
        try:
            return validate_collection_config(config)
        except ValueError as exc:
            raise CollectionConfigRecoveryError(
                f"catalog recovery requires collection config: {path}"
            ) from exc

    @staticmethod
    def discover_collection_dirs(
        data_dir: Path,
        *,
        require_metadata: bool = True,
    ) -> dict[tuple[str, str], Path]:
        """Return collection directories, optionally requiring a metadata DB."""
        discovered: dict[tuple[str, str], Path] = {}
        if not data_dir.is_dir():
            return discovered
        for tenant_dir in data_dir.iterdir():
            if not tenant_dir.is_dir() or not tenant_dir.name.startswith("t_"):
                continue
            tenant = tenant_dir.name[2:]
            if not tenant or tenant == "_system":
                continue
            for coll_dir in tenant_dir.iterdir():
                if not coll_dir.is_dir() or not coll_dir.name.startswith("c_"):
                    continue
                name = coll_dir.name[2:]
                if name and (
                    not require_metadata or (coll_dir / "meta.db").is_file()
                ):
                    discovered[(tenant, name)] = coll_dir
        return discovered

    def bootstrap(self, data_dir: Path) -> tuple[int, int]:
        data_dir = data_dir.resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        discovered = self.discover_collection_dirs(data_dir)

        seeded = 0
        removed = 0
        with self._writer() as conn, conn:
            rows = conn.execute(
                "SELECT tenant, name FROM collections"
            ).fetchall()
            existing = {(str(row[0]), str(row[1])) for row in rows}
            to_seed = set(discovered) - existing
            if to_seed:
                payload_rows = []
                for tenant, name in sorted(to_seed):
                    config = (
                        self._recovery_config(discovered[(tenant, name)]) or {}
                    )
                    payload_rows.append(
                        (
                            tenant,
                            name,
                            None,
                            None,
                            config.get("backend_type"),
                            self._dump_json(config.get("backend_config")),
                            config.get("embedder_type"),
                            config.get("embed_model"),
                            self._dump_json(config.get("embedder_config")),
                            # Discovered from an existing data dir; the
                            # creation-time version is unknown, so tagging
                            # (P2-25) leaves it unset rather than guessing.
                            None,
                            None,
                        )
                    )
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO collections (
                        tenant,
                        name,
                        display_name,
                        meta_json,
                        backend_type,
                        backend_config_json,
                        embedder_type,
                        embed_model,
                        embed_config_json,
                        pavedb_version,
                        collection_schema_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    payload_rows,
                )
                seeded = len(payload_rows)

            orphans = existing - set(discovered)
            if orphans:
                conn.executemany(
                    "DELETE FROM collections WHERE tenant=? AND name=?",
                    sorted(orphans),
                )
                removed = len(orphans)
        return (seeded, removed)

    def register_collection(
        self,
        tenant: str,
        name: str,
        *,
        display_name: str | None = None,
        meta: dict[str, Any] | None = None,
        backend_type: str | None = None,
        backend_config: dict[str, Any] | None = None,
        embedder_type: str | None = None,
        embed_model: str | None = None,
        embed_config: dict[str, Any] | None = None,
        pavedb_version: str | None = None,
        collection_schema_version: int | None = None,
        max_collections: int = 0,
    ) -> None:
        with self._writer() as conn, conn:
            # Count and insert share the writer lock and transaction, so the
            # limit cannot be raced past by concurrent creates.
            if max_collections > 0:
                exists = conn.execute(
                    "SELECT 1 FROM collections WHERE tenant=? AND name=?",
                    (tenant, name),
                ).fetchone()
                if exists is None:
                    (count,) = conn.execute(
                        "SELECT COUNT(*) FROM collections WHERE tenant=?",
                        (tenant,),
                    ).fetchone()
                    if count >= max_collections:
                        raise CollectionLimitError(
                            f"tenant '{tenant}' is at its collection limit "
                            f"({max_collections})"
                        )
            conn.execute(
                """
                INSERT OR IGNORE INTO collections (
                    tenant,
                    name,
                    display_name,
                    meta_json,
                    backend_type,
                    backend_config_json,
                    embedder_type,
                    embed_model,
                    embed_config_json,
                    pavedb_version,
                    collection_schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tenant,
                    name,
                    display_name,
                    self._dump_json(meta),
                    backend_type,
                    self._dump_json(backend_config),
                    embedder_type,
                    embed_model,
                    self._dump_json(embed_config),
                    pavedb_version,
                    collection_schema_version,
                ),
            )

    def restore_collection(
        self,
        tenant: str,
        name: str,
        *,
        display_name: str | None,
        meta: dict[str, Any],
        backend_type: str,
        backend_config: dict[str, Any],
        embedder_type: str,
        embed_model: str,
        embed_config: dict[str, Any],
        created_at: str | None,
        pavedb_version: str | None,
        collection_schema_version: int | None,
        query_homes: list[tuple[str, str]],
    ) -> None:
        """Replace one catalog row and its query-home index atomically."""
        restored_at = created_at or datetime.now(tz.utc).isoformat(
            timespec="seconds"
        )
        with self._writer() as conn, conn:
            conn.execute(
                """
                INSERT INTO collections (
                    tenant,
                    name,
                    display_name,
                    meta_json,
                    backend_type,
                    backend_config_json,
                    embedder_type,
                    embed_model,
                    embed_config_json,
                    created_at,
                    pavedb_version,
                    collection_schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant, name) DO UPDATE SET
                    display_name=excluded.display_name,
                    meta_json=excluded.meta_json,
                    backend_type=excluded.backend_type,
                    backend_config_json=excluded.backend_config_json,
                    embedder_type=excluded.embedder_type,
                    embed_model=excluded.embed_model,
                    embed_config_json=excluded.embed_config_json,
                    created_at=excluded.created_at,
                    pavedb_version=excluded.pavedb_version,
                    collection_schema_version=excluded.collection_schema_version
                """,
                (
                    tenant,
                    name,
                    display_name,
                    self._dump_json(meta),
                    backend_type,
                    self._dump_json(backend_config),
                    embedder_type,
                    embed_model,
                    self._dump_json(embed_config),
                    restored_at,
                    pavedb_version,
                    collection_schema_version,
                ),
            )
            conn.execute(
                "DELETE FROM query_home WHERE tenant=? AND collection=?",
                (tenant, name),
            )
            conn.executemany(
                """
                INSERT OR IGNORE INTO query_home (
                    query_id,
                    tenant,
                    collection,
                    created_at
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (query_id, tenant, name, executed_at)
                    for query_id, executed_at in query_homes
                ],
            )

    def unregister_collection(self, tenant: str, name: str) -> None:
        with self._writer() as conn, conn:
            conn.execute(
                "DELETE FROM collections WHERE tenant=? AND name=?",
                (tenant, name),
            )

    def update_collection(
        self,
        tenant: str,
        name: str,
        *,
        display_name: str | None = None,
    ) -> None:
        with self._writer() as conn, conn:
            cur = conn.execute(
                """
                UPDATE collections
                SET display_name=?
                WHERE tenant=? AND name=?
                """,
                (display_name, tenant, name),
            )
            if cur.rowcount <= 0:
                raise ValueError(
                    f"catalog row missing for collection '{tenant}/{name}'"
                )

    def update_collection_embedder(
        self,
        tenant: str,
        name: str,
        *,
        embedder_type: str,
        embed_model: str,
        embed_config: dict[str, Any],
    ) -> None:
        """Replace a collection's vector-space identity after reindex."""
        with self._writer() as conn, conn:
            cur = conn.execute(
                """
                UPDATE collections
                SET embedder_type=?, embed_model=?, embed_config_json=?
                WHERE tenant=? AND name=?
                """,
                (
                    embedder_type,
                    embed_model,
                    self._dump_json(embed_config),
                    tenant,
                    name,
                ),
            )
            if cur.rowcount <= 0:
                raise ValueError(
                    f"catalog row missing for collection '{tenant}/{name}'"
                )

    def move_collection(self, tenant: str, old: str, new: str) -> None:
        with self._writer() as conn, conn:
            cur = conn.execute(
                "UPDATE collections SET name=? WHERE tenant=? AND name=?",
                (new, tenant, old),
            )
            if cur.rowcount <= 0:
                existing = conn.execute(
                    """
                    SELECT 1
                    FROM collections
                    WHERE tenant=? AND name=?
                    """,
                    (tenant, new),
                ).fetchone()
                if existing is not None:
                    # Retry-safe: the rename may already have been applied on
                    # a prior catalog handle before a transient close.
                    return
                raise ValueError(
                    f"catalog row missing for collection '{tenant}/{old}'"
                )

    def put_query_home(
        self,
        query_id: str,
        tenant: str,
        collection: str,
    ) -> None:
        with self._writer() as conn, conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO query_home (
                    query_id,
                    tenant,
                    collection
                ) VALUES (?, ?, ?)
                """,
                (query_id, tenant, collection),
            )

    def resolve_query_home(
        self,
        query_id: str,
    ) -> tuple[str, str] | None:
        with self._reader() as conn:
            row = conn.execute(
                """
                SELECT tenant, collection
                FROM query_home
                WHERE query_id=?
                """,
                (query_id,),
            ).fetchone()
        if row is None:
            return None
        return (str(row[0]), str(row[1]))

    def purge_query_homes_for_collection(
        self,
        tenant: str,
        collection: str,
    ) -> None:
        with self._writer() as conn, conn:
            conn.execute(
                """
                DELETE FROM query_home
                WHERE tenant=? AND collection=?
                """,
                (tenant, collection),
            )

    def move_query_homes_for_collection(
        self,
        tenant: str,
        old: str,
        new: str,
    ) -> None:
        with self._writer() as conn, conn:
            conn.execute(
                """
                UPDATE query_home
                SET collection=?
                WHERE tenant=? AND collection=?
                """,
                (new, tenant, old),
            )

    def list_query_homes(
        self,
        tenant: str | None = None,
        collection: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if tenant is not None:
            where.append("tenant=?")
            params.append(tenant)
        if collection is not None:
            where.append("collection=?")
            params.append(collection)
        query = (
            "SELECT query_id, tenant, collection, created_at "
            "FROM query_home"
        )
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._reader() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "query_id": str(row[0]),
                "tenant": str(row[1]),
                "collection": str(row[2]),
                "created_at": str(row[3]),
            }
            for row in rows
        ]

    def list_tenants(self) -> list[str]:
        with self._reader() as conn:
            cur = conn.execute(
                "SELECT DISTINCT tenant FROM collections ORDER BY tenant"
            )
            return [str(row[0]) for row in cur.fetchall()]

    def list_collections(self, tenant: str) -> list[str]:
        with self._reader() as conn:
            cur = conn.execute(
                "SELECT name FROM collections WHERE tenant=? ORDER BY name",
                (tenant,),
            )
            return [str(row[0]) for row in cur.fetchall()]

    def list_collection_summaries(
        self,
        tenant: str,
    ) -> list[dict[str, Any]]:
        with self._reader() as conn:
            cur = conn.execute(
                "SELECT name, display_name, embedder_type, embed_model, "
                "embed_config_json "
                "FROM collections WHERE tenant=? ORDER BY name",
                (tenant,),
            )
            out: list[dict[str, Any]] = []
            for name, display_name, emb_type, emb_model, emb_cfg_json in (
                cur.fetchall()
            ):
                label = None
                if emb_type and emb_model:
                    label = f"{emb_type}:{emb_model}"
                try:
                    emb_cfg = json.loads(emb_cfg_json) if emb_cfg_json else {}
                except (TypeError, ValueError):
                    emb_cfg = {}
                out.append(
                    {
                        "name": name,
                        "display_name": display_name,
                        "embedder_type": emb_type,
                        "embed_model": emb_model,
                        "embedder_config": emb_cfg,
                        "embedder_label": label,
                    }
                )
            return out

    def list_collection_refs(self) -> list[tuple[str, str]]:
        with self._reader() as conn:
            cur = conn.execute(
                "SELECT tenant, name FROM collections ORDER BY tenant, name"
            )
            return [
                (str(row[0]), str(row[1]))
                for row in cur.fetchall()
            ]

    def get_collection_config(
        self,
        tenant: str,
        name: str,
    ) -> dict[str, Any] | None:
        with self._reader() as conn:
            cur = conn.execute(
                """
                SELECT
                    display_name,
                    meta_json,
                    backend_type,
                    backend_config_json,
                    embedder_type,
                    embed_model,
                    embed_config_json,
                    created_at,
                    pavedb_version,
                    collection_schema_version
                FROM collections
                WHERE tenant=? AND name=?
                """,
                (tenant, name),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return {
                "display_name": row[0],
                "meta": self._load_json(row[1]),
                "backend_type": row[2],
                "backend_config": self._load_json(row[3]),
                "embedder_type": row[4],
                "embed_model": row[5],
                "embedder_config": self._load_json(row[6]),
                "created_at": row[7],
                "pavedb_version": row[8],
                "collection_schema_version": row[9],
            }

    def collection_count(self) -> int:
        with self._reader() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM collections")
            row = cur.fetchone()
            return int(row[0] or 0) if row is not None else 0

    def tenant_count(self) -> int:
        with self._reader() as conn:
            cur = conn.execute("SELECT COUNT(DISTINCT tenant) FROM collections")
            row = cur.fetchone()
            return int(row[0] or 0) if row is not None else 0
