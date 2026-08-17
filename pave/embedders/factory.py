# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Embedder registry and instance factory.

Drives convention-based dispatch (`pave.embedders.<type>`) plus the
configured-instances registry introduced by P1-58: parsing
`embedder.instances` from config, resolving the `embedder.default`
selector, computing the `vector_space_key` used for cross-collection
compatibility, and stripping credentials before persistence. Heavy
embedder modules stay deferred until a specific instance is built.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import logging
from threading import Lock
from typing import Any, Iterable, NotRequired, TypedDict

from .base import Embedder, EmbedderConfig, EmbedderUnavailableError
from ..config import Config, get_cfg

log = logging.getLogger(__name__)


class EmbedderSpec(TypedDict):
    """Canonical embedder spec persisted on a collection.

    `embedder_type` + `embed_model` + `embedder_config` together
    fully identify the runtime to build. `api_key` is carried
    alongside but never participates in vector-space identity; it
    is stripped before persistence and ignored by
    `vector_space_key`.
    """
    embedder_type: str
    embed_model: str
    embedder_config: EmbedderConfig
    # Credential carried alongside the vector-space identity, never part of it.
    # Stripped by persistable_embedder_spec; ignored by vector_space_key.
    api_key: NotRequired[str]


@dataclass(frozen=True)
class EmbedderInstance:
    """Configured embedder instance from `embedder.instances`.

    `key` is the operator-facing name; `vector_key` is the
    deterministic vector-space identifier; `spec` is the persistable
    shape used by collections that adopt this instance.
    """
    key: str
    spec: EmbedderSpec
    vector_key: str


@dataclass(frozen=True)
class DefaultEmbedderResolution:
    """Result of resolving `embedder.default` against the registry.

    `selector` echoes the configured selector (or `None`); `vector_key`
    is the resolved vector space; `instances` is the ordered list of
    configured instances that match (first one wins for routing).
    """
    selector: str | None
    vector_key: str
    instances: tuple[EmbedderInstance, ...]


class EmbedderRegistryError(ValueError):
    """Raised when configured embedder instances cannot be resolved."""


def _embedder_module(etype: str):
    """Import the embedder module by convention `pave.embedders.<etype>`.

    Returns the module on success, or None if no such module exists. The
    module's heavy class body remains deferred until the class itself is
    referenced.
    """
    import importlib

    try:
        if "." in etype:
            return importlib.import_module(etype)
        return importlib.import_module(f"pave.embedders.{etype}")
    except ModuleNotFoundError:
        return None


def _embedder_schema(etype: str) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """Return (CONFIG_KEYS, SPACE_KEYS) for a registered embedder type."""
    mod = _embedder_module(etype)
    if mod is None:
        return None
    return (
        getattr(mod, "CONFIG_KEYS", ()),
        getattr(mod, "SPACE_KEYS", ()),
    )


def _is_modelless(etype: str) -> bool:
    """True for embedder types that own no model (e.g. identity/BYOV).

    Such types pin their vector space via `embedder_config` alone, so the
    "no model configured" requirement in spec resolution is skipped for them.
    """
    mod = _embedder_module(etype)
    return bool(getattr(mod, "MODELLESS", False)) if mod is not None else False


class LazyEmbedder:
    """Defers embedder construction until first `encode` / `dim` use.

    Keeps server startup, library `connect()`, and test fixtures
    cheap when no embedding is actually needed. Thread-safe: the
    first caller through pays the construction cost; subsequent
    callers see the cached instance.
    """
    def __init__(self, factory: Callable[[], Embedder]) -> None:
        self._factory = factory
        self._embedder: Embedder | None = None
        self._lock = Lock()

    def _get(self) -> Embedder:
        if self._embedder is None:
            with self._lock:
                if self._embedder is None:
                    self._embedder = self._factory()
        return self._embedder

    @property
    def dim(self) -> int:
        return self._get().dim

    def warm(self) -> None:
        embedder = self._get()
        warm = getattr(embedder, "warm", None)
        if callable(warm):
            warm()
            return
        embedder.encode(["warmup"])

    def encode(self, texts: list[str]):
        return self._get().encode(texts)


