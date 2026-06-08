# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from pave.embedders.sbert import SbertEmbedder
from pave.stores.local import LocalStore

pytestmark = [
    pytest.mark.slow,
    pytest.mark.relevance,
]

if os.getenv("PAVETEST_REL") != "1":
    pytest.skip(
        "set PAVETEST_REL=1 or use `make test-relevance`",
        allow_module_level=True,
    )

datasets = pytest.importorskip(
    "datasets",
    reason="install `datasets` or use `make test-relevance`",
)

_FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "relevance_profiles.json"
)


def _load_manifest() -> dict[str, Any]:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _manifest() -> dict[str, Any]:
    return _load_manifest()


def _env(name: str) -> str | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _select_profile_ids() -> list[str]:
    requested = _env("PAVETEST_REL_PROFILE")
    profiles = _manifest()["profiles"]
    if requested is None:
        return [str(item["id"]) for item in profiles]
    wanted = {part.strip() for part in requested.split(",") if part.strip()}
    selected = [str(item["id"]) for item in profiles if item["id"] in wanted]
    if not selected:
        raise AssertionError(
            "No relevance profiles matched PAVETEST_REL_PROFILE="
            f"{requested!r}"
        )
    return selected


def _select_models(profile: dict[str, Any]) -> list[dict[str, Any]]:
    required = set(profile.get("required_capabilities", []))
    requested = _env("PAVETEST_REL_MODEL_ID")
    models = _manifest()["models"]
    if requested is not None:
        wanted = {part.strip() for part in requested.split(",") if part.strip()}
        models = [item for item in models if item["id"] in wanted]
        if not models:
            raise AssertionError(
                "No relevance models matched PAVETEST_REL_MODEL_ID="
                f"{requested!r}"
            )
    out = [
        item
        for item in models
        if required.issubset(set(item.get("capabilities", [])))
    ]
    if not out:
        raise AssertionError(
            f"No model supports profile {profile['id']!r} with {sorted(required)}"
        )
    return out


def _profile_cases() -> list[tuple[dict[str, Any], dict[str, Any]]]:
    profiles = {
        str(item["id"]): item
        for item in _manifest()["profiles"]
    }
    cases: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for profile_id in _select_profile_ids():
        profile = profiles[profile_id]
        for model in _select_models(profile):
            cases.append((model, profile))
    return cases


def _case_id(case: tuple[dict[str, Any], dict[str, Any]]) -> str:
    model, profile = case
    return f"{profile['id']}[{model['id']}]"


