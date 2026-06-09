# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import types

import numpy as np
import pytest

pytest.importorskip(
    "sentence_transformers",
    reason="sentence-transformers not installed",
)

import pave.embedders.sbert as sbert_mod  # noqa: E402


class _DummyCFG:
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def get(self, key: str, default=None):
        return self._values.get(key, default)


def test_encode_returns_float32_ndarray(monkeypatch) -> None:
    seen: dict[str, object] = {}

    class FakeModel:
        def __init__(self, model_name: str, *, device: str) -> None:
            seen["model_name"] = model_name
            seen["device"] = device

        def get_sentence_embedding_dimension(self) -> int:
            return 2

        def encode(
            self,
            texts,
            *,
            batch_size: int,
            show_progress_bar: bool,
            convert_to_numpy: bool,
        ):
            seen["texts"] = list(texts)
            seen["batch_size"] = batch_size
            seen["show_progress_bar"] = show_progress_bar
            seen["convert_to_numpy"] = convert_to_numpy
            return np.array([[1, 2], [3, 4]], dtype=np.float64)

    monkeypatch.setattr(
        sbert_mod,
        "SentenceTransformer",
        FakeModel,
        raising=True,
    )
    monkeypatch.setattr(
        sbert_mod,
        "CFG",
        _DummyCFG(
            {
                "embedder.sbert.model": "sentence-transformers/test-model",
                "embedder.sbert.device": "cpu",
                "embedder.sbert.batch_size": 16,
            }
        ),
        raising=True,
    )

    emb = sbert_mod.SbertEmbedder()
    out = emb.encode(["a", "b"])

    assert isinstance(out, np.ndarray)
    assert out.dtype == np.float32
    assert out.shape == (2, 2)
    assert seen["model_name"] == "sentence-transformers/test-model"
    assert seen["device"] == "cpu"
    assert seen["texts"] == ["a", "b"]
    assert seen["batch_size"] == 16
    assert seen["show_progress_bar"] is False
    assert seen["convert_to_numpy"] is True


def test_auto_device_resolves_to_cpu_when_no_accelerator(monkeypatch) -> None:
    seen: dict[str, object] = {}

    class FakeModel:
        def __init__(self, model_name: str, *, device: str) -> None:
            seen["model_name"] = model_name
            seen["device"] = device

        def get_sentence_embedding_dimension(self) -> int:
            return 2

        def encode(self, texts, **_kwargs):
            return np.array([[1.0, 2.0] for _ in texts], dtype=np.float32)

    monkeypatch.setattr(
        sbert_mod,
        "SentenceTransformer",
        FakeModel,
        raising=True,
    )
    monkeypatch.setattr(
        sbert_mod.torch.cuda,
        "is_available",
        lambda: False,
        raising=True,
    )
    mps = getattr(sbert_mod.torch.backends, "mps", None)
    if mps is not None:
        monkeypatch.setattr(
            mps,
            "is_available",
            lambda: False,
            raising=True,
        )
    monkeypatch.setattr(
        sbert_mod,
        "CFG",
        _DummyCFG(
            {
                "embedder.sbert.model": "sentence-transformers/test-model",
                "embedder.sbert.device": "auto",
            }
        ),
        raising=True,
    )

    sbert_mod.SbertEmbedder()

    assert seen["model_name"] == "sentence-transformers/test-model"
    assert seen["device"] == "cpu"


def test_dim_reads_model_dimension_without_probe(monkeypatch) -> None:
    calls = {"encode": 0}

    class FakeModel:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def get_embedding_dimension(self) -> int:
            return 7

        def get_sentence_embedding_dimension(self) -> int:
            raise AssertionError("deprecated dimension method was called")

        def encode(self, texts, **_kwargs):
            calls["encode"] += 1
            return np.array([[0.0] * 7 for _ in texts], dtype=np.float32)

    monkeypatch.setattr(
        sbert_mod,
        "SentenceTransformer",
        FakeModel,
        raising=True,
    )
    monkeypatch.setattr(
        sbert_mod,
        "CFG",
        _DummyCFG({}),
        raising=True,
    )

    emb = sbert_mod.SbertEmbedder()
    assert emb.dim == 7
    assert calls["encode"] == 0


def test_dim_probes_when_model_dimension_missing(monkeypatch) -> None:
    seen: dict[str, object] = {}

    class FakeModel:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def get_sentence_embedding_dimension(self) -> int:
            raise RuntimeError("dimension unavailable")

        def encode(self, texts, **_kwargs):
            seen["texts"] = list(texts)
            return np.array([[1.0, 2.0, 3.0]], dtype=np.float32)

    monkeypatch.setattr(
        sbert_mod,
        "SentenceTransformer",
        FakeModel,
        raising=True,
    )
    monkeypatch.setattr(
        sbert_mod,
        "CFG",
        _DummyCFG({}),
        raising=True,
    )

    emb = sbert_mod.SbertEmbedder()
    assert emb.dim == 3
    assert seen["texts"] == ["_"]


def test_explicit_model_overrides_cfg(monkeypatch) -> None:
    seen: dict[str, object] = {}

    class FakeModel:
        def __init__(self, model_name: str, *, device: str) -> None:
            seen["model_name"] = model_name
            seen["device"] = device

        def get_sentence_embedding_dimension(self) -> int:
            return 5

        def encode(self, texts, **_kwargs):
            return np.array([[1.0] * 5 for _ in texts], dtype=np.float32)

    monkeypatch.setattr(
        sbert_mod,
        "SentenceTransformer",
        FakeModel,
        raising=True,
    )
    monkeypatch.setattr(
        sbert_mod,
        "CFG",
        _DummyCFG(
            {
                "embedder.sbert.model": "sentence-transformers/from-cfg",
                "embedder.sbert.device": "cpu",
                "embedder.sbert.batch_size": 16,
            }
        ),
        raising=True,
    )

    emb = sbert_mod.SbertEmbedder(
        model_name="sentence-transformers/from-arg",
        device="cuda",
        batch_size=8,
    )

    assert emb.dim == 5
    assert emb.batch_size == 8
    assert seen["model_name"] == "sentence-transformers/from-arg"
    assert seen["device"] == "cuda"
