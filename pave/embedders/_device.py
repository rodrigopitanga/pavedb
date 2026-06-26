# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Device resolution for the native sentence-transformers embedder.

Shared by the in-process and subprocess SBERT paths. Guards the availability of
an explicitly requested `cuda`/`mps` device so it fails fast with an actionable
message instead of a raw torch traceback, and normalizes the `gpu` alias.
`auto` (the default) auto-detects. Anything else (including device indexes like
`cuda:1`, or other torch backends) passes through unchanged, as before.
"""

from __future__ import annotations

from typing import Any


def _mps_available(torch_mod: Any) -> bool:
    mps = getattr(torch_mod.backends, "mps", None)
    return mps is not None and bool(mps.is_available())


def resolve_device(raw_device: Any, torch_mod: Any) -> str:
    """Return a torch device string, or raise RuntimeError if it can't run."""
    device = str(raw_device or "auto").strip().lower()
    if device == "auto":
        if torch_mod.cuda.is_available():
            return "cuda"
        if _mps_available(torch_mod):
            return "mps"
        return "cpu"

    # Validate the base device (e.g. "cuda" in "cuda:1"); keep any index.
    base, sep, index = device.partition(":")
    if base == "gpu":  # torch has no "gpu"; a Mac GPU is mps, not cuda
        base = "cuda"
        device = base + sep + index
    if base == "cuda" and not torch_mod.cuda.is_available():
        raise RuntimeError(
            "embedder.sbert.device='cuda' but CUDA is unavailable — the "
            "installed torch build has no CUDA (CPU-only wheel) or no NVIDIA "
            "GPU is present. Use device=auto or cpu, or install a "
            "CUDA-enabled torch wheel."
        )
    if base == "mps" and not _mps_available(torch_mod):
        raise RuntimeError(
            "embedder.sbert.device='mps' but MPS is unavailable — Metal needs "
            "Apple Silicon and a macOS torch build. Use device=auto or cpu."
        )
    return device
