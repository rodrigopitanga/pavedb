# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import sys
import types

import numpy as np
import pytest

pytest.importorskip(
    "sentence_transformers",
    reason="sentence-transformers not installed",
)

import pave.embedders.sbert as sbert_mod  # noqa: E402


class _DummyCFG:
    def __init__(self, values: dict[str, object]):
        self._values = values

    def get(self, key: str, default=None):
        return self._values.get(key, default)


def _install_fake_deps(
    monkeypatch,
    fake_model,
    *,
    cuda: bool = False,
    mps: bool = False,
):
    fake_st = types.ModuleType("sentence_transformers")
    fake_st.SentenceTransformer = fake_model
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)

    fake_torch = types.ModuleType("torch")
    fake_torch.cuda = types.SimpleNamespace(is_available=lambda: cuda)
    fake_torch.backends = types.SimpleNamespace(
        mps=types.SimpleNamespace(is_available=lambda: mps)
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)


def test_encode_returns_float32_ndarray(monkeypatch):
    seen: dict[str, object] = {}
    metric_calls = []

    class FakeModel:
        def __init__(self, model_name: str, *, device: str):
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

    _install_fake_deps(monkeypatch, FakeModel)
    monkeypatch.setattr(
        sbert_mod,
        "inc_labeled",
        lambda *args: metric_calls.append(args),
    )
    monkeypatch.setattr(
        sbert_mod,
        "get_cfg",
        lambda: _DummyCFG(
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
    assert metric_calls == [
        ("embedder_embeddings_total", {"model": "sentence-transformers/test-model"}),
        (
            "embedder_embeddings_samples_total",
            {"model": "sentence-transformers/test-model"},
            2.0,
        ),
    ]


def test_auto_device_resolves_to_cpu_when_no_accelerator(monkeypatch):
    seen: dict[str, object] = {}

    class FakeModel:
        def __init__(self, model_name: str, *, device: str):
            seen["model_name"] = model_name
            seen["device"] = device

        def get_sentence_embedding_dimension(self) -> int:
            return 2

        def encode(self, texts, **_kwargs):
            return np.array([[1.0, 2.0] for _ in texts], dtype=np.float32)

    _install_fake_deps(monkeypatch, FakeModel)
    monkeypatch.setattr(
        sbert_mod,
        "get_cfg",
        lambda: _DummyCFG(
            {
                "embedder.sbert.model": "sentence-transformers/test-model",
                "embedder.sbert.device": "auto",
                "embedder.sbert.batch_size": 64,
            }
        ),
        raising=True,
    )

    sbert_mod.SbertEmbedder()

    assert seen["model_name"] == "sentence-transformers/test-model"
    assert seen["device"] == "cpu"


def test_dim_reads_model_dimension_without_probe(monkeypatch):
    calls = {"encode": 0}

    class FakeModel:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_embedding_dimension(self) -> int:
            return 7

        def get_sentence_embedding_dimension(self) -> int:
            raise AssertionError("deprecated dimension method was called")

        def encode(self, texts, **_kwargs):
            calls["encode"] += 1
            return np.array([[0.0] * 7 for _ in texts], dtype=np.float32)

    _install_fake_deps(monkeypatch, FakeModel)
    monkeypatch.setattr(
        sbert_mod,
        "get_cfg",
        lambda: _DummyCFG(
            {
                "embedder.sbert.model": "sentence-transformers/test-model",
                "embedder.sbert.device": "auto",
                "embedder.sbert.batch_size": 64,
            }
        ),
        raising=True,
    )

    emb = sbert_mod.SbertEmbedder()
    assert emb.dim == 7
    assert calls["encode"] == 0


def test_dim_probes_when_model_dimension_missing(monkeypatch):
    seen: dict[str, object] = {}

    class FakeModel:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_sentence_embedding_dimension(self) -> int:
            raise RuntimeError("dimension unavailable")

        def encode(self, texts, **_kwargs):
            seen["texts"] = list(texts)
            return np.array([[1.0, 2.0, 3.0]], dtype=np.float32)

    _install_fake_deps(monkeypatch, FakeModel)
    monkeypatch.setattr(
        sbert_mod,
        "get_cfg",
        lambda: _DummyCFG(
            {
                "embedder.sbert.model": "sentence-transformers/test-model",
                "embedder.sbert.device": "auto",
                "embedder.sbert.batch_size": 64,
            }
        ),
        raising=True,
    )

    emb = sbert_mod.SbertEmbedder()
    assert emb.dim == 3
    assert seen["texts"] == ["_"]


def test_explicit_model_overrides_cfg(monkeypatch):
    seen: dict[str, object] = {}

    class FakeModel:
        def __init__(self, model_name: str, *, device: str):
            seen["model_name"] = model_name
            seen["device"] = device

        def get_sentence_embedding_dimension(self) -> int:
            return 5

        def encode(self, texts, **_kwargs):
            return np.array([[1.0] * 5 for _ in texts], dtype=np.float32)

    _install_fake_deps(monkeypatch, FakeModel, cuda=True)
    monkeypatch.setattr(
        sbert_mod,
        "get_cfg",
        lambda: _DummyCFG(
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
