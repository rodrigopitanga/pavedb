# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import hashlib
import json
import os
import re
import shutil
from collections.abc import Iterable
from typing import Any

import numpy as np

from pave.stores.base import BaseStore, Record, SearchOutput, SearchResult
from pave.config import get_cfg


class FakeEmbeddings:
    """Tiny in-memory index. Keeps interface you use in tests."""
    def __init__(self, config, **kwargs):  # config/kwargs unused
        self._docs = {}  # rid -> {"text": str, "meta_json": str, "meta": dict}
        self.last_sql = None

    def index(self, docs):
        for rid, payload, meta_json in docs:
            assert isinstance(meta_json, str)
            if isinstance(payload, dict):
                text = payload.get("text")
                meta = {k: v for k, v in payload.items() if k != "text"}
            else:
                text = payload
                meta = {}
            self._docs[rid] = {"text": text, "meta_json": meta_json, "meta": meta}

    def upsert(self, docs):
        return self.index(docs)

    def search(self, sql, k=5):
        import re
        self.last_sql = sql
        term = None
        m = re.search(r"similar\('([^']+)'", sql)
        if m:
            term = m.group(1).lower()
        elif "SELECT" not in sql.upper():
            term = sql.lower()

        if not term:
            return []

        filter_pairs = re.findall(r"\[([^\]]+)\]\s*=\s*'((?:''|[^'])*)'", sql)

        hits = []
        for rid, entry in self._docs.items():
            text = entry.get("text")
            if text is None:
                continue
            if term not in str(text).lower():
                continue

            metadata = entry.get("meta") or {}
            include = True
            for field, raw_val in filter_pairs:
                stored = metadata.get(field)
                if stored is None:
                    include = False
                    break
                expected = raw_val
                if isinstance(stored, (list, tuple, set)):
                    options = {str(v) for v in stored}
                    if expected not in options:
                        include = False
                        break
                else:
                    if str(stored) != expected:
                        include = False
                        break
            if not include:
                continue

            hits.append({
                "id": rid,
                "score": 1.0,
                "text": text,
                "docid": metadata.get("docid"),
            })
        return hits[:10]
        """
        q = (query or "").lower()
        out = []
        for rid, (text, _) in self._docs.items():
            if q in (text or "").lower():
                out.append({"id": rid, "score": float(len(q)), "text": text})
        return out[:k]
        """

    def lookup(self, ids):
        return {rid: (self._docs.get(rid) or {}).get("text") for rid in ids}

    def delete(self, ids):
        for rid in ids:
            self._docs.pop(rid, None)

    def save(self, path):
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(
                path, "_fake_index.json"), "w", encoding="utf-8") as f:
            json.dump(self._docs, f, ensure_ascii=False)

    def load(self, path):
        p = os.path.join(path, "_fake_index.json")
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                self._docs = json.load(f)


class FakeEmbedder:
    def __init__(self, dim: int = 64):
        self._dim = int(dim)

    @property
    def dim(self) -> int:
        return self._dim

    def _encode_one(self, text: str) -> np.ndarray:
        vec = np.zeros(self._dim, dtype=np.float32)
        tokens = re.findall(r"\w+", (text or "").lower())
        if not tokens:
            vec[0] = 1.0
            return vec
        for tok in tokens:
            digest = hashlib.blake2b(
                tok.encode("utf-8"),
                digest_size=8,
            ).digest()
            idx = int.from_bytes(digest[:4], "little") % self._dim
            sign = 1.0 if (digest[4] & 1) == 0 else -1.0
            vec[idx] += sign
        norm = float(np.linalg.norm(vec))
        if norm > 0.0:
            vec /= norm
        return vec

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self._dim), dtype=np.float32)
        rows = [self._encode_one(text) for text in texts]
        return np.stack(rows).astype(np.float32, copy=False)


