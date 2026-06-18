# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import numpy as np

from ..config import CFG


CONFIG_KEYS: tuple[str, ...] = ("dim", "base_url", "batch_size")
SPACE_KEYS: tuple[str, ...] = ("dim",)


def validate_spec(resolved_config: dict) -> dict:
    """Enforce openai-specific config invariants at create-collection time.

    Called by `factory.resolve_embedder_spec`; mutates and returns the
    coerced config so the catalog row stores the int form of `dim`.
    """
    raw_dim = resolved_config.get("dim")
    if raw_dim in (None, ""):
        raise ValueError("embedder.openai.dim must be configured")
    try:
        dim = int(raw_dim)
    except (TypeError, ValueError) as exc:
        raise ValueError("embedder.openai.dim must be an integer") from exc
    if dim <= 0:
        raise ValueError("embedder.openai.dim must be positive")
    resolved_config["dim"] = dim
    return resolved_config


def build_embedder(cfg, *, model=None, embedder_config=None, api_key=None):
    """Construct an OpenAIEmbedder using cfg/spec resolution.

    `api_key` comes from the embedder instance (``embedder.instances.<name>.
    api_key``). When omitted, the OpenAI SDK applies its native
    ``OPENAI_API_KEY`` environment fallback.
    """
    from .factory import resolve_setting

    spec = dict(embedder_config or {})
    model_name = str(
        model if model is not None else cfg.get("embedder.openai.model") or ""
    ).strip()
    if not model_name:
        raise RuntimeError("embedder.openai.model must be configured")
    return OpenAIEmbedder(
        model=model_name,
        batch_size=resolve_setting(cfg, "openai", "batch_size", spec, None),
        dim=resolve_setting(cfg, "openai", "dim", spec, None),
        api_key=api_key,
        base_url=resolve_setting(cfg, "openai", "base_url", spec, None),
    )


class OpenAIEmbedder:
    def __init__(
        self,
        *,
        model: str | None = None,
        batch_size: int | None = None,
        dim: int | str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        from openai import OpenAI

        self.model = str(
            model if model is not None else CFG.get("embedder.openai.model")
            or ""
        ).strip()
        if not self.model:
            raise RuntimeError("embedder.openai.model must be configured")
        if batch_size is None:
            batch_size = CFG.get("embedder.openai.batch_size")
        self.batch_size = int(batch_size if batch_size is not None else 256)
        self._dim = dim if dim is not None else CFG.get("embedder.openai.dim")
        # No explicit api_key → let the OpenAI SDK apply its own
        # OPENAI_API_KEY env fallback (and raise if neither is present).
        client_kwargs: dict = {}
        if api_key:
            client_kwargs["api_key"] = str(api_key)
        if base_url is None:
            base_url = CFG.get("embedder.openai.base_url")
        resolved_base_url = str(base_url if base_url is not None else "").strip()
        if resolved_base_url:
            client_kwargs["base_url"] = resolved_base_url
        self.client = OpenAI(**client_kwargs)

    @property
    def dim(self) -> int:
        try:
            if self._dim is None:
                raise RuntimeError(
                    "embedder.openai.dim must be configured for OpenAIEmbedder"
                )
            return int(self._dim)
        except Exception:
            raise RuntimeError(
                "embedder.openai.dim must be configured for OpenAIEmbedder"
            )

    def encode(self, texts: list[str]) -> np.ndarray:
        kwargs = {"model": self.model, "input": texts}
        if self._dim is not None:
            kwargs["dimensions"] = int(self._dim)
        res = self.client.embeddings.create(**kwargs)
        return np.array([d.embedding for d in res.data], dtype=np.float32)
