# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
from threading import Lock
from typing import Any, TypedDict

from .base import Embedder, EmbedderConfig
from ..config import CFG


class EmbedderSpec(TypedDict):
    embedder_type: str
    embed_model: str
    embedder_config: EmbedderConfig


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


class LazyEmbedder:
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

    def encode(self, texts: list[str]):
        return self._get().encode(texts)


def resolve_setting(
    cfg: CFG,
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
    return cfg.get(f"embedder.{etype}.{key}", default)


def resolve_embedder_spec(
    cfg: CFG = CFG,
    *,
    embedder_type: str | None = None,
    model: str | None = None,
    embedder_config: Mapping[str, Any] | None = None,
) -> EmbedderSpec:
    etype = str(
        embedder_type or cfg.get("embedder.type")
    ).strip().lower()
    schema = _embedder_schema(etype)
    if schema is None:
        raise ValueError(f"unknown embedder type: {etype}")
    allowed_keys, _space_keys = schema

    resolved_model = model
    if resolved_model is None:
        resolved_model = cfg.get(f"embedder.{etype}.model")
    resolved_model = str(resolved_model or "").strip()
    if not resolved_model:
        raise ValueError(f"no model configured for embedder.{etype}.model")

    resolved_config: EmbedderConfig = {}
    for key in allowed_keys:
        val = cfg.get(f"embedder.{etype}.{key}")
        if val is not None and val != "":
            resolved_config[key] = val
    for key, val in (embedder_config or {}).items():
        key = str(key)
        if key not in allowed_keys:
            raise ValueError(
                f"unknown embedder config key for {etype}: {key}"
            )
        if val is not None and val != "":
            resolved_config[key] = val

    mod = _embedder_module(etype)
    validate = getattr(mod, "validate_spec", None) if mod is not None else None
    if validate is not None:
        resolved_config = validate(resolved_config)

    return {
        "embedder_type": etype,
        "embed_model": resolved_model,
        "embedder_config": resolved_config,
    }


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


def embedder_space_key(spec: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(spec.get("embedder_type") or ""),
        str(spec.get("embed_model") or ""),
        json.dumps(
            _space_config(spec), sort_keys=True, separators=(",", ":")
        ),
    )


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


def get_embedder(
    cfg: CFG = CFG,
    *,
    embedder_type: str | None = None,
    model: str | None = None,
    embedder_config: Mapping[str, Any] | None = None,
) -> Embedder:
    etype = str(
        embedder_type or cfg.get("embedder.type")
    ).strip().lower()
    mod = _embedder_module(etype)
    if mod is None:
        raise RuntimeError(f"Unknown embedder.type: {etype}")
    build = getattr(mod, "build_embedder", None)
    if build is None:
        raise RuntimeError(
            f"pave.embedders.{etype} does not expose build_embedder(cfg, ...)"
        )
    return build(cfg, model=model, embedder_config=embedder_config or {})
