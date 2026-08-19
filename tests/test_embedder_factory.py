# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import importlib
import logging
import sys
import types

import pytest

import pave.embedders.factory as factory_mod
import pave.embedders.openai as openai_mod

_DEFAULT_SBERT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


class _DummyCFG:
    def __init__(self, values: dict[str, object]):
        self._values = values

    def get(self, key: str, default=None):
        return self._values.get(key, default)


def _reload_factory():
    return importlib.reload(factory_mod)


def _registry_cfg(
    *,
    default="native",
    runtime="auto",
    model=_DEFAULT_SBERT_MODEL,
    store="faiss",
    config=None,
    instances=None,
):
    active_config = {"runtime": runtime}
    if config:
        active_config.update(config)
    return _DummyCFG(
        {
            "embedder.default": default,
            "embedder.instances": instances or {
                "native": {
                    "type": "sbert",
                    "model": model,
                    "config": active_config,
                },
            },
            "vector_store.type": store,
        }
    )


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


def test_lazy_embedder_defers_factory_until_used():
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


def _patch_sbert_class(monkeypatch, fake_cls):
    sbert_mod = importlib.import_module("pave.embedders.sbert")
    monkeypatch.setattr(sbert_mod, "SbertEmbedder", fake_cls, raising=True)


def _patch_openai_class(monkeypatch, fake_cls):
    openai_mod = importlib.import_module("pave.embedders.openai")
    monkeypatch.setattr(openai_mod, "OpenAIEmbedder", fake_cls, raising=True)


def _patch_platform(monkeypatch, platform):
    sbert_mod = importlib.import_module("pave.embedders.sbert")
    monkeypatch.setattr(sbert_mod.sys, "platform", platform)


def test_factory_uses_process_mode_when_explicitly_requested(monkeypatch):
    factory = _reload_factory()

    class FakeProcessSbert:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    _patch_platform(monkeypatch, "linux")
    _fake_module(
        monkeypatch,
        "pave.embedders.sbert_worker",
        ProcessSbertEmbedder=FakeProcessSbert,
    )

    emb = factory.get_embedder(
        _registry_cfg(
            runtime="process",
            store="qdrant",
            config={"workers": 3},
        )
    )

    assert isinstance(emb, FakeProcessSbert)
    assert emb.kwargs["model_name"] == "sentence-transformers/all-MiniLM-L6-v2"
    assert emb.kwargs["workers"] == 3


@pytest.mark.parametrize(
    ("platform", "store"),
    [("darwin", "qdrant"), ("linux", "faiss")],
)
def test_factory_allows_direct_sbert_outside_darwin_faiss(
    monkeypatch,
    platform,
    store,
):
    factory = _reload_factory()

    class FakeSbert:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    _patch_platform(monkeypatch, platform)
    _patch_sbert_class(monkeypatch, FakeSbert)

    emb = factory.get_embedder(
        _registry_cfg(runtime="direct", store=store)
    )

    assert isinstance(emb, FakeSbert)


@pytest.mark.parametrize("runtime", ["direct", "none"])
def test_factory_rejects_in_process_sbert_on_darwin_faiss(
    monkeypatch,
    runtime,
):
    factory = _reload_factory()

    _patch_platform(monkeypatch, "darwin")

    with pytest.raises(RuntimeError, match="runtime=direct.*FAISS"):
        factory.get_embedder(
            _registry_cfg(runtime=runtime, store="faiss")
        )


def test_factory_uses_process_sbert_for_auto_mode_on_darwin_faiss(
    monkeypatch,
):
    factory = _reload_factory()

    class FakeProcessSbert:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    _patch_platform(monkeypatch, "darwin")
    _fake_module(
        monkeypatch,
        "pave.embedders.sbert_worker",
        ProcessSbertEmbedder=FakeProcessSbert,
    )

    emb = factory.get_embedder(
        _registry_cfg(runtime="auto")
    )

    assert isinstance(emb, FakeProcessSbert)


def test_factory_uses_direct_sbert_for_auto_mode_off_darwin(
    monkeypatch,
):
    factory = _reload_factory()

    class FakeSbert:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    _patch_platform(monkeypatch, "linux")
    _patch_sbert_class(monkeypatch, FakeSbert)

    emb = factory.get_embedder(
        _registry_cfg(runtime="auto")
    )

    assert isinstance(emb, FakeSbert)


