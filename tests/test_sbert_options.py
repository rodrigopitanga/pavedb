# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""P1-74: sbert trust_remote_code, task prefixes, remote runtime."""

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
from pave.embedders.base import EmbedderUnavailableError  # noqa: E402
from pave.embedders.factory import vector_space_key  # noqa: E402


def _install_fake_deps(monkeypatch, fake_model):
    fake_st = types.ModuleType("sentence_transformers")
    fake_st.SentenceTransformer = fake_model
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)

    fake_torch = types.ModuleType("torch")
    fake_torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    fake_torch.backends = types.SimpleNamespace(
        mps=types.SimpleNamespace(is_available=lambda: False)
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)


class _RecordingModel:
    """Fake SentenceTransformer recording ctor kwargs and encoded texts."""

    last_init: dict[str, object] = {}
    last_texts: list[str] = []

    def __init__(self, model_name: str, *, device: str, **kwargs):
        type(self).last_init = {"model_name": model_name, **kwargs}

    def get_sentence_embedding_dimension(self) -> int:
        return 2

    def encode(self, texts, **_kwargs):
        type(self).last_texts = list(texts)
        return np.zeros((len(texts), 2), dtype=np.float32)


def test_trust_remote_code_is_forwarded_only_when_enabled(monkeypatch):
    _install_fake_deps(monkeypatch, _RecordingModel)

    sbert_mod.SbertEmbedder(
        model_name="m", device="cpu", trust_remote_code=True
    )
    assert _RecordingModel.last_init.get("trust_remote_code") is True

    sbert_mod.SbertEmbedder(model_name="m", device="cpu")
    assert "trust_remote_code" not in _RecordingModel.last_init


def test_prefixes_apply_per_side_and_default_to_passthrough(monkeypatch):
    _install_fake_deps(monkeypatch, _RecordingModel)
    emb = sbert_mod.SbertEmbedder(
        model_name="m",
        device="cpu",
        query_prefix="query: ",
        passage_prefix="passage: ",
    )
    emb.encode(["doc"])
    assert _RecordingModel.last_texts == ["passage: doc"]
    emb.encode_queries(["ask"])
    assert _RecordingModel.last_texts == ["query: ask"]

    plain = sbert_mod.SbertEmbedder(model_name="m", device="cpu")
    plain.encode_queries(["ask"])
    assert _RecordingModel.last_texts == ["ask"]


def test_prefixes_participate_in_vector_space_key():
    base = {
        "embedder_type": "sbert",
        "embed_model": "m",
        "embedder_config": {},
    }
    prefixed = {
        "embedder_type": "sbert",
        "embed_model": "m",
        "embedder_config": {
            "query_prefix": "query: ",
            "passage_prefix": "passage: ",
            # Execution details must NOT alter the space identity.
            "endpoint": "http://gpu:8080",
            "trust_remote_code": True,
            "runtime": "remote",
        },
    }
    assert vector_space_key(base) == "sbert:m:"
    key = vector_space_key(prefixed)
    assert "query_prefix=query: " in key
    assert "passage_prefix=passage: " in key
    assert "endpoint" not in key
    assert "trust_remote_code" not in key
    assert "runtime" not in key


class _FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def test_remote_runtime_posts_prefixed_inputs(monkeypatch):
    calls: list[dict] = []

    class _HTTPError(Exception):
        pass

    fake_httpx = types.ModuleType("httpx")
    fake_httpx.HTTPError = _HTTPError
    fake_httpx.post = lambda url, json, timeout: (
        calls.append({"url": url, "json": json}),
        _FakeResponse(200, [[0.0, 1.0]] * len(json["inputs"])),
    )[1]
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)

    emb = sbert_mod.RemoteSbertEmbedder(
        endpoint="http://gpu:8080/",
        model_name="m",
        query_prefix="query: ",
        passage_prefix="passage: ",
    )
    out = emb.encode(["doc"])
    assert out.shape == (1, 2)
    assert calls[-1]["url"] == "http://gpu:8080/embed"
    assert calls[-1]["json"] == {"inputs": ["passage: doc"]}
    emb.encode_queries(["ask"])
    assert calls[-1]["json"] == {"inputs": ["query: ask"]}
    assert emb.dim == 2


def test_remote_runtime_maps_failures_to_unavailable(monkeypatch):
    class _HTTPError(Exception):
        pass

    fake_httpx = types.ModuleType("httpx")
    fake_httpx.HTTPError = _HTTPError

    def _boom(url, json, timeout):
        raise _HTTPError("connect refused")

    fake_httpx.post = _boom
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)

    emb = sbert_mod.RemoteSbertEmbedder(endpoint="http://gpu:8080", model_name="m")
    with pytest.raises(EmbedderUnavailableError):
        emb._post(["x"])

    fake_httpx.post = lambda url, json, timeout: _FakeResponse(503, "down")
    with pytest.raises(EmbedderUnavailableError):
        emb._post(["x"])


def test_build_embedder_remote_requires_endpoint(monkeypatch):
    _install_fake_deps(monkeypatch, _RecordingModel)

    class _CFG:
        def __init__(self, values):
            self._values = values

        def get(self, key, default=None):
            return self._values.get(key, default)

    cfg = _CFG({
        "embedder.sbert.model": "m",
        "embedder.sbert.runtime": "remote",
        "vector_store.type": "faiss",
    })
    with pytest.raises(RuntimeError, match="endpoint"):
        sbert_mod.build_embedder(cfg)

    cfg = _CFG({
        "embedder.sbert.model": "m",
        "embedder.sbert.runtime": "remote",
        "embedder.sbert.endpoint": "http://gpu:8080",
        "vector_store.type": "faiss",
    })
    emb = sbert_mod.build_embedder(cfg)
    assert isinstance(emb, sbert_mod.RemoteSbertEmbedder)
