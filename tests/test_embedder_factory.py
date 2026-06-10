# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import importlib

import pytest

import pave.embedders.factory as factory_mod


class _DummyCFG:
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def get(self, key: str, default=None):
        return self._values.get(key, default)


def _reload_factory():
    return importlib.reload(factory_mod)


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
        pass

    monkeypatch.setattr(factory.sys, "platform", "linux")
    import pave.embedders.sbert_worker as worker_mod

    monkeypatch.setattr(worker_mod, "ProcessSbertEmbedder", FakeProcessSbert)

    emb = factory.get_embedder(
        _DummyCFG(
            {
                "embedder.type": "sbert",
                "embedder.sbert.runtime": "process",
                "vector_store.type": "qdrant",
            }
        )
    )

    assert isinstance(emb, FakeProcessSbert)


def test_factory_uses_none_mode_for_direct_sbert(monkeypatch) -> None:
    factory = _reload_factory()

    class FakeSbert:
        pass

    monkeypatch.setattr(factory.sys, "platform", "darwin")
    import pave.embedders.sbert as sbert_mod

    monkeypatch.setattr(sbert_mod, "SbertEmbedder", FakeSbert)

    emb = factory.get_embedder(
        _DummyCFG(
            {
                "embedder.type": "sbert",
                "embedder.sbert.runtime": "direct",
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
        pass

    monkeypatch.setattr(factory.sys, "platform", "darwin")
    import pave.embedders.sbert_worker as worker_mod

    monkeypatch.setattr(worker_mod, "ProcessSbertEmbedder", FakeProcessSbert)

    emb = factory.get_embedder(
        _DummyCFG(
            {
                "embedder.type": "sbert",
                "embedder.sbert.runtime": "auto",
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
        pass

    monkeypatch.setattr(factory.sys, "platform", "linux")
    import pave.embedders.sbert as sbert_mod

    monkeypatch.setattr(sbert_mod, "SbertEmbedder", FakeSbert)

    emb = factory.get_embedder(
        _DummyCFG(
            {
                "embedder.type": "sbert",
                "embedder.sbert.runtime": "auto",
                "vector_store.type": "faiss",
            }
        )
    )

    assert isinstance(emb, FakeSbert)


def test_factory_rejects_unknown_sbert_mode() -> None:
    factory = _reload_factory()

    with pytest.raises(RuntimeError, match="Unknown embedder.sbert.runtime"):
        factory.get_embedder(
            _DummyCFG(
                {
                    "embedder.type": "sbert",
                    "embedder.sbert.runtime": "mystery",
                }
            )
        )