def resolve_setting(
    cfg: Config,
    etype: str,
    key: str,
    spec: Mapping[str, Any],
    default: Any = None,
) -> Any:
    """Read an embedder config field from spec first, falling back to global cfg.

    Exposed so per-embedder ``build_embedder`` functions can share the
    spec-then-config resolution rule without duplicating it.
    """
    if key in spec and spec[key] is not None:
        return spec[key]
    value = cfg.get(f"embedder.{etype}.{key}")
    return default if value is None else value


def _registry_error(message: str) -> EmbedderRegistryError:
    log.critical("Invalid embedder registry: %s", message)
    return EmbedderRegistryError(message)


def _allowed_config_keys(etype: str) -> tuple[str, ...]:
    schema = _embedder_schema(etype)
    if schema is None:
        raise ValueError(f"unknown embedder type: {etype}")
    return schema[0]


def _validate_embedder_config_keys(
    etype: str,
    config: Mapping[str, Any],
) -> None:
    allowed = set(_allowed_config_keys(etype))
    for key in config:
        if str(key) not in allowed:
            raise ValueError(f"unknown embedder config key for {etype}: {key}")


def _validate_embedder_spec(spec: EmbedderSpec) -> EmbedderSpec:
    etype = str(spec["embedder_type"])
    mod = _embedder_module(etype)
    validate = getattr(mod, "validate_spec", None) if mod is not None else None
    if validate is None:
        return spec
    return {
        "embedder_type": spec["embedder_type"],
        "embed_model": spec["embed_model"],
        "embedder_config": validate(dict(spec.get("embedder_config") or {})),
    }


def _spec_from_parts(
    *,
    etype: str,
    model: str,
    config: Mapping[str, Any] | None = None,
) -> EmbedderSpec:
    etype = str(etype or "").strip().lower()
    if not etype:
        raise ValueError("embedder type is required")
    model = str(model or "").strip()
    if not model and not _is_modelless(etype):
        raise ValueError(f"no model configured for embedder.{etype}.model")
    cfg: EmbedderConfig = {
        str(key): val
        for key, val in dict(config or {}).items()
        if val is not None and val != ""
    }
    _validate_embedder_config_keys(etype, cfg)
    return _validate_embedder_spec({
        "embedder_type": etype,
        "embed_model": model,
        "embedder_config": cfg,
    })


def _space_config(spec: Mapping[str, Any]) -> EmbedderConfig:
    etype = str(spec.get("embedder_type") or "")
    cfg = dict(spec.get("embedder_config") or {})
    schema = _embedder_schema(etype)
    # Conservative fallback for unknown types: include every config key.
    space_keys = schema[1] if schema is not None else tuple(sorted(cfg))
    return {
        key: cfg[key]
        for key in space_keys
        if key in cfg and cfg[key] not in (None, "")
    }


def persistable_embedder_spec(spec: Mapping[str, Any]) -> EmbedderSpec:
    """Return the vector-space identity that belongs in collection catalog."""
    return {
        "embedder_type": str(spec.get("embedder_type") or ""),
        "embed_model": str(spec.get("embed_model") or ""),
        "embedder_config": _space_config(spec),
    }


def vector_space_key(spec: Mapping[str, Any]) -> str:
    """Stable, human-readable opaque key identifying the vector space.

    Two collections with identical `vector_space_key` are guaranteed to
    produce embeddings in the same space. Used by SDKs and the cross-collection
    merge check.
    """
    etype = str(spec.get("embedder_type") or "")
    model = str(spec.get("embed_model") or "")
    cfg = _space_config(spec)
    cfg_part = ",".join(f"{k}={cfg[k]}" for k in sorted(cfg))
    return f"{etype}:{model}:{cfg_part}"


