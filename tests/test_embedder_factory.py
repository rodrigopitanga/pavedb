# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import importlib
import sys
import types

import pytest

import pave.embedders.factory as factory_mod

_DEFAULT_SBERT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


class _DummyCFG:
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def get(self, key: str, default=None):
        return self._values.get(key, default)


def _reload_factory():
    return importlib.reload(factory_mod)


def _fake_module(monkeypatch, name: str, **attrs):
    module = types.ModuleType(name)
    for attr, value in attrs.items():
        setattr(module, attr, value)
    monkeypatch.setitem(sys.modules, name, module)
    pkg_name, _, attr_name = name.rpartition(".")
    if pkg_name:
        pkg = importlib.import_module(pkg_name)
        monkeypatch.setattr(pkg, attr_name, module, raising=False)
    return module


def test_lazy_embedder_defers_factory_until_used() -> None:
    factory = _reload_factory()
    calls = {"factory": 0}

    class FakeEmbedder:
        @property
        def dim(self) -> int:
            return 3

        def encode(self, texts: list[str]):
            return texts

    def make_embedder():
        calls["factory"] += 1
        return FakeEmbedder()

    emb = factory.LazyEmbedder(make_embedder)

    assert calls["factory"] == 0
    assert emb.dim == 3
    assert emb.encode(["a"]) == ["a"]
    assert calls["factory"] == 1


def test_factory_uses_process_mode_when_explicitly_requested(monkeypatch) -> None:
    factory = _reload_factory()

    class FakeProcessSbert:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    monkeypatch.setattr(factory.sys, "platform", "linux")
    _fake_module(
        monkeypatch,
        "pave.embedders.sbert_worker",
        ProcessSbertEmbedder=FakeProcessSbert,
    )

    emb = factory.get_embedder(
        _DummyCFG(
            {
                "embedder.type": "sbert",
                "embedder.sbert.runtime": "process",
                "embedder.sbert.model": _DEFAULT_SBERT_MODEL,
                "vector_store.type": "qdrant",
            }
        )
    )

    assert isinstance(emb, FakeProcessSbert)
    assert emb.kwargs["model_name"] == "sentence-transformers/all-MiniLM-L6-v2"


def test_factory_uses_none_mode_for_direct_sbert(monkeypatch) -> None:
    factory = _reload_factory()

    class FakeSbert:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    monkeypatch.setattr(factory.sys, "platform", "darwin")
    _fake_module(
        monkeypatch,
        "pave.embedders.sbert",
        SbertEmbedder=FakeSbert,
    )

    emb = factory.get_embedder(
        _DummyCFG(
            {
                "embedder.type": "sbert",
                "embedder.sbert.runtime": "direct",
                "embedder.sbert.model": _DEFAULT_SBERT_MODEL,
                "vector_store.type": "faiss",
            }
        )
    )

    assert isinstance(emb, FakeSbert)


def test_factory_uses_process_sbert_for_auto_mode_on_darwin_faiss(
    monkeypatch,
) -> None:
    factory = _reload_factory()

    class FakeProcessSbert:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    monkeypatch.setattr(factory.sys, "platform", "darwin")
    _fake_module(
        monkeypatch,
        "pave.embedders.sbert_worker",
        ProcessSbertEmbedder=FakeProcessSbert,
    )

    emb = factory.get_embedder(
        _DummyCFG(
            {
                "embedder.type": "sbert",
                "embedder.sbert.runtime": "auto",
                "embedder.sbert.model": _DEFAULT_SBERT_MODEL,
                "vector_store.type": "faiss",
            }
        )
    )

    assert isinstance(emb, FakeProcessSbert)


def test_factory_uses_direct_sbert_for_auto_mode_off_darwin(
    monkeypatch,
) -> None:
    factory = _reload_factory()

    class FakeSbert:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    monkeypatch.setattr(factory.sys, "platform", "linux")
    _fake_module(
        monkeypatch,
        "pave.embedders.sbert",
        SbertEmbedder=FakeSbert,
    )

    emb = factory.get_embedder(
        _DummyCFG(
            {
                "embedder.type": "sbert",
                "embedder.sbert.runtime": "auto",
                "embedder.sbert.model": _DEFAULT_SBERT_MODEL,
                "vector_store.type": "faiss",
            }
        )
    )

    assert isinstance(emb, FakeSbert)


def test_factory_passes_explicit_sbert_spec(monkeypatch) -> None:
    factory = _reload_factory()
    seen: dict[str, object] = {}

    class FakeSbert:
        def __init__(self, **kwargs) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(factory.sys, "platform", "linux")
    _fake_module(
        monkeypatch,
        "pave.embedders.sbert",
        SbertEmbedder=FakeSbert,
    )

    factory.get_embedder(
        _DummyCFG(
            {
                "embedder.type": "sbert",
                "embedder.sbert.runtime": "direct",
                "vector_store.type": "faiss",
            }
        ),
        embedder_type="sbert",
        model="sentence-transformers/custom",
        embedder_config={"batch_size": 7, "device": "cpu"},
    )

    assert seen == {
        "model_name": "sentence-transformers/custom",
        "device": "cpu",
        "batch_size": 7,
    }


