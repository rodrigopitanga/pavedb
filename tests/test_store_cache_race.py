# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import threading
import time
from contextlib import contextmanager

from pave.stores.local import LocalStore, _CollectionReadWriteLock
from utils import FakeEmbedder


class BlockingIngestEmbedder(FakeEmbedder):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def encode(self, texts):
        if any("slow ingest payload" in text for text in texts):
            self.started.set()
            if not self.release.wait(timeout=2.0):
                raise TimeoutError("blocked ingest encode was not released")
        return super().encode(texts)


class CountingEmbedder(FakeEmbedder):
    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, ...]] = []

    def encode(self, texts):
        self.calls.append(tuple(texts))
        return super().encode(texts)


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


def test_collection_rwlock_queued_writer_does_not_block_new_reader():
    lock = _CollectionReadWriteLock()
    first_reader_ready = threading.Event()
    release_first_reader = threading.Event()
    writer_entered = threading.Event()
    second_reader_entered = threading.Event()

    def first_reader():
        with lock.read_lock():
            first_reader_ready.set()
            release_first_reader.wait(timeout=3.0)

    def writer():
        first_reader_ready.wait(timeout=1.0)
        with lock.write_lock():
            writer_entered.set()

    def second_reader():
        with lock.read_lock():
            second_reader_entered.set()

    r1 = threading.Thread(target=first_reader, daemon=True)
    w = threading.Thread(target=writer, daemon=True)
    r1.start()
    w.start()
    assert first_reader_ready.wait(timeout=1.0)

    deadline = time.monotonic() + 1.0
    while lock._pending_writers == 0 and time.monotonic() < deadline:
        time.sleep(0.001)
    assert lock._pending_writers == 1

    r2 = threading.Thread(target=second_reader, daemon=True)
    r2.start()
    r2.join(timeout=1.0)

    assert second_reader_entered.is_set()
    assert not writer_entered.is_set()

    release_first_reader.set()
    r1.join(timeout=1.0)
    w.join(timeout=1.0)
    assert writer_entered.is_set()


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

    def run_search():
        result["hits"] = store.search(tenant, collection, "retry", k=1)
        done.set()

    t = threading.Thread(target=run_search, daemon=True)
    t.start()
    t.join(timeout=2.0)

    assert done.is_set(), "search deadlocked on final cache retry"
    assert result["hits"]


def test_search_reports_collection_moved_during_query_encode(temp_data_dir):
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    tenant, collection = "acme", "moving_search"
    store.index_records(
        tenant,
        collection,
        "DOC-1",
        [("0", "moving search probe", {"lang": "en"})],
    )

    original_encode = store._encode_query_cached
    encode_started = threading.Event()
    release_encode = threading.Event()
    errors: list[BaseException] = []

    def blocked_encode(*args, **kwargs):
        encode_started.set()
        if not release_encode.wait(timeout=2.0):
            raise TimeoutError("blocked search encode was not released")
        return original_encode(*args, **kwargs)

    store._encode_query_cached = blocked_encode  # type: ignore[method-assign]

    def search():
        try:
            store.search(tenant, collection, "moving", k=1)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=search, daemon=True)
    worker.start()
    assert encode_started.wait(timeout=1.0)
    try:
        store.move_collection(tenant, collection, "moved_search")
    finally:
        release_encode.set()
    worker.join(timeout=2.0)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    assert str(errors[0]) == "collection 'moving_search' does not exist"


def test_search_runs_while_batch_ingest_embeds(temp_data_dir):
    embedder = BlockingIngestEmbedder()
    store = LocalStore(str(temp_data_dir), embedder)
    tenant, collection = "acme", "ingest_search_overlap"
    store.index_records(
        tenant,
        collection,
        "DOC-1",
        [("0", "fast searchable probe", {"lang": "en"})],
    )

    def ingest():
        store.index_documents(
            tenant,
            collection,
            [
                (
                    "DOC-2",
                    [("0", "slow ingest payload", {"lang": "en"})],
                    None,
                )
            ],
        )

    _assert_search_runs_while_ingest_embeds(
        store,
        embedder,
        tenant,
        collection,
        ingest,
        "search waited for batch ingest embedding",
    )


def test_search_runs_while_single_doc_ingest_embeds(temp_data_dir):
    embedder = BlockingIngestEmbedder()
    store = LocalStore(str(temp_data_dir), embedder)
    tenant, collection = "acme", "single_ingest_search_overlap"
    store.index_records(
        tenant,
        collection,
        "DOC-1",
        [("0", "fast searchable probe", {"lang": "en"})],
    )

    def ingest():
        store.index_records(
            tenant,
            collection,
            "DOC-2",
            [("0", "slow ingest payload", {"lang": "en"})],
        )

    _assert_search_runs_while_ingest_embeds(
        store,
        embedder,
        tenant,
        collection,
        ingest,
        "search waited for index_records embedding",
    )


