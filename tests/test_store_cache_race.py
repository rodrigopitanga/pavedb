# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import threading
from contextlib import contextmanager

from pave.stores.local import LocalStore, _CollectionReadWriteLock
from utils import FakeEmbedder


def test_get_document_uses_atomic_cache_lookup_under_flush_race(temp_data_dir):
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    tenant, collection, docid = "acme", "race", "DOC-1"
    records = [("0", "cache race probe", {"lang": "en"})]
    store.index_records(tenant, collection, docid, records)

    key = (tenant, collection)
    col_db = store._dbs[key]

    class RaceyCache(dict):
        """Simulates clear() happening between membership check and lookup."""

        def __init__(self, target_key, target_db):
            super().__init__({target_key: target_db})
            self._target_key = target_key

        def __contains__(self, probe_key):
            if probe_key == self._target_key:
                super().clear()
                return True
            return super().__contains__(probe_key)

    store._dbs = RaceyCache(key, col_db)

    # With the old pattern (`if key in _dbs: _dbs[key]`) this raises KeyError.
    assert store.get_document(tenant, collection, docid) is not None


def test_collection_rwlock_nested_read_does_not_deadlock_pending_writer():
    """search() holds a read lock and then re-enters via _read_collection_db.
    A queued writer between the two reads must not strand the reader on the
    writer-priority gate (otherwise the reader waits on the writer waiting on
    the reader -> deadlock)."""
    lock = _CollectionReadWriteLock()
    writer_queued = threading.Event()
    reader_done = threading.Event()

    def reader_with_nested_read():
        with lock.read_lock():
            # Give the writer time to register as pending.
            writer_queued.wait(timeout=1.0)
            with lock.read_lock():
                pass
        reader_done.set()

    def writer():
        writer_queued.set()
        with lock.write_lock():
            pass

    r = threading.Thread(target=reader_with_nested_read, daemon=True)
    w = threading.Thread(target=writer, daemon=True)
    r.start()
    w.start()
    r.join(timeout=3.0)
    w.join(timeout=3.0)
    assert reader_done.is_set(), (
        "nested read failed to re-enter while a writer was queued"
    )


def test_search_final_cache_retry_does_not_run_under_write_lock(temp_data_dir):
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    tenant, collection = "acme", "retry_search"
    store.index_records(
        tenant,
        collection,
        "DOC-1",
        [("0", "retry search probe", {"lang": "en"})],
    )

    original_read_lock = store._collection_read_lock
    state = {"in_read": False, "misses": 3}

    @contextmanager
    def read_lock(t: str, c: str):
        with original_read_lock(t, c):
            state["in_read"] = True
            try:
                yield
            finally:
                state["in_read"] = False

    class FlakyEmb(dict):
        def get(self, key, default=None):
            if state["in_read"] and state["misses"] > 0:
                state["misses"] -= 1
                return default
            return super().get(key, default)

    store._collection_read_lock = read_lock  # type: ignore[method-assign]
    store._emb = FlakyEmb(store._emb)

    done = threading.Event()
    result: dict[str, object] = {}

    def run_search() -> None:
        result["hits"] = store.search(tenant, collection, "retry", k=1)
        done.set()

    t = threading.Thread(target=run_search, daemon=True)
    t.start()
    t.join(timeout=2.0)

    assert done.is_set(), "search deadlocked on final cache retry"
    assert result["hits"]
