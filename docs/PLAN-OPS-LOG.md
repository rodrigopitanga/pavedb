<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# PLAN-OPS-LOG — Structured Log Emission (P2-28)

Two distinct logging objectives, two independent streams.

---

## Objectives

### Stream 1 — Dev/technical log (existing, cleanup only)

**Who uses it:** developers and ops engineers debugging in real time.
**Format:** human-readable (`HH:MM:SS LEVEL pave: message`), colored in TTY.
**Destination:** stderr (unchanged).
**Future option:** `log.format: json` to emit JSON to stderr instead of text
(deferred; not in this plan).

Changes in this plan: cleanup only — fix noisy log lines, omit absent fields.

### Stream 2 — Operational/business log (new)

**Who uses it:** log aggregators (Datadog, Loki, ELK, Splunk, CloudWatch).
**Format:** JSON lines, one per operation, schema-stable.
**Destination:** configurable — `null` (off), `stdout`, or a file path.
**Default:** `null` (no change to existing deployments).

This is the deliverable of P2-28.

---

## Config schema

```yaml
log:
  ops_log: null          # null (off) | stdout | /path/to/ops.jsonl
```

`stdout` is the recommended value for Docker/12-factor deployments (pave already
uses stderr for the dev stream, so stdout is a clean separation).

File paths are for traditional deployments; no rotation is provided — delegate
to logrotate or a log shipper. Retention (rolling window, purge) is P2-39/v0.8.

---

## Ops event schema

One JSON line per operation, written to the configured destination.

```json
{"ts":"2026-02-26T23:55:55.123Z","op":"search","tenant":"bench","collection":"lat_x","k":5,"hits":5,"latency_ms":1094.6,"status":"ok","request_id":"abc123"}
{"ts":"...","op":"ingest","tenant":"bench","collection":"lat_x","docid":"doc1","chunks":3,"latency_ms":234.5,"status":"ok"}
{"ts":"...","op":"search","tenant":"bench","collection":"lat_x","k":5,"hits":0,"latency_ms":12.3,"status":"error","error_code":"search_failed"}
```

### Always-present fields

| Field | Type | Notes |
|---|---|---|
| `ts` | string | ISO 8601 UTC, millisecond precision |
| `op` | string | see Operations below |
| `tenant` | string | path parameter |
| `collection` | string | path parameter; omitted for tenant-level ops |
| `latency_ms` | float | wall time from route entry to response, 2 dp |
| `status` | string | `"ok"` or `"error"` |

### Conditional fields

| Field | Present when |
|---|---|
| `request_id` | client provided one (omit entirely otherwise) |
| `error_code` | `status == "error"` |
| `k` | op == search |
| `hits` | op == search |
| `docid` | op == ingest, delete_doc |
| `chunks` | op == ingest, status == ok |
| `new_name` | op == rename_collection |

### Operations

| `op` value | Route |
|---|---|
| `search` | POST/GET `/collections/{t}/{c}/search` |
| `ingest` | POST `/collections/{t}/{c}/documents` |
| `delete_doc` | DELETE `/collections/{t}/{c}/documents/{id}` |
| `create_collection` | POST `/collections/{t}/{name}` |
| `delete_collection` | DELETE `/collections/{t}/{name}` |
| `rename_collection` | PUT `/collections/{t}/{name}` |
| `list_collections` | GET `/collections/{t}` |

Admin routes (`/admin/archive`, `/admin/metrics`) and health routes are not
instrumented — they are infrastructure ops, not business events.

---

## Uvicorn access log

Independent of the ops stream. Uvicorn's access log defaults to on; configure
it separately from `ops_log`.

```yaml
log:
  access_log: null     # null (default, keep uvicorn default) | stdout | /path
```

`null` / not set: leave uvicorn's access log at its default (on).
`stdout`: no change — uvicorn already writes access log to stdout.
`/path`: attach a `FileHandler` to the `uvicorn.access` logger.

---

## Dev stream cleanups (Stream 1)

These are independent of the ops log and can be committed separately.

### `pave/service.py` — `SEARCH-OUT`
Currently logs the full `list[SearchResult]` at INFO on every search. This is
verbose and duplicates the summary line. Demote to DEBUG.

### `pave/stores/txtai_store.py` — `POS FILTERS`
Currently logs once per candidate result (O(hits) lines per search). Fix:
- Log once per search call, not per hit.
- Skip entirely when the post-filter dict is empty (the common case).

### `pave/service.py` — `req=None`
The summary line reads `req=None` when no request_id was provided. Omit the
`req=` token entirely when the value is None.

---

## Additional decisions (dev stream pass)

Agreed and committed together with the three items above.

### Naming convention: `AREA-EVENT: payload` (debug internals)

All debug-level internal state messages follow `UPPER-CASE-DASH: payload`:

| Old | New |
|---|---|
| `SEARCH-OUT: {out}` | `SEARCH-OUT: {out}` (unchanged, already correct) |
| `POS FILTERS: {pos_f}` | `SEARCH-FILTER-POST: {pos_f}` |
| `after split: PRE {pre_f} POS {pos_f}` | `SEARCH-FILTER-SPLIT: pre={pre_f} post={pos_f}` |
| `debug:: QUERY: {query} SQL: {sql}` | `SEARCH-SQL: query={query!r} sql={sql!r}` |
| `PREPARED {n} upserts: {prepared}` | `INGEST-PREPARED: {n} chunks [rid0, rid1, ...] ...` (first 3 rids) |