def test_factory_passes_explicit_sbert_spec(monkeypatch):
    factory = _reload_factory()
    seen: dict[str, object] = {}

    class FakeSbert:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    _patch_platform(monkeypatch, "linux")
    _patch_sbert_class(monkeypatch, FakeSbert)

    factory.get_embedder(
        _registry_cfg(),
        embedder_type="sbert",
        model="sentence-transformers/custom",
        embedder_config={"batch_size": 7, "device": "cpu"},
    )

    assert seen == {
        "model_name": "sentence-transformers/custom",
        "device": "cpu",
        "batch_size": 7,
        "trust_remote_code": False,
        "query_prefix": None,
        "passage_prefix": None,
    }


def test_factory_passes_explicit_openai_spec(monkeypatch):
    factory = _reload_factory()
    seen: dict[str, object] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    _patch_openai_class(monkeypatch, FakeOpenAI)

    cfg = _registry_cfg(
        default="frontier",
        instances={
            "frontier": {
                "type": "openai",
                "model": "bge-m3",
                "api_key": "instance-key",
                "config": {"dim": 1024},
            },
        },
    )

    factory.get_embedder(
        cfg,
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
        "api_key": "instance-key",
        "base_url": "http://localhost:11434/v1",
        "timeout_s": openai_mod._DEFAULT_TIMEOUT_S,
    }


def test_embedder_space_key_ignores_sbert_runtime_knobs():
    factory = _reload_factory()

    first = factory.embedder_space_key(
        {
            "embedder_type": "sbert",
            "embed_model": "sentence-transformers/all-MiniLM-L6-v2",
            "embedder_config": {
                "runtime": "direct",
                "device": "cpu",
                "batch_size": 8,
                "workers": 1,
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
                "workers": 4,
            },
        }
    )

    assert first == second


def test_embedder_space_key_includes_openai_vector_shape():
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


def test_resolve_openai_spec_requires_dim():
    factory = _reload_factory()

    with pytest.raises(ValueError, match="embedder.openai.dim"):
        factory.resolve_embedder_spec(
            _DummyCFG({}),
            embedder_type="openai",
            model="bge-m3",
        )


def test_embedder_space_key_ignores_openai_base_url():
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


def test_vector_space_key_format():
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


def test_vector_space_key_unknown_type_is_conservative():
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


def test_factory_rejects_unknown_sbert_mode():
    factory = _reload_factory()

    with pytest.raises(RuntimeError, match="Unknown embedder.sbert.runtime"):
        factory.get_embedder(
            _registry_cfg(runtime="mystery")
        )


def test_default_selector_resolves_compatible_instance_pool():
    factory = _reload_factory()
    cfg = _registry_cfg(
        default="ollama",
        instances={
            "ollama-a": {
                "type": "ollama",
                "model": "nomic-embed-text",
                "config": {"dim": 768, "base_url": "http://a/v1"},
            },
            "ollama-b": {
                "type": "ollama",
                "model": "nomic-embed-text",
                "config": {"dim": 768, "base_url": "http://b/v1"},
            },
        },
    )

    resolved = factory.resolve_default_embedder(cfg)

    assert resolved.selector == "ollama"
    assert resolved.vector_key == "ollama:nomic-embed-text:dim=768"
    assert [instance.key for instance in resolved.instances] == [
        "ollama-a",
        "ollama-b",
    ]
    assert factory.runtime_embedder_spec_for_vector(
        cfg,
        {
            "embedder_type": "ollama",
            "embed_model": "nomic-embed-text",
            "embedder_config": {"dim": 768},
        },
    ) == {
        "embedder_type": "ollama",
        "embed_model": "nomic-embed-text",
        "embedder_config": {
            "dim": 768,
            "base_url": "http://a/v1",
        },
    }


def test_default_selector_accepts_user_defined_instance_key():
    factory = _reload_factory()
    cfg = _registry_cfg(
        default="frontier-affordable",
        instances={
            "frontier-affordable": {
                "type": "openai",
                "model": "text-embedding-3-small",
                "config": {"dim": 1536, "base_url": "http://a/v1"},
            },
            "frontier-backup": {
                "type": "openai",
                "model": "text-embedding-3-small",
                "config": {"dim": 1536, "base_url": "http://b/v1"},
            },
        },
    )

    resolved = factory.resolve_default_embedder(cfg)

    assert resolved.selector == "frontier-affordable"
    assert resolved.vector_key == "openai:text-embedding-3-small:dim=1536"
    assert [instance.key for instance in resolved.instances] == [
        "frontier-affordable",
        "frontier-backup",
    ]