def _instance_from_config(key: str, raw: Mapping[str, Any]) -> EmbedderInstance:
    if not isinstance(raw, Mapping):
        raise _registry_error(
            f"embedder.instances.{key} must be a mapping with type, model, "
            "and optional config"
        )
    config = raw.get("config") or {}
    if not isinstance(config, Mapping):
        raise _registry_error(
            f"embedder.instances.{key}.config must be a mapping"
        )
    try:
        spec = _spec_from_parts(
            etype=str(raw.get("type") or ""),
            model=str(raw.get("model") or ""),
            config=config,
        )
    except ValueError as exc:
        raise _registry_error(
            f"embedder.instances.{key} is invalid: {exc}"
        ) from exc
    api_key = raw.get("api_key")
    if api_key is not None:
        if not isinstance(api_key, str):
            raise _registry_error(
                f"embedder.instances.{key}.api_key must be a string"
            )
        if api_key:
            spec["api_key"] = api_key
    return EmbedderInstance(
        key=key,
        spec=spec,
        vector_key=vector_space_key(spec),
    )


def embedder_instances(cfg: Config | None = None) -> tuple[EmbedderInstance, ...]:
    cfg = cfg or get_cfg()
    raw_instances = cfg.get("embedder.instances") or {}
    if not isinstance(raw_instances, Mapping):
        raise _registry_error("embedder.instances must be a mapping")
    if not raw_instances:
        raise _registry_error(
            "embedder.instances must define at least one instance, for "
            "example embedder.instances.native"
        )
    return tuple(
        _instance_from_config(str(key), raw)
        for key, raw in raw_instances.items()
    )


def _selector_matches(selector: str, instance: EmbedderInstance) -> bool:
    if selector == instance.vector_key:
        return True
    parts = selector.split(":")
    etype = str(instance.spec["embedder_type"])
    model = str(instance.spec["embed_model"])
    if len(parts) == 1:
        return selector == etype
    if len(parts) == 2:
        return parts[0] == etype and parts[1] == model
    return False


def _format_instance(instance: EmbedderInstance) -> str:
    return f"{instance.key}={instance.vector_key}"


def _format_instances(instances: Iterable[EmbedderInstance]) -> str:
    return ", ".join(_format_instance(instance) for instance in instances)


def _configured_instances(cfg: Config) -> tuple[EmbedderInstance, ...]:
    raw_instances = cfg.get("embedder.instances")
    if not raw_instances:
        return ()
    return embedder_instances(cfg)


def _instance_for_selector(
    cfg: Config,
    selector: str,
) -> EmbedderInstance | None:
    matched = [
        instance for instance in _configured_instances(cfg)
        if selector == instance.key or _selector_matches(selector, instance)
    ]
    if not matched:
        return None
    matched_keys = sorted({instance.vector_key for instance in matched})
    if len(matched_keys) > 1:
        matches = ", ".join(_format_instance(i) for i in matched)
        raise ValueError(
            f"embedder selector {selector!r} is ambiguous; it matches "
            f"vector spaces {', '.join(matched_keys)} via {matches}. "
            "Use a full vector key or a more specific selector."
        )
    return matched[0]


