# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Ollama embedder.

Thin `OpenAIEmbedder` adapter pointed at a local Ollama daemon's
OpenAI-compatible endpoint. Adds a daemon-up probe so an unreachable
daemon surfaces as `EmbedderUnavailableError` at construction time
rather than at first encode.
"""

from __future__ import annotations

import urllib.error
import urllib.request

from .base import EmbedderUnavailableError
from .openai import _DEFAULT_TIMEOUT_S, OpenAIEmbedder


CONFIG_KEYS: tuple[str, ...] = ("dim", "base_url", "batch_size", "timeout_s")
SPACE_KEYS: tuple[str, ...] = ("dim",)

_DEFAULT_BASE_URL = "http://localhost:11434/v1"
_DEFAULT_MODEL = "nomic-embed-text"
_DEFAULT_DIM = 768
_PROBE_TIMEOUT_S = 1.5


def validate_spec(resolved_config: dict) -> dict:
    from .openai import validate_spec as _openai_validate
    return _openai_validate(resolved_config)


def build_embedder(cfg, *, model=None, embedder_config=None, api_key=None):
    from .factory import resolve_setting

    spec = dict(embedder_config or {})
    resolved_model = str(
        model if model is not None else cfg.get("embedder.ollama.model") or _DEFAULT_MODEL
    ).strip()
    base_url = str(
        resolve_setting(cfg, "ollama", "base_url", spec, _DEFAULT_BASE_URL)
    )
    batch_size = resolve_setting(cfg, "ollama", "batch_size", spec, None)
    dim = resolve_setting(cfg, "ollama", "dim", spec, _DEFAULT_DIM)
    # Resolved under the ollama key, not inherited from embedder.openai.*, so a
    # global openai budget cannot silently retime a local daemon.
    timeout_s = resolve_setting(
        cfg, "ollama", "timeout_s", spec, _DEFAULT_TIMEOUT_S
    )
    # Ollama's OpenAI-compatible endpoint ignores the key but the SDK requires
    # a non-empty one; a per-instance api_key overrides the dummy default.
    resolved_api_key = api_key if api_key else "ollama"

    _probe_daemon(base_url)

    return OllamaEmbedder(
        model=resolved_model,
        batch_size=batch_size,
        dim=dim,
        api_key=resolved_api_key,
        base_url=base_url,
        timeout_s=timeout_s,
    )


def _probe_daemon(base_url: str) -> None:
    """Cheap reachability check against Ollama's `/api/tags` endpoint."""
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    probe_url = f"{root}/api/tags"
    try:
        urllib.request.urlopen(probe_url, timeout=_PROBE_TIMEOUT_S).close()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise EmbedderUnavailableError(
            f"Ollama daemon not reachable at {base_url}: {exc}"
        ) from exc


class OllamaEmbedder(OpenAIEmbedder):
    """OpenAI-compatible client pointed at a local Ollama daemon."""