def test_default_selector_ambiguous_hint_logs_useful_error(caplog):
    factory = _reload_factory()
    cfg = _registry_cfg(
        default="ollama",
        instances={
            "ollama-a": {
                "type": "ollama",
                "model": "nomic-embed-text",
                "config": {"dim": 768, "base_url": "http://a/v1"},
            },
            "ollama-b": {
                "type": "ollama",
                "model": "bge-m3",
                "config": {"dim": 1024, "base_url": "http://b/v1"},
            },
        },
    )

    with caplog.at_level(logging.CRITICAL, logger="pave.embedders.factory"):
        with pytest.raises(factory.EmbedderRegistryError) as exc:
            factory.resolve_default_embedder(cfg)

    message = str(exc.value)
    assert "embedder.default='ollama' is ambiguous" in message
    assert "ollama-a=ollama:nomic-embed-text:dim=768" in message
    assert "ollama-b=ollama:bge-m3:dim=1024" in message
    assert "Use a full vector key or a more specific selector" in message
    assert any(record.levelno == logging.CRITICAL for record in caplog.records)


def test_missing_default_with_multiple_instances_logs_actionable_error(caplog):
    factory = _reload_factory()
    cfg = _registry_cfg(
        default=None,
        instances={
            "ollama-a": {
                "type": "ollama",
                "model": "nomic-embed-text",
                "config": {"dim": 768, "base_url": "http://a/v1"},
            },
            "ollama-b": {
                "type": "ollama",
                "model": "nomic-embed-text",
                "config": {"dim": 768, "base_url": "http://b/v1"},
            },
        },
    )

    with caplog.at_level(logging.CRITICAL, logger="pave.embedders.factory"):
        with pytest.raises(factory.EmbedderRegistryError) as exc:
            factory.resolve_default_embedder(cfg)

    assert "embedder.default is required" in str(exc.value)
    assert "ollama-a=ollama:nomic-embed-text:dim=768" in str(exc.value)
    assert "ollama-b=ollama:nomic-embed-text:dim=768" in str(exc.value)
    assert any(record.levelno == logging.CRITICAL for record in caplog.records)


def test_single_configured_instance_can_be_default_without_selector():
    factory = _reload_factory()
    cfg = _registry_cfg(default=None)

    resolved = factory.resolve_default_embedder(cfg)

    assert resolved.selector is None
    assert resolved.vector_key == (
        "sbert:sentence-transformers/all-MiniLM-L6-v2:"
    )


def test_persistable_embedder_spec_strips_operational_config():
    factory = _reload_factory()

    spec = factory.persistable_embedder_spec(
        {
            "embedder_type": "ollama",
            "embed_model": "nomic-embed-text",
            "embedder_config": {
                "dim": 768,
                "base_url": "http://operator-box/v1",
                "batch_size": 32,
            },
        }
    )

    assert spec == {
        "embedder_type": "ollama",
        "embed_model": "nomic-embed-text",
        "embedder_config": {"dim": 768},
    }


def test_runtime_resolution_raises_embedder_unavailable_when_no_instance_encodes():
    factory = _reload_factory()
    cfg = _registry_cfg()  # one sbert instance only

    with pytest.raises(factory.EmbedderUnavailableError) as exc:
        factory.runtime_embedder_spec_for_vector(
            cfg,
            {
                "embedder_type": "ollama",
                "embed_model": "nomic-embed-text",
                "embedder_config": {"dim": 768},
            },
        )

    # Must not be a ValueError, or the service ingest path would mislabel it
    # as invalid_csv_options instead of embedder_unavailable.
    assert not isinstance(exc.value, ValueError)
    assert "ollama:nomic-embed-text:dim=768" in str(exc.value)


def test_runtime_resolution_raises_embedder_unavailable_when_type_fallback_ambiguous():
    factory = _reload_factory()
    cfg = _registry_cfg(
        default="ollama",
        instances={
            "ollama-a": {
                "type": "ollama",
                "model": "nomic-embed-text",
                "config": {"dim": 768, "base_url": "http://a/v1"},
            },
            "ollama-b": {
                "type": "ollama",
                "model": "bge-m3",
                "config": {"dim": 1024, "base_url": "http://b/v1"},
            },
        },
    )

    with pytest.raises(factory.EmbedderUnavailableError) as exc:
        factory.runtime_embedder_spec_for_vector(
            cfg,
            {
                "embedder_type": "ollama",
                "embed_model": "mxbai-embed-large",
                "embedder_config": {"dim": 512},
            },
        )

    assert not isinstance(exc.value, ValueError)
    assert "ambiguous" in str(exc.value)