def resolve_default_embedder(
    cfg: Config | None = None,
) -> DefaultEmbedderResolution:
    cfg = cfg or get_cfg()
    instances = embedder_instances(cfg)
    raw_selector = cfg.get("embedder.default")
    selector = (
        None
        if raw_selector is None or str(raw_selector).strip() == ""
        else str(raw_selector).strip()
    )
    vector_keys = sorted({instance.vector_key for instance in instances})
    if selector is None:
        if len(instances) == 1:
            selected = vector_keys[0]
        else:
            raise _registry_error(
                "embedder.default is required when more than one embedder "
                f"instance is configured. Instances: "
                f"{_format_instances(instances)}. Use an instance key, "
                "type, type:model, or full vector key."
            )
    else:
        by_key = {instance.key: instance for instance in instances}
        if selector in by_key:
            selected = by_key[selector].vector_key
        else:
            matched = [
                instance for instance in instances
                if _selector_matches(selector, instance)
            ]
            matched_keys = sorted({instance.vector_key for instance in matched})
            if not matched:
                raise _registry_error(
                    f"embedder.default={selector!r} did not match any "
                    "configured instance. Available instances: "
                    f"{_format_instances(instances)}"
                )
            if len(matched_keys) > 1:
                raise _registry_error(
                    f"embedder.default={selector!r} is ambiguous; it matches "
                    f"vector spaces {', '.join(matched_keys)} via "
                    f"{_format_instances(matched)}. "
                    "Use a full vector key or a more specific selector."
                )
            selected = matched_keys[0]

    pool = tuple(
        instance for instance in instances if instance.vector_key == selected
    )
    return DefaultEmbedderResolution(
        selector=selector,
        vector_key=selected,
        instances=pool,
    )


def _deny_execution_grant(config: dict[str, Any]) -> None:
    """Withhold the operator's execution grant from a model they never named.

    Sets False rather than deleting the key: `resolve_setting` reads an absent
    key as "fall back to embedder.<type>.<key>", so a pop would let a
    type-level grant re-apply to the foreign model -- and where the instance
    sets an explicit False, popping would *grant* what the operator denied.
    """
    config["trust_remote_code"] = False


def runtime_embedder_spec_for_vector(
    cfg: Config,
    spec: Mapping[str, Any],
) -> EmbedderSpec:
    etype = str(spec.get("embedder_type") or "")
    if _is_modelless(etype):
        # Self-sufficient: the vector space is fully defined by embedder_config
        # (identity reads `dim` from it), so there is no configured instance to
        # match — the spec is its own runtime spec.
        return {
            "embedder_type": etype,
            "embed_model": str(spec.get("embed_model") or ""),
            "embedder_config": dict(spec.get("embedder_config") or {}),
        }
    vector_key = vector_space_key(spec)
    instances = embedder_instances(cfg)
    for instance in instances:
        if instance.vector_key == vector_key:
            return instance.spec
    same_type = [
        instance for instance in instances
        if str(instance.spec["embedder_type"]) == etype
    ]
    if len(same_type) == 1:
        runtime_config = dict(same_type[0].spec.get("embedder_config") or {})
        runtime_config.update(dict(spec.get("embedder_config") or {}))
        # This is the spec the store actually builds the embedder from, so the
        # grant has to be withheld here, not only where the spec is persisted.
        instance_model = str(same_type[0].spec.get("embed_model") or "").strip()
        if instance_model != str(spec.get("embed_model") or "").strip():
            _deny_execution_grant(runtime_config)
        return {
            "embedder_type": etype,
            "embed_model": str(spec.get("embed_model") or ""),
            "embedder_config": runtime_config,
        }
    if len(same_type) > 1:
        raise EmbedderUnavailableError(
            f"no configured embedder instance exactly matches vector space "
            f"{vector_key}, and type fallback for {etype!r} is ambiguous via "
            f"{_format_instances(same_type)}"
        )
    op_config = dict(spec.get("embedder_config") or {})
    if op_config != _space_config(spec):
        return {
            "embedder_type": str(spec.get("embedder_type") or ""),
            "embed_model": str(spec.get("embed_model") or ""),
            "embedder_config": op_config,
        }
    raise EmbedderUnavailableError(
        f"no configured embedder instance can encode vector space {vector_key}"
    )


