<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Stress benchmark findings (2026-03-29)

These findings came from `bench-stress` / `make benchmark`
after `P2-30` turned stress errors into hard failures.

## 1. `no such table: chunks`

### Symptoms

- `ingest_small` returning `ingest_failed: no such table: chunks`
- `doc_delete` returning `delete_document_failed: no such table: chunks`

### Root cause

`CollectionDB.open(..., read_only=True)` was not using a
true SQLite read-only open.

It called plain `sqlite3.connect(path)`, which can create a
new empty database file if `meta.db` disappears between the
caller's existence check and the actual open.

Because the read-only path intentionally skips migrations,
the next read against `chunks` failed with:

```text
no such table: chunks
```

### Solution

- Use a true SQLite URI read-only open:
  `file:...?...mode=ro`
- Let missing-file opens fail cleanly instead of creating a
  new empty database
- Keep the existing caller behavior:
  - `has_doc()` returns `False`
  - `_read_meta_batch_safe()` returns `{}` on transient
    read-open failure

### Regression tests

- `test_open_read_only_missing_file_does_not_create_db`
- `test_has_doc_fallback_handles_db_removed_before_read_only_open`

## 2. `Directory not empty` on collection delete

### Symptoms

- `collection_delete` returning:
  `delete_collection_failed: [Errno 39] Directory not empty`

### Root cause

`LocalStore.delete_collection()` used raw `shutil.rmtree()`
directly, while archive restore already had a retrying
delete helper for transient filesystem races (`ENOTEMPTY`,
`EBUSY`, `ENOENT`).

So collection delete was less hardened than the archive
paths under the same stress conditions.

### Solution

- Route collection deletion through the existing
  `_remove_path()` helper
- Reuse the same transient retry policy already used by
  archive restore

### Regression test

- `test_delete_collection_retries_transient_dir_not_empty`

## Operational meaning

These failures should be treated as store lifecycle bugs,
not as benchmark noise.

`bench-stress` intentionally mixes create, delete, ingest,
search, and archive operations to flush out TOCTOU bugs in
the SQLite/file-store layer. `P2-30` is doing the right
thing by gating on them.

## Follow-up

The FAISS-side symptom
`bad parameter or other API misuse` was observed in the
same benchmark sample, but it was not targeted in this
slice.

The two fixes above remove the strongest deterministic
lifecycle bugs first. Re-run `bench-stress` after this
patch and only open a separate FAISS issue if that error
still reproduces.
