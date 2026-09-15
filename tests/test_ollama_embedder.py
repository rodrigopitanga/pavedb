# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import importlib

import pytest

import pave.embedders.factory as factory_mod
import pave.embedders.ollama as ollama_mod
from pave.embedders import EmbedderUnavailableError
from pave.embedders.worker_pool import ThreadEmbedderPool


class _DummyCFG:
    def __init__(self, values: dict[str, object]):
        self._values = values

    def get(self, key: str, default=None):
        return self._values.get(key, default)


def test_ollama_schema_matches_openai_shape():
    assert ollama_mod.CONFIG_KEYS == (
        "dim", "base_url", "batch_size", "timeout_s", "workers",
    )
    assert ollama_mod.SPACE_KEYS == ("dim",)


def test_ollama_vector_space_key_excludes_base_url():
    a = factory_mod.vector_space_key({
        "embedder_type": "ollama",
        "embed_model": "nomic-embed-text",
        "embedder_config": {"dim": 768, "base_url": "http://a/v1"},
    })
    b = factory_mod.vector_space_key({
        "embedder_type": "ollama",
        "embed_model": "nomic-embed-text",
        "embedder_config": {"dim": 768, "base_url": "http://b/v1"},
    })
    assert a == b == "ollama:nomic-embed-text:dim=768"


def test_ollama_probe_raises_embedder_unavailable_on_oserror(monkeypatch):
    """_probe_daemon turns urllib OSError into EmbedderUnavailableError."""
    def fail(*_a, **_kw):
        raise OSError("connection refused")

    monkeypatch.setattr(ollama_mod.urllib.request, "urlopen", fail)

    with pytest.raises(EmbedderUnavailableError) as exc:
        ollama_mod._probe_daemon("http://localhost:11434/v1")
    assert "Ollama daemon not reachable" in str(exc.value)


def test_ollama_dispatched_via_convention(monkeypatch):
    """Factory finds ollama module by name without explicit branches."""
    importlib.reload(factory_mod)

    seen: dict[str, object] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            seen.update(kwargs)
            self._dim = kwargs["dim"]

        @property
        def dim(self):
            return self._dim

    monkeypatch.setattr(ollama_mod, "OllamaEmbedder", FakeOpenAI, raising=True)
    monkeypatch.setattr(ollama_mod, "_probe_daemon", lambda _url: None)

    emb = factory_mod.get_embedder(
        _DummyCFG(
            {
                "embedder.default": "ollama",
                "embedder.instances": {
                    "ollama": {
                        "type": "ollama",
                        "model": "nomic-embed-text",
                        "config": {
                            "dim": 1024,
                            "base_url": "http://x/v1",
                        },
                    },
                },
            }
        ),
        embedder_type="ollama",
    )

    assert isinstance(emb, ThreadEmbedderPool)
    assert isinstance(emb._embedders[0], FakeOpenAI)
    assert seen["model"] == "nomic-embed-text"
    assert seen["base_url"] == "http://x/v1"
    assert seen["dim"] == 1024
    assert seen["api_key"] == "ollama"


def test_ollama_instance_api_key_overrides_dummy(monkeypatch):
    """A per-instance api_key replaces the 'ollama' placeholder."""
    importlib.reload(factory_mod)

    seen: dict[str, object] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            seen.update(kwargs)
            self._dim = kwargs["dim"]

        @property
        def dim(self):
            return self._dim

    monkeypatch.setattr(ollama_mod, "OllamaEmbedder", FakeOpenAI, raising=True)
    monkeypatch.setattr(ollama_mod, "_probe_daemon", lambda _url: None)

    factory_mod.get_embedder(
        _DummyCFG(
            {
                "embedder.default": "gateway",
                "embedder.instances": {
                    "gateway": {
                        "type": "ollama",
                        "model": "nomic-embed-text",
                        "api_key": "proxy-secret",
                        "config": {"dim": 1024, "base_url": "http://x/v1"},
                    },
                },
            }
        ),
        embedder_type="ollama",
    )

    assert seen["api_key"] == "proxy-secret"