def _openai_instance_cfg(api_key=None):
    instance = {
        "type": "openai",
        "model": "text-embedding-3-small",
        "config": {"dim": 1536},
    }
    if api_key is not None:
        instance["api_key"] = api_key
    return _registry_cfg(default="frontier", instances={"frontier": instance})


def test_instance_api_key_is_sibling_not_part_of_vector_space():
    factory = _reload_factory()
    cfg = _openai_instance_cfg(api_key="sk-secret")

    (instance,) = factory.embedder_instances(cfg)

    assert instance.spec["api_key"] == "sk-secret"
    # The credential rides alongside the spec but never inside the config bag,
    # never in the persisted catalog row, never in the vector-space key.
    assert "api_key" not in instance.spec["embedder_config"]
    assert "api_key" not in factory.persistable_embedder_spec(instance.spec)[
        "embedder_config"
    ]
    assert instance.vector_key == "openai:text-embedding-3-small:dim=1536"
    assert "sk-secret" not in instance.vector_key


def test_resolve_embedder_spec_surfaces_instance_api_key():
    factory = _reload_factory()
    cfg = _openai_instance_cfg(api_key="sk-secret")

    spec = factory.resolve_embedder_spec(
        cfg,
        embedder_type="openai",
        model="text-embedding-3-small",
        embedder_config={"dim": 1536},
    )

    assert spec["api_key"] == "sk-secret"
    assert "api_key" not in spec["embedder_config"]


def test_resolve_embedder_spec_omits_api_key_when_instance_has_none():
    factory = _reload_factory()
    cfg = _openai_instance_cfg(api_key=None)

    spec = factory.resolve_embedder_spec(
        cfg,
        embedder_type="openai",
        model="text-embedding-3-small",
        embedder_config={"dim": 1536},
    )

    # Absent → key is not present, so the openai SDK applies OPENAI_API_KEY.
    assert "api_key" not in spec


def test_api_key_inside_config_is_rejected():
    factory = _reload_factory()
    cfg = _registry_cfg(
        default="frontier",
        instances={
            "frontier": {
                "type": "openai",
                "model": "text-embedding-3-small",
                "config": {"dim": 1536, "api_key": "sk-secret"},
            },
        },
    )

    with pytest.raises(factory.EmbedderRegistryError) as exc:
        factory.embedder_instances(cfg)

    assert "api_key" in str(exc.value)


def test_non_string_instance_api_key_is_rejected():
    factory = _reload_factory()
    cfg = _openai_instance_cfg(api_key=1234)

    with pytest.raises(factory.EmbedderRegistryError) as exc:
        factory.embedder_instances(cfg)

    assert "api_key must be a string" in str(exc.value)


def _effective_trust(factory, cfg, model):
    """What build_embedder really sees: persisted -> runtime -> resolved.

    Asserting on resolve_embedder_spec alone proves nothing -- the store
    builds from runtime_embedder_spec_for_vector, and the persisted config is
    discarded by persistable_embedder_spec on the way.
    """
    persisted = factory.resolve_embedder_spec(cfg, model=model)
    runtime = factory.runtime_embedder_spec_for_vector(cfg, persisted)
    return factory.resolve_setting(
        cfg,
        "sbert",
        "trust_remote_code",
        runtime.get("embedder_config", {}),
        False,
    )


def test_foreign_model_does_not_inherit_trust_remote_code():
    """A tenant-named model must not execute under the operator's grant."""
    factory = _reload_factory()
    cfg = _registry_cfg(config={"trust_remote_code": True})

    assert _effective_trust(factory, cfg, "attacker/evil-hf-repo") is False


def test_configured_model_still_inherits_trust_remote_code():
    """The operator's own model keeps the grant they configured for it."""
    factory = _reload_factory()
    cfg = _registry_cfg(config={"trust_remote_code": True})

    assert _effective_trust(factory, cfg, _DEFAULT_SBERT_MODEL) is True


