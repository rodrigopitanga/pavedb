# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from pave.stores.local import LocalStore
from utils import FakeEmbedder


def test_has_doc_uses_atomic_cache_lookup_under_flush_race(temp_data_dir):
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
    assert store.has_doc(tenant, collection, docid) is True
