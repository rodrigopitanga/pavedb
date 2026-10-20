# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pave.filters import matches_filters, sanit_field
from pave.metadb import CollectionDB


pytestmark = pytest.mark.parity


def _meta_db(tmp_path: Path) -> Path:
    return tmp_path / "t_acme" / "c_parity" / "meta.db"


def _normalize(filters: dict[str, list[Any]]) -> dict[str, list[Any]]:
    return {sanit_field(key): values for key, values in filters.items()}


def _fully_pushed(filters: dict[str, list[Any]]) -> bool:
    skip_prefixes = ("!", ">=", "<=", ">", "<")
    for values in filters.values():
        for value in values:
            if not isinstance(value, str):
                return False
            if "*" in value or value.startswith(skip_prefixes):
                return False
    return True


def _open_parity_db(tmp_path: Path) -> tuple[CollectionDB, list[str]]:
    db = CollectionDB()
    db.open(_meta_db(tmp_path))
    for i in range(500):
        lang = ("pt", "en", "es", "de")[i % 4]
        tier = ("gold", "silver", "bronze")[i % 3]
        docid = f"doc{i:03d}"
        db.upsert_chunks(
            docid,
            [
                (
                    f"{docid}::chunk_0",
                    f"chunks/{docid}__chunk_0.txt",
                    {
                        "lang": lang,
                        "score": i % 17,
                        "ratio": round((i % 10) / 10, 1),
                        "enabled": i % 2 == 0,
                        "created": f"2026-01-{(i % 28) + 1:02d}",
                        "tags": [tier, f"group-{i % 5}"],
                    },
                )
            ],
            doc_meta={"docid": docid, "source": tier},
        )
    return db, [f"doc{i:03d}::chunk_0" for i in range(500)]


@pytest.mark.parametrize(
    "filters",
    [
        {"lang": ["pt"]},
        {"lang": ["!pt"]},
        {"source": ["gold"], "lang": ["en"]},
        {"docid": ["doc042"]},
        {"tags": ["gold"]},
        {"tags": ["*old"]},
        {"score": [">10"]},
        {"ratio": ["<=0.5"]},
        {"enabled": ["True"]},
        {"created": [">=2026-01-20"]},
        {"lang": ["pt", "*e*"]},
        {"source": ["gold"], "score": [">10"]},
    ],
)
def test_filter_pushdown_matches_canonical_capabilities(tmp_path, filters):
    db, all_rids = _open_parity_db(tmp_path)
    normed = _normalize(filters)

    pushed = db.filter_by_meta(all_rids, normed)
    meta = db.get_meta_batch(all_rids)
    canonical = {
        rid
        for rid in all_rids
        if matches_filters(meta.get(rid, {}), normed)
    }

    assert canonical <= pushed
    if _fully_pushed(normed):
        assert pushed == canonical
    db.close()


def test_filter_candidate_selection_matches_exact_pushdown(tmp_path):
    db, all_rids = _open_parity_db(tmp_path)
    for filters in (
        {"lang": ["pt"]},
        {"source": ["gold"], "lang": ["en"]},
        {"docid": ["doc042"]},
        {"tags": ["gold", "silver"]},
    ):
        selected = db.select_filter_candidates(filters, None)
        assert selected == db.filter_by_meta(all_rids, filters)
        assert db.select_filter_candidates(
            filters, None, len(all_rids) + 1,
        ) == selected
    # A cap below the eligible count stops the scan there, so the result
    # proves only that the predicate reaches the cap.
    capped = db.select_filter_candidates({"lang": ["pt"]}, None, 5)
    assert len(capped) == 5
    assert capped < db.select_filter_candidates({"lang": ["pt"]}, None)
    db.close()


def test_filter_pushdown_respects_chunk_metadata_override(tmp_path):
    db = CollectionDB()
    db.open(_meta_db(tmp_path))
    db.upsert_chunks(
        "doc1",
        [
            ("doc1::override", "chunks/override.txt", {"lang": "en"}),
            ("doc1::inherited", "chunks/inherited.txt", {}),
            (
                "doc1::list",
                "chunks/list.txt",
                {"lang": ["pt", "en"]},
            ),
        ],
        doc_meta={"lang": "pt"},
    )
    candidates = ["doc1::override", "doc1::inherited", "doc1::list"]

    assert db.filter_by_meta(candidates, {"lang": ["pt"]}) == {
        "doc1::inherited",
        "doc1::list",
    }
    assert db.filter_by_meta(candidates, {"lang": ["!pt"]}) == set(candidates)
    db.close()