### `INGEST-PREPARED` and `SEARCH-OUT` excerpts

The full prepared/result lists were trimmed to excerpts:
- `INGEST-PREPARED`: count + first 3 chunk IDs, `...` suffix if more
- `SEARCH-OUT`: count + first 3 `(id, score)` pairs, `...` suffix if more

### `SEARCH-FILTER-SPLIT` guarded

Added `if pre_f or pos_f:` guard — no log line when no filters were passed
(the common case).

### f-strings throughout

`%s` format strings replaced with f-strings in all log calls.

### Logger variable nomenclature standardised

- `service.py`: `_log` → `log` (module-level, no underscore)
- `txtai_store.py`: `from pave.config import LOG as log` →
  `from pave.config import get_logger` + `log = get_logger()`
- Convention going forward: `log = get_logger()` at module level.

### Ingest INFO summary (service.py)

Added symmetric `log.info("ingest tenant=... coll=... docid=... chunks=... ms=...")`
to `ingest_document()`. Timing measured with `_time.perf_counter()` around the
full ingest (including purge + index + save).

### Startup config summary (main.py)

`main_srv()` now emits two INFO lines after `resolve_bind()`:
```
config: auth=<mode> store=<type> data_dir=<path> bind=<host>:<port> workers=<n>
limits: search_cap=<n> search_to=<n>ms ingest_cap=<n> tenant_cap=<n|unlimited> ops_log=<dest>
```
`log.ops_log` and `log.access_log` to be added to `_DEFAULTS` in
`pave/config.py` when implementing P2-28.

---

## New module: `pave/log.py`

```python
def configure(dest: str | None) -> None:
    """
    Called once from build_app(). dest is null/None, 'stdout', or a file path.
    Opens the file handle if needed. No-op if dest is None/null.
    """

def emit(**fields) -> None:
    """
    Write one JSON line. No-op if not configured. Thread-safe:
    - stdout: single sys.stdout.write() call (atomic under GIL for small lines)
    - file: protected by a module-level threading.Lock()
    Fields are serialised with json.dumps(separators=(',', ':')) — no spaces,
    fits in a single line, safe for line-based consumers.
    """

def close() -> None:
    """Flush and close the file handle if open. Called from lifespan shutdown."""
```

`ts` is generated inside `emit()` via `datetime.utcnow().isoformat() + 'Z'`
(no external dependency). None values are dropped before serialisation.

---

## Where `emit()` is called

Route handlers in `main.py`, **not** service layer. Rationale: `request_id`
and wall-clock timing (including serialisation overhead) are only available at
the HTTP layer. The service layer has no HTTP context.

All 8 instrumented routes use the `@ops_event` decorator from `pave/log.py`.
The decorator handles timing, `try/finally` emission, and both sync and async
handlers via `asyncio.iscoroutinefunction`.

```python
@app.delete("/collections/{tenant}/{name}")
@ops_event("delete_collection")
def delete_collection(tenant, name, ctx, store):
    ...

@app.post("/collections/{tenant}/{name}/search")
@ops_event(
    "search", coll="name",
    k=lambda kw, r: kw["body"].k,
    hits=lambda kw, r: (
        len(json.loads(r.body).get("matches", []))
        if getattr(r, "status_code", 400) < 400 else None
    ),
    request_id="rid",
)
async def search_post(tenant, name, body, rid, ...):
    ...
```

`ops_event(op, *, coll="name", **extra_keys)` parameters:
- `coll`: kwargs key for the collection path parameter (`None` to omit).
- `**extra_keys`: additional emit fields.
  - `str` value → `kwargs[key]` (direct kwarg lookup)
  - `callable` → `fn(kwargs, result)` called after the handler returns;
    `result` is the return value (JSONResponse or dict)

`_result_status(result)` duck-types the return value: checks `status_code`
attribute (JSONResponse-like) for HTTP errors, or `ok` key for dict returns.

---

## Files changed

| File | Change |
|---|---|
| `pave/log.py` | New — `configure()`, `emit()`, `close()`, `ops_event()` decorator |
| `pave/main.py` | `ops_log.configure()` in `build_app()`; `@ops_event(...)` on all 8 instrumented routes; `access_log` routing in `uvicorn.run()`; `ops_log.close()` in lifespan |
| `pave/service.py` | Demote `SEARCH-OUT` to DEBUG; omit `req=` token when None |
| `pave/stores/txtai_store.py` | Fix `POS FILTERS` log (once per search, skip if empty) |
| `pave/config.py` | Add `log.ops_log` and `log.access_log` to `_DEFAULTS` (deferred to implementation) |
| `config.yml.example` | Document both new keys |
| `tests/test_log.py` | New — configure/emit/close; field omission; None dropping |

---

## Not in scope (P2-28)

- JSON format for the dev stream (`log.format: json`) — future option.
- Log retention, rolling window, purge — P2-39, requires SQLite Phase 3 (v0.8).
- Per-tenant log filtering — post-retention.
- Admin route instrumentation — low value, deferred.
- Error logging at service layer — P2-40, v0.5.9.
