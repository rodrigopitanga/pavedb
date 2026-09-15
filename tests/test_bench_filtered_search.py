# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pytest

from pave.backends.faiss import FaissBackend

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "benchmarks"),
)

import filtered_search  # type: ignore[import-not-found]


def test_filtered_benchmark_models_overfetch_and_result_parity(tmp_path):
    backend = FaissBackend(2, storage_dir=tmp_path / "index")
    backend.add(
        ["r1", "r2", "r3", "r4"],
        np.asarray(
            [[1.0, 0.0], [0.8, 0.2], [0.2, 0.8], [0.0, 1.0]],
            dtype=np.float32,
        ),
    )
    query = np.asarray([1.0, 0.0], dtype=np.float32)
    allowed = {"r3", "r4"}

    legacy, calls, limit = filtered_search._legacy_filtered_search(
        backend,
        query,
        2,
        allowed,
    )
    accelerated = backend.search_filtered(query, 2, allowed)

    assert [rid for rid, _score in legacy] == [
        rid for rid, _score in accelerated
    ]
    assert calls == 1
    assert limit == 4


@pytest.mark.parametrize("value", ["0", "1.1", "0.1,-0.2", "abc"])
def test_parse_selectivities_rejects_out_of_range_values(value):
    with pytest.raises(argparse.ArgumentTypeError, match="selectivities"):
        filtered_search._parse_selectivities(value)