def resolve_embedder_spec(
    cfg: Config | None = None,
    *,
    embedder_type: str | None = None,
    model: str | None = None,
    embedder_config: Mapping[str, Any] | None = None,
) -> EmbedderSpec:
    cfg = cfg or get_cfg()
    if embedder_type is None and model is None and embedder_config is None:
        return resolve_default_embedder(cfg).instances[0].spec

    default_spec: EmbedderSpec | None = None
    if embedder_type is None:
        default_spec = resolve_default_embedder(cfg).instances[0].spec
        etype = str(default_spec["embedder_type"])
    else:
        etype = str(embedder_type).strip().lower()
    schema = _embedder_schema(etype)
    if schema is None:
        raise ValueError(f"unknown embedder type: {etype}")
    allowed_keys, _space_keys = schema
    _validate_embedder_config_keys(etype, embedder_config or {})

    selector = etype if model is None else f"{etype}:{model}"
    matched_instance = None
    if model is not None and embedder_config:
        candidate_vector_key = vector_space_key(
            {
                "embedder_type": etype,
                "embed_model": model,
                "embedder_config": embedder_config,
            }
        )
        matched_instance = _instance_for_selector(cfg, candidate_vector_key)
    if matched_instance is None:
        matched_instance = _instance_for_selector(cfg, selector)
    if matched_instance is None and model is not None:
        type_match = _instance_for_selector(cfg, etype)
        if type_match is not None:
            matched_instance = type_match

    resolved_model = model
    if resolved_model is None:
        if matched_instance is not None:
            resolved_model = matched_instance.spec["embed_model"]
        elif default_spec is not None:
            resolved_model = default_spec["embed_model"]
    resolved_model = str(resolved_model or "").strip()
    if not resolved_model and not _is_modelless(etype):
        raise ValueError(f"no model configured for embedder type {etype!r}")
    # Per-collection models are a supported feature, but the operator granted
    # trust_remote_code to the model they configured -- not to an arbitrary
    # repository a tenant names on create, which would execute its code here.
    instance_model = (
        str(matched_instance.spec.get("embed_model") or "").strip()
        if matched_instance is not None
        else ""
    )
    foreign_model = matched_instance is not None and instance_model != resolved_model

    resolved_config: EmbedderConfig = {}
    if matched_instance is not None:
        resolved_config.update(matched_instance.spec.get("embedder_config") or {})
    for key, val in (embedder_config or {}).items():
        key = str(key)
        if key not in allowed_keys:
            raise ValueError(f"unknown embedder config key for {etype}: {key}")
        if val is not None and val != "":
            resolved_config[key] = val
    # After the caller's own config, so neither source can re-grant it.
    if foreign_model:
        _deny_execution_grant(resolved_config)

    spec = _spec_from_parts(
        etype=etype,
        model=resolved_model,
        config=resolved_config,
    )
    if matched_instance is not None and "api_key" in matched_instance.spec:
        spec["api_key"] = matched_instance.spec["api_key"]
    return spec


def embedder_cache_key(spec: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(spec.get("embedder_type") or ""),
        str(spec.get("embed_model") or ""),
        json.dumps(
            spec.get("embedder_config") or {},
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def embedder_space_key(spec: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(spec.get("embedder_type") or ""),
        str(spec.get("embed_model") or ""),
        json.dumps(
            _space_config(spec), sort_keys=True, separators=(",", ":")
        ),
    )


def get_embedder(
    cfg: Config | None = None,
    *,
    embedder_type: str | None = None,
    model: str | None = None,
    embedder_config: Mapping[str, Any] | None = None,
) -> Embedder:
    cfg = cfg or get_cfg()
    spec = resolve_embedder_spec(
        cfg,
        embedder_type=embedder_type,
        model=model,
        embedder_config=embedder_config,
    )
    etype = str(spec["embedder_type"])
    mod = _embedder_module(etype)
    if mod is None:
        raise RuntimeError(f"Unknown embedder.type: {etype}")
    build = getattr(mod, "build_embedder", None)
    if build is None:
        raise RuntimeError(
            f"pave.embedders.{etype} does not expose build_embedder(cfg, ...)"
        )
    return build(
        cfg,
        model=spec["embed_model"],
        embedder_config=spec.get("embedder_config") or {},
        api_key=spec.get("api_key"),
    )
