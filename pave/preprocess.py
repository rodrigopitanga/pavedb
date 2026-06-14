# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations
import io, csv, mimetypes
from .config import CFG
from collections.abc import Iterable, Iterator
from typing import Any
from pypdf import PdfReader

from pave.stores.base import Metadata

TXT_CHUNK_SIZE = int(CFG.get("preprocess.txt_chunk_size"))
TXT_CHUNK_OVERLAP = int(CFG.get("preprocess.txt_chunk_overlap"))

CsvOptions = dict[str, Any]

def _chunks(text: str, size: int = TXT_CHUNK_SIZE, overlap: int = TXT_CHUNK_OVERLAP):
    text = text or ""
    step = max(size - overlap, 1)
    i = 0
    while i < len(text):
        yield text[i : i + size]
        i += step

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
        cols = header_row
        data_rows = reader
    else:
        cols = [f"col_{i}" for i in range(len(first))]
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

def preprocess(filename: str, content: bytes, csv_options: CsvOptions \
               | None = None) -> Iterator[tuple[str, str, Metadata]]:
    """
    Yields (local_id, text, extra_meta):
    - PDF: one chunk per page
    - TXT: charcount-based chunks
    - CSV: one chunk per row ("; " join)
    """
    mt, _ = mimetypes.guess_type(filename)
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext == "pdf":
        reader = PdfReader(io.BytesIO(content))
        for i, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            yield f"page_{i}", text, {"page": i}
    elif ext == "txt":
        text = content.decode("utf-8", errors="ignore")
        step = max(TXT_CHUNK_SIZE - TXT_CHUNK_OVERLAP, 1)
        for i, chunk in enumerate(_chunks(text)):
            yield f"chunk_{i}", chunk, {"offset": i * step}
    elif ext == "csv" or mt == "text/csv":
        yield from _preprocess_csv(filename, content, csv_options or {})
        return
    else:
        raise ValueError(f"unsupported file type: {ext or 'unknown'}")
