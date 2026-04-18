# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
CollectionDB — impl2: read/write split connections.

Two persistent connections (check_same_thread=False):
  _rconn: read connection (WAL, no lock, concurrent reads)
  _wconn: write connection (serialised by _write_lock)
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone as tz
from pathlib import Path
from typing import Any, Iterator

from pave.filters import sanit_sql


class LegacyMetadataError(RuntimeError):
    pass


_COLLECTION_MIGRATIONS: dict[int, list[str]] = {
    1: [
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """,
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
    ],
    2: [
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
    ],
    3: [
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
    ],
}

_CATALOG_MIGRATIONS: dict[int, list[str]] = {
    1: [
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """,
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
    ],
}


class CollectionDB:
    """Per-collection SQLite metadata store (impl2).

    Two persistent connections with check_same_thread=False:
      _rconn: used for all reads (WAL, no lock, fully concurrent)
      _wconn: used for all writes (protected by _write_lock)
    """

    def __init__(self) -> None:
        self.path: Path | None = None
        self._rconn: sqlite3.Connection | None = None
        self._wconn: sqlite3.Connection | None = None
        self._write_lock = threading.Lock()
        self._state_cv = threading.Condition()
        self._active_readers = 0
        self._closing = False

    def _open_conn(
        self,
        path: Path,
        *,
        read_only: bool = False,
    ) -> sqlite3.Connection:
        """Open a single sqlite3 connection with standard pragmas."""
        if read_only:
            conn = sqlite3.connect(
                f"file:{path.as_posix()}?mode=ro",
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

    def open(self, path: Path, *, read_only: bool = False) -> None:
        """Open (or create) the meta.db at *path*.

        When *read_only* is True only the read connection is opened
        and migrations are skipped.  Use this for fallback reads
        (``has_doc``, ``catalog_metrics``, ``_read_meta_batch_safe``)
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
            )
            if not read_only:
                self._wconn = self._open_conn(path)
            self._active_readers = 0
            self._closing = False
        if not read_only:
            self._apply_migrations()

    def close(self) -> None:
        with self._state_cv:
            if self._rconn is None and self._wconn is None:
                self._closing = False
                return
            self._closing = True
            while self._active_readers > 0:
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
        try:
            yield conn
        finally:
            with self._state_cv:
                if self._active_readers > 0:
                    self._active_readers -= 1
                if self._closing and self._active_readers == 0:
                    self._state_cv.notify_all()

    def _apply_migrations(self) -> None:
        conn = self._require_wconn()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        cur = conn.execute("SELECT MAX(version) FROM schema_migrations")
        row = cur.fetchone()
        current = int(row[0] or 0)
        for version in sorted(_COLLECTION_MIGRATIONS):
            if version <= current:
                continue
            for stmt in _COLLECTION_MIGRATIONS[version]:
                conn.execute(stmt)
            now = datetime.now(tz.utc).isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) "
                "VALUES (?, ?)",
                (version, now),
            )
        conn.commit()

    # ------------------------------------------------------------------
    # Write operations — serialised by _write_lock, use _wconn
    # ------------------------------------------------------------------

    def upsert_chunks(
        self,
        docid: str,
        chunks: list[tuple[str, str | None, dict[str, Any]]],
        doc_meta: dict[str, Any] | None = None,
    ) -> None:
        """Insert/replace chunk rows and upsert the document row.

        All writes happen in a single transaction.
        Must be called inside collection_lock.
        """
        conn = self._require_wconn()
        doc_meta_dict = doc_meta or {}
        doc_meta_json = json.dumps(doc_meta_dict, ensure_ascii=False)
        now = datetime.now(tz.utc).isoformat(timespec="seconds")
        rows = []
        for rid, chunk_path, meta in chunks:
            meta_json = json.dumps(meta, ensure_ascii=False)
            rows.append((docid, rid, chunk_path, meta_json, now))
        with self._write_lock, conn:
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
            doc_kv_rows = [
                (docid, str(mk), str(mv))
                for mk, mv in doc_meta_dict.items()
            ]
            if doc_kv_rows:
                conn.executemany(
                    "INSERT INTO document_meta "
                    "(docid, key, value) VALUES (?, ?, ?)",
                    doc_kv_rows,
                )
            conn.executemany(
                """
                INSERT OR REPLACE INTO chunks
                (docid, rid, chunk_path, meta_json, ingested_at)
                VALUES (?, ?, ?, ?, ?)
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
                for mk, mv in meta.items():
                    kv_rows.append((rid, str(mk), str(mv)))
            if kv_rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO chunk_meta "
                    "(rid, key, value) VALUES (?, ?, ?)",
                    kv_rows,
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
        conn = self._require_wconn()
        with self._write_lock, conn:
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

    # ------------------------------------------------------------------
    # Read operations — use _rconn, no lock needed (WAL)
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
            if isinstance(ingested_at, str) and ingested_at.endswith("+00:00"):
                ingested_at = ingested_at.replace("+00:00", "Z")
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
                if isinstance(ingested_at, str) and ingested_at.endswith("+00:00"):
                    ingested_at = ingested_at.replace("+00:00", "Z")
                docs.append(
                    {
                        "docid": docid,
                        "version": int(version or 0),
                        "ingested_at": ingested_at,
                        "chunk_count": int(chunk_count or 0),
                    }
                )
            return docs

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

        Handles exact-match and negation (!value) only.
        Values with *, >, <, >=, <= are skipped (left for
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
        skip_prefixes = (">=", "<=", "!=", ">", "<")

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
                    if value.startswith("!") and len(value) > 1:
                        neg_val = value[1:]
                        if key == "docid":
                            matched = self._docid_matches(
                                conn,
                                current_batch,
                                neg_val,
                            )
                        else:
                            matched = self._chunk_meta_matches(
                                conn,
                                current_batch,
                                key,
                                neg_val,
                            )
                            matched.update(
                                self._document_meta_matches(
                                    conn,
                                    current_batch,
                                    key,
                                    neg_val,
                                )
                            )
                        key_matches.update(current - matched)
                        continue

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

        Called OUTSIDE collection_lock — WAL reads via _rconn are concurrent.
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


class CatalogDB:
    """Global SQLite catalog for collection listing and config."""

    def __init__(self) -> None:
        self.path: Path | None = None
        self._rconn: sqlite3.Connection | None = None
        self._wconn: sqlite3.Connection | None = None
        self._write_lock = threading.Lock()
        self._state_cv = threading.Condition()
        self._active_readers = 0
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
            self._closing = False
        self._apply_migrations()

    def close(self) -> None:
        with self._state_cv:
            if self._rconn is None and self._wconn is None:
                self._closing = False
                return
            self._closing = True
            while self._active_readers > 0:
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
        try:
            yield conn
        finally:
            with self._state_cv:
                if self._active_readers > 0:
                    self._active_readers -= 1
                if self._closing and self._active_readers == 0:
                    self._state_cv.notify_all()

    def _apply_migrations(self) -> None:
        conn = self._require_wconn()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        cur = conn.execute("SELECT MAX(version) FROM schema_migrations")
        row = cur.fetchone()
        current = int(row[0] or 0)
        for version in sorted(_CATALOG_MIGRATIONS):
            if version <= current:
                continue
            for stmt in _CATALOG_MIGRATIONS[version]:
                conn.execute(stmt)
            now = datetime.now(tz.utc).isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) "
                "VALUES (?, ?)",
                (version, now),
            )
        conn.commit()

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

    def bootstrap(
        self,
        data_dir: Path,
        *,
        display_name: str | None = None,
        meta: dict[str, Any] | None = None,
        backend_type: str | None = None,
        backend_config: dict[str, Any] | None = None,
        embedder_type: str | None = None,
        embed_model: str | None = None,
        embed_config: dict[str, Any] | None = None,
    ) -> tuple[int, int]:
        data_dir = data_dir.resolve()
        data_dir.mkdir(parents=True, exist_ok=True)

        discovered: set[tuple[str, str]] = set()
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
                if not name:
                    continue
                if (coll_dir / "meta.db").is_file():
                    discovered.add((tenant, name))

        conn = self._require_wconn()
        seeded = 0
        removed = 0
        with self._write_lock, conn:
            rows = conn.execute(
                "SELECT tenant, name FROM collections"
            ).fetchall()
            existing = {(str(row[0]), str(row[1])) for row in rows}
            to_seed = discovered - existing
            if to_seed:
                payload_rows = [
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
                    )
                    for tenant, name in sorted(to_seed)
                ]
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
                        embed_config_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    payload_rows,
                )
                seeded = len(payload_rows)

            orphans = existing - discovered
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
    ) -> None:
        conn = self._require_wconn()
        with self._write_lock, conn:
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
                    embed_config_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )

    def unregister_collection(self, tenant: str, name: str) -> None:
        conn = self._require_wconn()
        with self._write_lock, conn:
            conn.execute(
                "DELETE FROM collections WHERE tenant=? AND name=?",
                (tenant, name),
            )

    def rename_collection(self, tenant: str, old: str, new: str) -> None:
        conn = self._require_wconn()
        with self._write_lock, conn:
            cur = conn.execute(
                "UPDATE collections SET name=? WHERE tenant=? AND name=?",
                (new, tenant, old),
            )
            if cur.rowcount <= 0:
                raise ValueError(
                    f"catalog row missing for collection '{tenant}/{old}'"
                )

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
                "SELECT name, display_name, embedder_type, embed_model "
                "FROM collections WHERE tenant=? ORDER BY name",
                (tenant,),
            )
            out: list[dict[str, Any]] = []
            for name, display_name, emb_type, emb_model in cur.fetchall():
                label = None
                if emb_type and emb_model:
                    label = f"{emb_type}:{emb_model}"
                out.append(
                    {
                        "name": name,
                        "display_name": display_name,
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
                    created_at
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