def test_factory_passes_explicit_openai_spec(monkeypatch) -> None:
    factory = _reload_factory()
    seen: dict[str, object] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs) -> None:
            seen.update(kwargs)

    _fake_module(
        monkeypatch,
        "pave.embedders.openai",
        OpenAIEmbedder=FakeOpenAI,
    )

    factory.get_embedder(
        _DummyCFG({
            "embedder.type": "openai",
            "embedder.openai.api_key": "global-key",
        }),
        embedder_type="openai",
        model="bge-m3",
        embedder_config={
            "dim": 1024,
            "base_url": "http://localhost:11434/v1",
        },
    )

    assert seen == {
        "model": "bge-m3",
        "batch_size": None,
        "dim": 1024,
        "api_key": "global-key",
        "base_url": "http://localhost:11434/v1",
    }


def test_embedder_space_key_ignores_sbert_runtime_knobs() -> None:
    factory = _reload_factory()

    first = factory.embedder_space_key(
        {
            "embedder_type": "sbert",
            "embed_model": "sentence-transformers/all-MiniLM-L6-v2",
            "embedder_config": {
                "runtime": "direct",
                "device": "cpu",
                "batch_size": 8,
            },
        }
    )
    second = factory.embedder_space_key(
        {
            "embedder_type": "sbert",
            "embed_model": "sentence-transformers/all-MiniLM-L6-v2",
            "embedder_config": {
                "runtime": "process",
                "device": "mps",
                "batch_size": 64,
            },
        }
    )

    assert first == second


def test_embedder_space_key_includes_openai_vector_shape() -> None:
    factory = _reload_factory()

    first = factory.embedder_space_key(
        {
            "embedder_type": "openai",
            "embed_model": "bge-m3",
            "embedder_config": {"dim": 1024, "batch_size": 32},
        }
    )
    second = factory.embedder_space_key(
        {
            "embedder_type": "openai",
            "embed_model": "bge-m3",
            "embedder_config": {"dim": 1536, "batch_size": 32},
        }
    )

    assert first != second


def test_resolve_openai_spec_requires_dim() -> None:
    factory = _reload_factory()

    with pytest.raises(ValueError, match="embedder.openai.dim"):
        factory.resolve_embedder_spec(
            _DummyCFG({"embedder.type": "openai"}),
            embedder_type="openai",
            model="bge-m3",
        )


def test_embedder_space_key_ignores_openai_base_url() -> None:
    """base_url is operational, not geometric — same vector space."""
    factory = _reload_factory()

    first = factory.embedder_space_key(
        {
            "embedder_type": "openai",
            "embed_model": "bge-m3",
            "embedder_config": {"dim": 1024, "base_url": "http://a/v1"},
        }
    )
    second = factory.embedder_space_key(
        {
            "embedder_type": "openai",
            "embed_model": "bge-m3",
            "embedder_config": {"dim": 1024, "base_url": "http://b/v1"},
        }
    )

    assert first == second


def test_vector_space_key_format() -> None:
    factory = _reload_factory()

    sbert_key = factory.vector_space_key(
        {
            "embedder_type": "sbert",
            "embed_model": "sentence-transformers/all-MiniLM-L6-v2",
            "embedder_config": {"runtime": "direct", "batch_size": 64},
        }
    )
    openai_key = factory.vector_space_key(
        {
            "embedder_type": "openai",
            "embed_model": "bge-m3",
            "embedder_config": {"dim": 1024, "base_url": "http://x/v1"},
        }
    )

    assert sbert_key == "sbert:sentence-transformers/all-MiniLM-L6-v2:"
    assert openai_key == "openai:bge-m3:dim=1024"


def test_vector_space_key_unknown_type_is_conservative() -> None:
    """Unknown embedder types include every config key in the space."""
    factory = _reload_factory()

    a = factory.vector_space_key(
        {
            "embedder_type": "cohere",
            "embed_model": "embed-v3",
            "embedder_config": {"dim": 1024, "batch_size": 32},
        }
    )
    b = factory.vector_space_key(
        {
            "embedder_type": "cohere",
            "embed_model": "embed-v3",
            "embedder_config": {"dim": 1024, "batch_size": 64},
        }
    )
    assert a != b


def test_factory_rejects_unknown_sbert_mode() -> None:
    factory = _reload_factory()

    with pytest.raises(RuntimeError, match="Unknown embedder.sbert.runtime"):
        factory.get_embedder(
            _DummyCFG(
                {
                    "embedder.type": "sbert",
                    "embedder.sbert.runtime": "mystery",
                    "embedder.sbert.model": _DEFAULT_SBERT_MODEL,
                }
            )
        )