def _normalize_rows(matrix: np.ndarray) -> np.ndarray:
    arr = np.asarray(matrix, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return arr / norms


def _score_ranking(
    query_vec: np.ndarray,
    doc_ids: list[str],
    doc_matrix: np.ndarray,
) -> list[tuple[str, float]]:
    q = _normalize_rows(np.asarray(query_vec, dtype=np.float32).reshape(1, -1))[0]
    scores = doc_matrix @ q
    order = np.argsort(-scores, kind="stable")
    return [
        (doc_ids[int(idx)], float(scores[int(idx)]))
        for idx in order
    ]


def _top_band(
    scored_ids: list[tuple[str, float]],
    *,
    atol: float = 5e-4,
) -> set[str]:
    if not scored_ids:
        return set()
    top_score = scored_ids[0][1]
    return {
        rid
        for rid, score in scored_ids
        if top_score - score <= atol
    }


def _cutoff_window(
    scored_ids: list[tuple[str, float]],
    *,
    k: int,
    atol: float = 5e-4,
) -> tuple[set[str], set[str]]:
    if not scored_ids:
        return set(), set()
    cutoff_idx = min(len(scored_ids), max(1, k)) - 1
    cutoff = scored_ids[cutoff_idx][1]
    must_have = {
        rid
        for rid, score in scored_ids
        if score > cutoff + atol
    }
    may_have = {
        rid
        for rid, score in scored_ids
        if score >= cutoff - atol
    }
    return must_have, may_have


@lru_cache(maxsize=None)
def _profile_rows(profile_id: str) -> list[dict[str, str]]:
    profile = next(
        item for item in _manifest()["profiles"] if item["id"] == profile_id
    )
    cfg = profile["dataset"]
    dataset = datasets.load_dataset(
        cfg["path"],
        split=cfg.get("split", "test"),
    )
    pairs = [str(item) for item in cfg["language_pairs"]]
    counts = {pair: 0 for pair in pairs}
    rows: list[dict[str, str]] = []
    min_chars = int(cfg.get("min_text_chars", 1))

    for row in dataset:
        pair = str(row[cfg["lang_field"]])
        if pair not in counts or counts[pair] >= int(cfg["rows_per_pair"]):
            continue
        query_text = str(row[cfg["query_field"]]).strip()
        doc_text = str(row[cfg["document_field"]]).strip()
        if len(query_text) < min_chars or len(doc_text) < min_chars:
            continue
        ordinal = counts[pair]
        counts[pair] += 1
        rows.append(
            {
                "doc_id": f"docs::{pair}-{ordinal:03d}",
                "query_text": query_text,
                "doc_text": doc_text,
                "pair": pair,
            }
        )
        if all(
            count >= int(cfg["rows_per_pair"])
            for count in counts.values()
        ):
            break

    missing = [
        pair for pair, count in counts.items()
        if count < int(cfg["rows_per_pair"])
    ]
    if missing:
        raise AssertionError(
            f"Profile {profile_id!r} could not load enough rows for {missing}"
        )
    return rows


_CASES = _profile_cases()


@pytest.mark.parametrize(
    ("model_cfg", "profile_cfg"),
    _CASES,
    ids=[_case_id(case) for case in _CASES],
)
def test_public_profile_matches_bruteforce_baseline(
    model_cfg: dict[str, Any],
    profile_cfg: dict[str, Any],
    tmp_path,
) -> None:
    rows = _profile_rows(str(profile_cfg["id"]))
    k = int(profile_cfg.get("expected_k", 5))
    embedder = SbertEmbedder(model_name=str(model_cfg["model"]))
    store = LocalStore(str(tmp_path), embedder)
    tenant = "relevance"
    collection = str(profile_cfg["id"])

    store.create_collection(tenant, collection)
    n = store.index_records(
        tenant,
        collection,
        "docs",
        [
            (
                row["doc_id"],
                row["doc_text"],
                {"pair": row["pair"], "source": "tatoeba"},
            )
            for row in rows
        ],
        doc_meta={"profile": profile_cfg["id"]},
    )
    assert n.indexed_chunks == len(rows)

    doc_ids = [row["doc_id"] for row in rows]
    doc_matrix = _normalize_rows(
        embedder.encode([row["doc_text"] for row in rows])
    )

    for row in rows:
        query_vec = embedder.encode([row["query_text"]])[0]
        scored = _score_ranking(query_vec, doc_ids, doc_matrix)
        expected = scored[:k]
        hits = store.search(
            tenant,
            collection,
            row["query_text"],
            k=k,
        )
        got = [hit.id for hit in hits]
        expected_ids = [rid for rid, _score in expected]
        assert len(got) == len(expected_ids), (
            f"profile={profile_cfg['id']} model={model_cfg['id']} "
            f"pair={row['pair']} expected_len={len(expected_ids)} "
            f"got_len={len(got)}"
        )
        got_set = set(got)
        must_have, may_have = _cutoff_window(scored, k=k)
        assert must_have.issubset(got_set), (
            f"profile={profile_cfg['id']} model={model_cfg['id']} "
            f"pair={row['pair']} must_have={sorted(must_have)} "
            f"got={sorted(got)}"
        )
        assert got_set.issubset(may_have), (
            f"profile={profile_cfg['id']} model={model_cfg['id']} "
            f"pair={row['pair']} may_have={sorted(may_have)} "
            f"got={sorted(got)}"
        )
        assert got[0] in _top_band(scored), (
            f"profile={profile_cfg['id']} model={model_cfg['id']} "
            f"pair={row['pair']} top_band={sorted(_top_band(scored))} "
            f"got_top={got[0]}"
        )