def test_foreign_model_denies_grant_explicitly_not_by_absence():
    """Deleting the key would let embedder.<type>.<key> re-grant it."""
    factory = _reload_factory()
    cfg = _registry_cfg(config={"trust_remote_code": True})

    persisted = factory.resolve_embedder_spec(cfg, model="attacker/evil")
    runtime = factory.runtime_embedder_spec_for_vector(cfg, persisted)

    assert runtime["embedder_config"]["trust_remote_code"] is False


@pytest.mark.parametrize("etype", ["xml.sax", ".relative", "os"])
def test_unshipped_embedder_type_is_rejected_without_importing(
    monkeypatch,
    etype,
):
    """A caller-named module must never become an import in this process."""
    factory = _reload_factory()
    imported: list[str] = []
    real_import = importlib.import_module

    def _spy(name, package=None):
        imported.append(name)
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", _spy)

    with pytest.raises(ValueError) as exc:
        factory.resolve_embedder_spec(
            _registry_cfg(),
            embedder_type=etype,
            model="whatever",
        )

    assert "unknown embedder type" in str(exc.value)
    assert imported == []


def _effective_api_key(factory, cfg, model):
    """What build_embedder really sees: persisted -> runtime -> resolved.

    The store re-resolves the runtime spec through resolve_embedder_spec
    (stores/local.py:339, 1095), so that last resolution is where the
    credential would land.
    """
    persisted = factory.resolve_embedder_spec(
        cfg,
        embedder_type="openai",
        model=model,
        embedder_config={"dim": 1536},
    )
    runtime = factory.runtime_embedder_spec_for_vector(
        cfg,
        factory.persistable_embedder_spec(persisted),
    )
    built = factory.resolve_embedder_spec(
        cfg,
        embedder_type=str(runtime["embedder_type"]),
        model=str(runtime["embed_model"]),
        embedder_config=runtime.get("embedder_config") or {},
    )
    return built.get("api_key")


@pytest.mark.parametrize("api_key", [None, "sk-secret"])
def test_foreign_openai_model_is_rejected_before_env_fallback(
    monkeypatch,
    api_key,
):
    """A tenant-named model must not reach any operator credential."""
    factory = _reload_factory()
    cfg = _openai_instance_cfg(api_key=api_key)
    monkeypatch.setenv("OPENAI_API_KEY", "operator-env-key")

    with pytest.raises(ValueError, match="openai model .* is not configured"):
        factory.get_embedder(
            cfg,
            embedder_type="openai",
            model="tenant/expensive",
            embedder_config={"dim": 1536},
        )


def test_unconfigured_openai_type_cannot_use_operator_env_key(monkeypatch):
    factory = _reload_factory()
    monkeypatch.setenv("OPENAI_API_KEY", "operator-env-key")

    with pytest.raises(ValueError, match="openai model .* is not configured"):
        factory.get_embedder(
            _registry_cfg(),
            embedder_type="openai",
            model="tenant/expensive",
            embedder_config={"dim": 1536},
        )


def test_configured_model_still_gets_instance_api_key():
    """The operator's own model keeps the credential they configured."""
    factory = _reload_factory()
    cfg = _openai_instance_cfg(api_key="sk-secret")
    key = _effective_api_key(factory, cfg, "text-embedding-3-small")

    assert key == "sk-secret"


def test_configured_openai_model_still_uses_environment_key(monkeypatch):
    factory = _reload_factory()
    cfg = _openai_instance_cfg(api_key=None)
    monkeypatch.setenv("OPENAI_API_KEY", "operator-env-key")
    monkeypatch.delenv("OPENAI_ADMIN_KEY", raising=False)

    embedder = factory.get_embedder(
        cfg,
        embedder_type="openai",
        model="text-embedding-3-small",
        embedder_config={"dim": 1536},
    )

    assert embedder.client.api_key == "operator-env-key"


def test_openai_client_bounds_the_total_call_budget(monkeypatch):
    """A per-attempt timeout is not a budget: the SDK retries timeouts."""
    import types

    import pave.embedders.openai as openai_mod

    captured = {}

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake = types.ModuleType("openai")
    fake.OpenAI = _FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake)

    emb = openai_mod.OpenAIEmbedder(
        model="text-embedding-3-small", dim=8, api_key="sk-x", timeout_s=30.0
    )
    emb._client_or_raise() if hasattr(emb, "_client_or_raise") else emb.client

    attempts = openai_mod._MAX_RETRIES + 1
    assert captured["max_retries"] == openai_mod._MAX_RETRIES
    assert captured["timeout"] * attempts == 30.0, captured
