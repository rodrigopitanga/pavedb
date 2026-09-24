# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations
import io, csv, mimetypes
from .config import get_cfg
from .config.config import CHUNK_OVERLAP_RATIO, chunking_pair_error
from collections.abc import Iterable, Iterator
from typing import Any
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from pave.stores.base import Metadata

CsvOptions = dict[str, Any]
Chunking = dict[str, Any]


class PreprocessError(ValueError):
    """Client-supplied document content cannot be preprocessed."""


class ChunkingError(ValueError):
    """A chunking spec or text the operator bounds refuse (P1-78)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def resolve_chunking(spec: Chunking | None) -> Chunking:
    """Complete a collection's chunking spec from the operator defaults and
    check it against the operator bounds."""
    bounds = get_cfg().get("chunking")
    spec = dict(spec or {})
    strategy = spec.pop("strategy", None) or bounds["default_strategy"]
    if strategy not in bounds["strategies"]:
        raise ChunkingError(
            "invalid_chunking",
            f"chunking strategy {strategy!r} is not enabled on this instance",
        )
    if strategy == "none":
        if spec:
            raise ChunkingError(
                "invalid_chunking", "strategy none takes no size or overlap"
            )
        return {"strategy": "none"}
    size = spec.pop("size", bounds["default_size"])
    overlap = spec.pop("overlap", None)
    if overlap is None:
        overlap = bounds["default_overlap"]
        if isinstance(size, int) and not isinstance(size, bool):
            # A chosen size keeps the default overlap only while it fits.
            overlap = min(overlap, size // CHUNK_OVERLAP_RATIO)
    if spec:
        raise ChunkingError(
            "invalid_chunking", f"unknown chunking keys: {sorted(spec)}"
        )
    problem = chunking_pair_error(size, overlap, bounds)
    if problem:
        raise ChunkingError("invalid_chunking", problem)
    return {"strategy": "fixed", "size": size, "overlap": overlap}


def _text_chunks(
    text: str, chunking: Chunking
) -> Iterator[tuple[str, str, Metadata]]:
    text = text or ""
    if chunking["strategy"] == "none":
        limit = get_cfg().get("chunking.max_size")
        if len(text) > limit:
            raise ChunkingError(
                "chunk_too_large",
                f"text of {len(text)} characters exceeds chunking.max_size "
                f"{limit}; chunk it before ingesting",
            )
        if text:
            yield "chunk_0", text, {"offset": 0}
        return
    size, overlap = chunking["size"], chunking["overlap"]
    step = size - overlap
    for i, start in enumerate(range(0, len(text), step)):
        yield f"chunk_{i}", text[start : start + size], {"offset": start}

def _csv_parse_col_spec(spec: str) -> tuple[list[str], list[int]]:
    names: list[str] = []
    idxs: list[int] = []
    if not spec:
        return names, idxs
    for tok in (t.strip() for t in spec.split(",") if t.strip()):
        if tok.isdigit():
            i = int(tok)
            if i <= 0:
                raise ValueError("CSV column indices are 1-based")
            idxs.append(i - 1)
        else:
            names.append(tok)
    return names, idxs

def _csv_stringify_row(row: Metadata, keys: list[str]) -> str:
    return "\n".join(f"{k}: {'' if row.get(k) is None else row.get(k)}" for k in keys)

def _csv_unique_labels(header: list[Any]) -> list[str]:
    """Give every column a unique, non-empty label.

    Blank/whitespace headers become ``col_<N>`` (1-based, matching the
    ``--csv-*-cols`` index spec); duplicate labels get a ``_<n>`` suffix
    (``date``, ``date_2``). Using a positional identity instead of the raw
    header string stops empty or duplicate headers from silently
    overwriting one another in the per-row dict.
    """
    used: set[str] = set()
    out: list[str] = []
    for i, raw in enumerate(header):
        label = (str(raw).strip() if raw is not None else "") or f"col_{i + 1}"
        if label in used:
            n = 2
            while f"{label}_{n}" in used:
                n += 1
            label = f"{label}_{n}"
        used.add(label)
        out.append(label)
    return out

def _preprocess_csv(
    filename: str,
    content: bytes,
    csv_options: CsvOptions,
) -> Iterator[tuple[str, str, Metadata]]:
    has_header = (csv_options.get("has_header") or "auto").lower()  # auto|yes|no
    meta_spec = csv_options.get("meta_cols") or ""
    inc_spec  = csv_options.get("include_cols") or ""

    meta_names, meta_idxs = _csv_parse_col_spec(meta_spec)
    inc_names , inc_idxs  = _csv_parse_col_spec(inc_spec)

    # decode
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    sio = io.StringIO(text)
    sniffer = csv.Sniffer()
    try:
        dialect = sniffer.sniff(text[:4096])
    except Exception:
        dialect = csv.excel

    reader = csv.reader(sio, dialect)
    first = next(reader, None)
    if first is None:
        return

    header_row: list[str | None] = None
    if has_header == "yes":
        header_row = [str(h).strip() for h in first]
    elif has_header == "no":
        header_row = None
    else:
        try:
            header_row = [str(h).strip() for h in first] if sniffer.has_header(text[:4096]) else None
        except Exception:
            header_row = None

    if header_row is not None:
        cols = _csv_unique_labels(header_row)
        data_rows = reader
    else:
        cols = [f"col_{i + 1}" for i in range(len(first))]
        data_rows = [first, *list(reader)]

    ncols = len(cols)
    name_to_idx = {c: i for i, c in enumerate(cols)}

    # refuse if names referenced but no header
    if (meta_names or inc_names) and header_row is None:
        raise ValueError("CSV has no header but column names were provided. Use 1-based indices or supply a header.")

    def resolve(names: list[str], idxs: list[int]) -> list[str]:
        out: list[str] = []
        for nm in names:
            if nm not in name_to_idx:
                raise ValueError(f"CSV column '{nm}' not found in header")
            out.append(nm)
        for i in idxs:
            if i < 0 or i >= ncols:
                raise ValueError(f"CSV column index {i+1} out of range (1..{ncols})")
            out.append(cols[i])
        seen = set(); out2=[]
        for k in out:
            if k not in seen:
                seen.add(k); out2.append(k)
        return out2

    meta_keys = resolve(meta_names, meta_idxs)
    if inc_names or inc_idxs:
        include_keys = resolve(inc_names, inc_idxs)
    else:
        # DEFAULT: include all columns EXCEPT meta
        meta_set = set(meta_keys)
        include_keys = [c for c in cols if c not in meta_set]

    rowno = 0
    for row in data_rows:
        rowno += 1
        if len(row) < ncols:
            row = row + [""] * (ncols - len(row))
        elif len(row) > ncols:
            row = row[:ncols]
        asdict = {cols[i]: row[i] for i in range(ncols)}
        text_part = _csv_stringify_row(asdict, include_keys)
        extra = {k: asdict.get(k, "") for k in meta_keys}
        extra["row"] = rowno
        extra["has_header"] = bool(header_row is not None)
        yield (f"row_{rowno-1}", text_part, extra)

def preprocess(filename: str, content: bytes, csv_options: CsvOptions
               | None = None, *,
               chunking: Chunking) -> Iterator[tuple[str, str, Metadata]]:
    """
    Yields (local_id, text, extra_meta):
    - PDF: one chunk per page
    - TXT: chunks per the collection's `chunking`
    - CSV: one chunk per row as newline-joined "key: value" fields
    """
    mt, _ = mimetypes.guess_type(filename)
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext == "pdf":
        try:
            reader = PdfReader(io.BytesIO(content))
            for i, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                yield f"page_{i}", text, {"page": i}
        except PyPdfError as exc:
            raise PreprocessError("invalid PDF document") from exc
    elif ext == "txt":
        text = content.decode("utf-8", errors="ignore")
        yield from _text_chunks(text, chunking)
    elif ext == "csv" or mt == "text/csv":
        yield from _preprocess_csv(filename, content, csv_options or {})
        return
    else:
        raise PreprocessError(f"unsupported file type: {ext or 'unknown'}")


def preprocess_text(
    text: str, *, chunking: Chunking
) -> Iterator[tuple[str, str, Metadata]]:
    return _text_chunks(text, chunking)
