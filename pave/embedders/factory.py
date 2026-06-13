# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from collections.abc import Callable
import json
import sys
from threading import Lock
from typing import Any

from .base import Embedder
from ..config import CFG


def _embedder_schema(etype: str) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """Return (CONFIG_KEYS, SPACE_KEYS) for a registered embedder type.

    Lazily imports the embedder module so the heavy class body (sbert pulls
    torch, openai pulls the SDK) is not loaded at factory import time.
    """
    if etype == "sbert":
        from .sbert import CONFIG_KEYS, SPACE_KEYS
        return CONFIG_KEYS, SPACE_KEYS
    if etype == "openai":
        from .openai import CONFIG_KEYS, SPACE_KEYS
        return CONFIG_KEYS, SPACE_KEYS
    return None


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


def _setting(
    cfg: CFG,
    etype: str,
    key: str,
    spec: dict[str, Any],
    default: Any = None,
) -> Any:
    if key in spec and spec[key] is not None:
        return spec[key]
    return cfg.get(f"embedder.{etype}.{key}", default)


def resolve_embedder_spec(
    cfg: CFG = CFG,
    *,
    embedder_type: str | None = None,
    model: str | None = None,
    embedder_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    etype = str(
        embedder_type or cfg.get("embedder.type") or "sbert"
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

    resolved_config: dict[str, Any] = {}
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

    if etype == "openai":
        raw_dim = resolved_config.get("dim")
        if raw_dim in (None, ""):
            raise ValueError("embedder.openai.dim must be configured")
        try:
            dim = int(raw_dim)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "embedder.openai.dim must be an integer"
            ) from exc
        if dim <= 0:
            raise ValueError("embedder.openai.dim must be positive")
        resolved_config["dim"] = dim

    return {
        "embedder_type": etype,
        "embed_model": resolved_model,
        "embedder_config": resolved_config,
    }


def embedder_cache_key(spec: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(spec.get("embedder_type") or ""),
        str(spec.get("embed_model") or ""),
        json.dumps(
            spec.get("embedder_config") or {},
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _space_config(spec: dict[str, Any]) -> dict[str, Any]:
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


def embedder_space_key(spec: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(spec.get("embedder_type") or ""),
        str(spec.get("embed_model") or ""),
        json.dumps(
            _space_config(spec), sort_keys=True, separators=(",", ":")
        ),
    )


def vector_space_key(spec: dict[str, Any]) -> str:
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
    embedder_config: dict[str, Any] | None = None,
) -> Embedder:
    spec = dict(embedder_config or {})
    etype = str(
        embedder_type or cfg.get("embedder.type", "sbert") or "sbert"
    ).strip().lower()
    match etype:
        case "sbert":
            model_name = str(
                model if model is not None else cfg.get("embedder.sbert.model")
                or ""
            ).strip()
            if not model_name:
                raise RuntimeError("embedder.sbert.model must be configured")
            runtime_value = _setting(cfg, "sbert", "runtime", spec, None)
            if runtime_value is None:
                raise RuntimeError("embedder.sbert.runtime must be configured")
            runtime = str(runtime_value).lower()
            device = _setting(cfg, "sbert", "device", spec, None)
            batch_size = _setting(cfg, "sbert", "batch_size", spec, None)
            backend_type = str(cfg.get("vector_store.type", "faiss")).lower()
            if runtime == "process":
                from .sbert_worker import ProcessSbertEmbedder

                return ProcessSbertEmbedder(
                    model_name=model_name,
                    device=None if device is None else str(device),
                    batch_size=None if batch_size is None else int(batch_size),
                )
            if runtime in {"direct", "none"}:
                from .sbert import SbertEmbedder

                return SbertEmbedder(
                    model_name=model_name,
                    device=None if device is None else str(device),
                    batch_size=None if batch_size is None else int(batch_size),
                )
            if runtime != "auto":
                raise RuntimeError(f"Unknown embedder.sbert.runtime: {runtime}")
            if sys.platform == "darwin" and backend_type == "faiss":
                from .sbert_worker import ProcessSbertEmbedder

                return ProcessSbertEmbedder(
                    model_name=model_name,
                    device=None if device is None else str(device),
                    batch_size=None if batch_size is None else int(batch_size),
                )
            from .sbert import SbertEmbedder

            return SbertEmbedder(
                model_name=model_name,
                device=None if device is None else str(device),
                batch_size=None if batch_size is None else int(batch_size),
            )
        case "openai":
            from .openai import OpenAIEmbedder

            model_name = str(
                model
                if model is not None
                else cfg.get("embedder.openai.model")
                or ""
            ).strip()
            if not model_name:
                raise RuntimeError("embedder.openai.model must be configured")
            return OpenAIEmbedder(
                model=model_name,
                batch_size=_setting(cfg, "openai", "batch_size", spec, None),
                dim=_setting(cfg, "openai", "dim", spec, None),
                api_key=cfg.get("embedder.openai.api_key"),
                base_url=_setting(cfg, "openai", "base_url", spec, None),
            )
        case _:
            raise RuntimeError(f"Unknown embedder.type: {etype}")
