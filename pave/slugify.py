# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import re
import unicodedata


def slugify(value: str) -> str:
    """Return PaveDB's client-side collection slug for a human label."""
    raw = str(value or "").strip()
    normalized = unicodedata.normalize("NFKD", raw)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_text).strip("-").lower()
    return re.sub(r"-+", "-", slug)
