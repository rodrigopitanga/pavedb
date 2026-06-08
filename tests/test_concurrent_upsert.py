# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import time
import json
from concurrent.futures import ThreadPoolExecutor

from pave.stores.local import LocalStore
from utils import FakeEmbedder

REC0 = ("doc::0", "texto A", "{}")
REC1 = ("doc::1", "texto B", "{}")

def test_concurrent_upsert_with_manual_lock(cfg, temp_data_dir):
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    tenant, coll = "tenantY", "collSafe"
    store._load_or_init(tenant, coll)
    backend = store._emb[(tenant, coll)]
    col_db = store._dbs[(tenant, coll)]

    def safe_upsert(data):
        with store._collection_write_lock(tenant, coll):
            rid, text, meta = data
            chunk_path = f"chunks/{store._urid_to_fname(rid)}"
            parsed_meta = json.loads(meta) if isinstance(meta, str) else meta
            parsed_meta = dict(parsed_meta or {})
            parsed_meta["docid"] = "doc"
            store._save_chunk_text(tenant, coll, rid, text)
            col_db.upsert_chunks("doc", [(rid, chunk_path, parsed_meta)])
            backend.delete([rid])
            vectors = store._embedder.encode([text])
            backend.add([rid], vectors)
            time.sleep(0.05)
            store._save(tenant, coll)

    for _ in range(100):
        with ThreadPoolExecutor(max_workers=2) as ex:
            ex.submit(safe_upsert, REC0)
            ex.submit(safe_upsert, REC1)
        results = store.search(tenant, coll, "texto", 5)
        texts = [r.text for r in results]
        assert "texto A" in texts and "texto B" in texts,\
            "Inconsistent state detected despite locking (manual test)"

def test_concurrent_upsert_with_lock_always_consistent(cfg, temp_data_dir):
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    tenant, coll = "tenantZ", "collSafe"
    store._load_or_init(tenant, coll)
    emb = store._emb[(tenant, coll)]

    # index_records is an atomic full-doc replace, so concurrent calls with
    # the SAME docid intentionally clobber each other. Concurrency on
    # DISTINCT docids must remain consistent.
    def safe_upsert(docid, data):
        store.index_records(tenant, coll, docid, [data])

    for _ in range(100):
        with ThreadPoolExecutor(max_workers=2) as ex:
            ex.submit(safe_upsert, "docA", REC0)
            ex.submit(safe_upsert, "docB", REC1)
        results = store.search(tenant, coll, "texto", 5)
        texts = [r.text for r in results]
        assert "texto A" in texts and "texto B" in texts,\
            "Inconsistent state detected despite locking (main codepath)"