def _assert_search_runs_while_ingest_embeds(
    store,
    embedder,
    tenant,
    collection,
    ingest_fn,
    wait_message,
):
    ingest_done = threading.Event()
    ingest_error: list[BaseException] = []

    def run_ingest():
        try:
            ingest_fn()
        except BaseException as exc:
            ingest_error.append(exc)
        finally:
            ingest_done.set()

    thread = threading.Thread(target=run_ingest, daemon=True)
    thread.start()
    assert embedder.started.wait(timeout=2.0)

    search_done = threading.Event()
    search_result: dict[str, object] = {}

    def search():
        search_result["hits"] = store.search(
            tenant,
            collection,
            "fast searchable",
            k=1,
        )
        search_done.set()

    search_thread = threading.Thread(target=search, daemon=True)
    search_thread.start()
    search_thread.join(timeout=0.5)

    embedder.release.set()
    thread.join(timeout=2.0)

    assert search_done.is_set(), wait_message
    assert search_result["hits"]
    assert ingest_done.is_set()
    assert not ingest_error


def test_search_retries_when_embedder_swapped_between_encode_and_search(
    temp_data_dir,
):
    """A delete+recreate that swaps the cached embedder between encode and the
    FAISS read lock must not feed the stale query vector (wrong dimension for
    the new backend) to search; it reloads and re-encodes instead (R3-23)."""
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    tenant, collection = "acme", "embedder_swap"
    store.index_records(
        tenant,
        collection,
        "DOC-1",
        [("0", "swap race probe", {"lang": "en"})],
    )
    key = (tenant, collection)

    real_encode = store._encode_query_cached
    encode_calls = {"n": 0}
    swapped = {"done": False}

    def spy_encode(t, c, embedder, q):
        encode_calls["n"] += 1
        vec = real_encode(t, c, embedder, q)
        if not swapped["done"]:
            swapped["done"] = True
            # Simulate delete+recreate swapping the cached embedder object
            # in the gap between encode and the FAISS read lock.
            store._collection_embedders[key] = FakeEmbedder()
        return vec

    store._encode_query_cached = spy_encode  # type: ignore[method-assign]

    hits = store.search(tenant, collection, "swap race", k=1)

    assert swapped["done"]
    assert encode_calls["n"] >= 2  # swap forced a reload + re-encode, not a stale read
    assert hits


def test_search_reuses_cached_query_vector(temp_data_dir):
    embedder = CountingEmbedder()
    store = LocalStore(str(temp_data_dir), embedder)
    tenant, collection = "acme", "query_cache"
    store.index_records(
        tenant,
        collection,
        "DOC-1",
        [("0", "cached query probe", {"lang": "en"})],
    )

    first = store.search(tenant, collection, "cached query", k=1)
    second = store.search(tenant, collection, "cached query", k=1)

    assert first
    assert second
    assert embedder.calls.count(("cached query",)) == 1
    assert set(second.timing) == {
        "embed_ms",
        "search_ms",
        "filter_ms",
        "hydrate_ms",
    }


def test_query_cache_rejects_reused_embedder_id(temp_data_dir):
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    old_embedder = CountingEmbedder()
    new_embedder = CountingEmbedder()
    key = ("acme", "docs", id(new_embedder), "query")
    store._query_vector_cache[key] = (old_embedder, old_embedder.encode(["stale"])[0])

    _vector, hit = store._encode_query_cached(
        "acme", "docs", new_embedder, "query"
    )

    assert not hit
    assert new_embedder.calls == [("query",)]
    assert store._query_vector_cache[key][0] is new_embedder


def test_collection_locks_are_evicted_once_idle(temp_data_dir):
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    store.index_records("acme", "real", "DOC-1", [("0", "hello", {})])
    for i in range(50):
        store.search("acme", f"missing-{i}", "hello", k=1)
    assert store._collection_locks == {}


def test_evicting_a_collection_lock_keeps_writers_exclusive(temp_data_dir):
    store = LocalStore(str(temp_data_dir), FakeEmbedder())
    inside = 0
    overlap = []
    guard = threading.Lock()

    def writer():
        nonlocal inside
        for _ in range(200):
            with store._collection_write_lock("acme", "hot"):
                with guard:
                    inside += 1
                    overlap.append(inside)
                time.sleep(0)
                with guard:
                    inside -= 1

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert max(overlap) == 1
    assert store._collection_locks == {}