class DummyStore(BaseStore):
    def _dir(self, tenant: str, collection: str) -> str:
        return os.path.join(get_cfg().get("data_dir"), tenant, collection)

    def _load_or_init(self, tenant: str, collection: str) -> None:
        os.makedirs(os.path.join(self._dir(tenant, collection), "index"), exist_ok=True)

    def _save(self, tenant: str, collection: str) -> None:
        cat = os.path.join(self._dir(tenant, collection), "catalog.json")
        if not os.path.isfile(cat):
            with open(cat, "w", encoding="utf-8") as f:
                json.dump({}, f)

    def create_collection(self, tenant: str, name: str) -> None:
        self._load_or_init(tenant, name)
        self._save(tenant, name)

    def delete_collection(self, tenant: str, collection: str) -> None:
        shutil.rmtree(self._dir(tenant, collection), ignore_errors=True)

    def rename_collection(self, tenant: str, old_name: str, new_name: str) -> None:
        if old_name == new_name:
            raise ValueError(f"old and new collection names are the same: {old_name}")
        old_path = self._dir(tenant, old_name)
        new_path = self._dir(tenant, new_name)
        if not os.path.isdir(old_path):
            raise ValueError(f"collection '{old_name}' does not exist")
        if os.path.exists(new_path):
            raise ValueError(f"collection '{new_name}' already exists")
        os.rename(old_path, new_path)

    def list_collections(self, tenant: str) -> list[dict[str, Any]]:
        tenant_path = os.path.join(get_cfg().get("data_dir"), tenant)
        if not os.path.isdir(tenant_path):
            return []
        collections: list[dict[str, Any]] = []
        for entry in sorted(os.listdir(tenant_path)):
            entry_path = os.path.join(tenant_path, entry)
            if os.path.isdir(entry_path):
                catalog_path = os.path.join(entry_path, "catalog.json")
                if os.path.isfile(catalog_path):
                    collections.append({"name": entry})
        return collections

    def list_tenants(self) -> list[str]:
        from pathlib import Path
        data_dir_path = Path(get_cfg().get("data_dir")).resolve()
        if not data_dir_path.is_dir():
            return []
        tenants: list[str] = []
        for entry in data_dir_path.iterdir():
            if not entry.is_dir():
                continue
            name = entry.name
            if not name.startswith("t_"):
                continue
            tenant = name[2:]
            if tenant:
                tenants.append(tenant)
        return tenants

    def get_collection_detail(
        self,
        tenant: str,
        name: str,
    ) -> dict[str, Any] | None:
        coll_path = os.path.join(self._dir(tenant, name), "catalog.json")
        if not os.path.isfile(coll_path):
            return None
        try:
            data = json.load(open(coll_path, "r", encoding="utf-8"))
        except Exception:
            data = {}
        cfg = get_cfg()
        embedder_type = str(cfg.get("embedder.type")).lower()
        embed_model = cfg.get(f"embedder.{embedder_type}.model")
        return {
            "tenant": tenant,
            "name": name,
            "display_name": None,
            "embedder_type": embedder_type,
            "embed_model": str(embed_model) if embed_model else None,
            "created_at": None,
            "doc_count": len(data),
            "chunk_count": sum(len(chunk_ids) for chunk_ids in data.values()),
        }

    def purge_doc(self, tenant: str, collection: str, docid: str) -> int:
        cat = os.path.join(self._dir(tenant, collection), "catalog.json")
        try:
            data = json.load(open(cat, "r", encoding="utf-8"))
        except Exception:
            data = {}
        removed = 1 if docid in data else 0
        data.pop(docid, None)
        with open(cat, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return removed

    def has_doc(self, tenant: str, collection: str, docid: str) -> bool:
        cat = os.path.join(self._dir(tenant, collection), "catalog.json")
        try:
            data = json.load(open(cat, "r", encoding="utf-8"))
        except Exception:
            data = {}
        ret = 1 if docid in data else 0
        return bool(ret)

    def get_document(
        self,
        tenant: str,
        collection: str,
        docid: str,
    ) -> dict[str, Any] | None:
        cat = os.path.join(self._dir(tenant, collection), "catalog.json")
        try:
            data = json.load(open(cat, "r", encoding="utf-8"))
        except Exception:
            data = {}
        chunk_ids = list(data.get(docid, []))
        if not chunk_ids:
            return None
        return {
            "docid": docid,
            "version": 1,
            "ingested_at": None,
            "metadata": {"docid": docid},
            "chunk_ids": chunk_ids,
            "chunk_count": len(chunk_ids),
        }

    def list_documents(
        self,
        tenant: str,
        collection: str,
    ) -> list[dict[str, Any]]:
        cat = os.path.join(self._dir(tenant, collection), "catalog.json")
        try:
            data = json.load(open(cat, "r", encoding="utf-8"))
        except Exception:
            data = {}
        docs: list[dict[str, Any]] = []
        for docid, chunk_ids in data.items():
            docs.append(
                {
                    "docid": docid,
                    "version": 1,
                    "ingested_at": "1970-01-01T00:00:00Z",
                    "chunk_count": len(chunk_ids),
                }
            )
        return docs

    def list_chunks(
        self,
        tenant: str,
        collection: str,
        docid: str,
    ) -> list[dict[str, Any]]:
        return []

    def get_chunk(
        self,
        tenant: str,
        collection: str,
        rid: str,
    ) -> dict[str, Any] | None:
        return None

    def get_chunk_content(
        self,
        tenant: str,
        collection: str,
        rid: str,
    ) -> dict[str, Any] | None:
        return None

    def index_records(self, tenant: str, collection: str, docid: str,
                      records: Iterable[Record],
                      doc_meta: dict[str, Any] | None = None) -> int:
        cat = os.path.join(self._dir(tenant, collection), "catalog.json")
        try:
            data = json.load(open(cat, "r", encoding="utf-8"))
        except Exception:
            data = {}
        ids: list[str] = []
        for i, (rid, _, _) in enumerate(records):
            ids.append(rid or f"{docid}-{i}")
        data.setdefault(docid, []).extend(ids)
        with open(cat, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return len(ids)

    def search(self, tenant: str, collection: str, text: str, k: int = 5,
               filters: dict[str, Any] | None = None) -> SearchOutput:
        cat = os.path.join(self._dir(tenant, collection), "catalog.json")
        if not os.path.isfile(cat):
            return SearchOutput(matches=[])
        data = json.load(open(cat, "r", encoding="utf-8"))
        hits: list[SearchResult] = []
        for docid, ids in data.items():
            for cid in ids[:k]:
                hits.append(SearchResult(
                    id=cid, score=1.0, text=None, tenant=tenant,
                    collection=collection, meta={"docid": docid},
                    match_reason="matched"))
        return SearchOutput(matches=hits)

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
        return None

    def get_query_log_entry(
        self,
        tenant: str,
        collection: str,
        query_id: str,
    ) -> dict[str, Any] | None:
        return None

    def list_query_logs(
        self,
        tenant: str,
        collection: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return []

    def put_query_home(
        self,
        query_id: str,
        tenant: str,
        collection: str,
    ) -> None:
        return None

    def resolve_query_home(
        self,
        query_id: str,
    ) -> tuple[str, str] | None:
        return None

    def purge_query_homes_for_collection(
        self,
        tenant: str,
        collection: str,
    ) -> None:
        return None

    def list_query_homes(
        self,
        tenant: str | None = None,
        collection: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return []

    def dump_archive(
        self,
        output_path: str | os.PathLike[str] | None = None,
    ) -> tuple[str, str | None]:
        import tempfile
        import zipfile
        from pathlib import Path

        data_dir = Path(get_cfg().get("data_dir")).resolve()
        if output_path is None:
            tmp_dir = Path(tempfile.mkdtemp(prefix="dummy-export_"))
            archive_path = tmp_dir / "pavedb-data.zip"
            tmp_dir_str = str(tmp_dir)
        else:
            archive_path = Path(output_path).resolve()
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_dir_str = None
        with zipfile.ZipFile(archive_path, "w") as zf:
            for root, _dirs, files in os.walk(data_dir):
                root_path = Path(root)
                for filename in files:
                    file_path = root_path / filename
                    zf.write(file_path, file_path.relative_to(data_dir).as_posix())
        return str(archive_path), tmp_dir_str

    def restore_archive(self, archive_bytes: bytes) -> None:
        import io
        import zipfile
        from pathlib import Path

        data_dir = Path(get_cfg().get("data_dir")).resolve()
        if data_dir.exists():
            shutil.rmtree(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(archive_bytes), "r") as zf:
            zf.extractall(data_dir)


class SpyStore(BaseStore):
    def __init__(self, impl: BaseStore):
        self.impl = impl
        self.calls: list[tuple] = []

    def create_collection(self, tenant: str, name: str) -> None:
        self.calls.append(("create_collection", tenant, name))
        return self.impl.create_collection(tenant, name)

    def delete_collection(self, tenant: str, collection: str) -> None:
        self.calls.append(("delete_collection", tenant, collection))
        return self.impl.delete_collection(tenant, collection)

    def rename_collection(self, tenant: str, old_name: str, new_name: str) -> None:
        self.calls.append(("rename_collection", tenant, old_name, new_name))
        return self.impl.rename_collection(tenant, old_name, new_name)

    def list_collections(self, tenant: str) -> list[dict[str, Any]]:
        self.calls.append(("list_collections", tenant))
        return self.impl.list_collections(tenant)

    def get_collection_detail(
        self,
        tenant: str,
        name: str,
    ) -> dict[str, Any] | None:
        self.calls.append(("get_collection_detail", tenant, name))
        return self.impl.get_collection_detail(tenant, name)

    def purge_doc(self, tenant: str, collection: str, docid: str) -> int:
        self.calls.append(("purge_doc", tenant, collection, docid))
        return self.impl.purge_doc(tenant, collection, docid)

    def has_doc(self, tenant: str, collection: str, docid: str) -> bool:
        self.calls.append(("has_doc", tenant, collection, docid))
        return self.impl.has_doc(tenant, collection, docid)

    def get_document(
        self,
        tenant: str,
        collection: str,
        docid: str,
    ) -> dict[str, Any] | None:
        self.calls.append(("get_document", tenant, collection, docid))
        return self.impl.get_document(tenant, collection, docid)

    def list_documents(
        self,
        tenant: str,
        collection: str,
    ) -> list[dict[str, Any]]:
        self.calls.append(("list_documents", tenant, collection))
        return self.impl.list_documents(tenant, collection)

    def list_chunks(
        self,
        tenant: str,
        collection: str,
        docid: str,
    ) -> list[dict[str, Any]]:
        self.calls.append(("list_chunks", tenant, collection, docid))
        return self.impl.list_chunks(tenant, collection, docid)

    def get_chunk(
        self,
        tenant: str,
        collection: str,
        rid: str,
    ) -> dict[str, Any] | None:
        self.calls.append(("get_chunk", tenant, collection, rid))
        return self.impl.get_chunk(tenant, collection, rid)

    def get_chunk_content(
        self,
        tenant: str,
        collection: str,
        rid: str,
    ) -> dict[str, Any] | None:
        self.calls.append(("get_chunk_content", tenant, collection, rid))
        return self.impl.get_chunk_content(tenant, collection, rid)

    def index_records(self, tenant: str, collection: str, docid: str,
                      records: Iterable[Record],
                      doc_meta: dict[str, Any] | None = None) -> int:
        recs = list(records)
        self.calls.append(
            ("index_records", tenant, collection, docid, len(recs), doc_meta)
        )
        return self.impl.index_records(
            tenant, collection, docid, recs, doc_meta=doc_meta
        )

    def search(self, tenant: str, collection: str, text: str, k: int = 5,
               filters: dict[str, Any] | None = None) -> SearchOutput:
        self.calls.append(("search", tenant, collection, text, k, filters))
        return self.impl.search(tenant, collection, text, k, filters)

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
        self.calls.append(
            (
                "log_query",
                {
                    "query_id": query_id,
                    "tenant": tenant,
                    "collection": collection,
                    "actor": actor,
                    "query_text": query_text,
                    "k": k,
                    "filters": filters,
                    "include_common": include_common,
                    "common_tenant": common_tenant,
                    "common_collection": common_collection,
                    "result_ids": result_ids,
                    "result_count": result_count,
                    "latency_ms": latency_ms,
                    "timing": timing,
                    "request_id": request_id,
                    "replay_of": replay_of,
                },
            )
        )
        return self.impl.log_query(
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

    def get_query_log_entry(
        self,
        tenant: str,
        collection: str,
        query_id: str,
    ) -> dict[str, Any] | None:
        self.calls.append(("get_query_log_entry", tenant, collection, query_id))
        return self.impl.get_query_log_entry(tenant, collection, query_id)

    def list_query_logs(
        self,
        tenant: str,
        collection: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            ("list_query_logs", tenant, collection, limit, offset)
        )
        return self.impl.list_query_logs(tenant, collection, limit, offset)

    def put_query_home(
        self,
        query_id: str,
        tenant: str,
        collection: str,
    ) -> None:
        self.calls.append(("put_query_home", query_id, tenant, collection))
        return self.impl.put_query_home(query_id, tenant, collection)

    def resolve_query_home(
        self,
        query_id: str,
    ) -> tuple[str, str] | None:
        self.calls.append(("resolve_query_home", query_id))
        return self.impl.resolve_query_home(query_id)

    def purge_query_homes_for_collection(
        self,
        tenant: str,
        collection: str,
    ) -> None:
        self.calls.append(
            ("purge_query_homes_for_collection", tenant, collection)
        )
        return self.impl.purge_query_homes_for_collection(
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
        self.calls.append(
            ("list_query_homes", tenant, collection, limit, offset)
        )
        return self.impl.list_query_homes(
            tenant=tenant,
            collection=collection,
            limit=limit,
            offset=offset,
        )

    def list_tenants(self) -> list[str]:
        self.calls.append(("list_tenants",))
        return self.impl.list_tenants()

    def catalog_metrics(self) -> dict[str, int]:
        self.calls.append(("catalog_metrics",))
        return self.impl.catalog_metrics()

    def dump_archive(
        self,
        output_path: str | os.PathLike[str] | None = None,
    ) -> tuple[str, str | None]:
        self.calls.append(("dump_archive", output_path))
        return self.impl.dump_archive(output_path)

    def restore_archive(self, archive_bytes: bytes) -> None:
        self.calls.append(("restore_archive", len(archive_bytes)))
        return self.impl.restore_archive(archive_bytes)
