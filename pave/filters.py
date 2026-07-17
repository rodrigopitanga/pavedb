# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import operator
from datetime import datetime
from typing import Any

from pave.config import get_logger
from pave.stores.base import FilterSpec, Metadata, MetadataValidationError

log = get_logger()

_SQL_TRANS = str.maketrans({
    ";": " ",
    '"': " ",
    "`": " ",
    "\\": " ",
    "\x00": "",
})
_FILTER_MATCH_MAX_DEPTH = 10


def sanit_sql(value: Any, *, max_len: int | None = None) -> str:
    if value is None:
        return ""
    text = str(value).translate(_SQL_TRANS)
    for token in ("--", "/*", "*/"):
        if token in text:
            text = text.split(token, 1)[0]
    text = text.strip()
    if max_len is not None and max_len > 0 and len(text) > max_len:
        text = text[:max_len]
    return text.replace("'", "''")


def sanit_field(name: Any) -> str:
    """Sanitize a metadata field key.

    Keeps alphanumerics (Unicode-aware) and ``-``; every run of other
    characters — spaces, punctuation, and literal ``_`` — collapses to a
    single ``_``, with leading/trailing ``_`` trimmed. So ``"first name"``
    maps to ``"first_name"`` (through v0.9 the space was dropped, giving
    ``"firstname"``). Hyphens are preserved: ``"first-name"`` stays as-is.
    """
    if not isinstance(name, str):
        name = str(name)
    out: list[str] = []
    prev_us = False
    for ch in name:
        if ch.isalnum() or ch == "-":
            out.append(ch)
            prev_us = False
        elif not prev_us:  # space / punctuation / '_' -> one collapsed '_'
            out.append("_")
            prev_us = True
    return "".join(out).strip("_")


def lookup_meta(meta: Metadata | None, key: str) -> Any:
    if not meta:
        return None
    if key in meta:
        return meta.get(key)
    for raw_key, value in meta.items():
        if sanit_field(raw_key) == key:
            return value
    return None


def _sanit_meta_value(value: Any, *, path: str = "metadata") -> Any:
    if isinstance(value, dict):
        return sanit_meta_dict(value, path=path)
    if isinstance(value, (list, tuple, set)):
        return [
            _sanit_meta_value(v, path=f"{path}[{i}]")
            for i, v in enumerate(value)
        ]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return sanit_sql(value)


def sanit_meta_dict(
    meta: Metadata | None,
    *,
    path: str = "metadata",
) -> Metadata:
    safe: Metadata = {}
    if not isinstance(meta, dict):
        return safe
    seen: dict[str, str] = {}
    for raw_key, raw_value in meta.items():
        raw_key_text = raw_key if isinstance(raw_key, str) else str(raw_key)
        key_path = f"{path}.{raw_key_text}" if path else raw_key_text
        safe_key = sanit_field(raw_key)
        if not safe_key:
            raise MetadataValidationError(
                f"metadata key '{key_path}' sanitizes to empty string"
            )
        if safe_key == "text":
            raise MetadataValidationError(
                f"metadata key '{key_path}' sanitizes to reserved key 'text'"
            )
        prev_key_path = seen.get(safe_key)
        if prev_key_path is not None:
            raise MetadataValidationError(
                f"metadata keys '{prev_key_path}' and '{key_path}' "
                f"both sanitize to '{safe_key}'"
            )
        seen[safe_key] = key_path
        safe[safe_key] = _sanit_meta_value(raw_value, path=key_path)
    return safe


def matches_filters(
    m: Metadata,
    filters: FilterSpec | None,
) -> bool:
    if not filters:
        return True

    def match(have: Any, cond: Any, depth: int = 0) -> bool:
        if depth >= _FILTER_MATCH_MAX_DEPTH:
            log.warning(
                "Filter match depth limit (%s) exceeded for value: %s",
                _FILTER_MATCH_MAX_DEPTH,
                type(have),
            )
            return False

        if have is None:
            return False
        if isinstance(have, (list, tuple, set)):
            return any(match(item, cond, depth + 1) for item in have)
        if isinstance(cond, str):
            safe_cond = sanit_sql(cond)
        else:
            safe_cond = str(cond)
        have_text = str(have)
        ops = {
            ">=": operator.ge,
            "<=": operator.le,
            "!=": operator.ne,
            ">": operator.gt,
            "<": operator.lt,
        }
        for op_str, op_fn in ops.items():
            if safe_cond.startswith(op_str):
                value = safe_cond[len(op_str):].strip()
                try:
                    have_num, value_num = float(have), float(value)
                    return op_fn(have_num, value_num)
                except Exception:
                    try:
                        have_dt = datetime.fromisoformat(str(have))
                        value_dt = datetime.fromisoformat(value)
                        return op_fn(have_dt, value_dt)
                    except Exception:
                        if op_str == "!=":
                            return have_text != value
                        return False
        if safe_cond == "*":
            return True
        if safe_cond.startswith("*") and safe_cond.endswith("*"):
            return safe_cond[1:-1] in have_text
        if safe_cond.startswith("*"):
            return have_text.endswith(safe_cond[1:])
        if safe_cond.endswith("*"):
            return have_text.startswith(safe_cond[:-1])
        if safe_cond.startswith("!") and len(safe_cond) > 1:
            return have_text != safe_cond[1:]
        return have_text == safe_cond

    for key, vals in filters.items():
        if not any(match(lookup_meta(m, key), value) for value in vals):
            return False
    return True
