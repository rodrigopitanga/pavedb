# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations
import json, os, tempfile, time, threading
from collections import deque
from contextlib import contextmanager
from typing import Any

_started = time.time()
_lock = threading.Lock()
_data_dir: str | None = None
_dirty = False
_last_changed = time.monotonic()
_METRICS_FILE = "metrics.json"

_counters: dict[str, float] = {
    "requests_total": 0.0,
    "collections_created_total": 0.0,
    "collections_deleted_total": 0.0,
    "documents_indexed_total": 0.0,
    "documents_deleted_total": 0.0,
    "chunks_indexed_total": 0.0,
    "purge_total": 0.0,
    "search_total": 0.0,
    "errors_total": 0.0,
}
_labeled_counters: dict[str, dict[tuple[tuple[str, str], ...], float]] = {}
_gauges: dict[str, float] = {}

_METRIC_META: dict[str, tuple[str, str]] = {
    "http_requests_total": ("counter", "HTTP requests by route and status."),
    "searches_zero_matches_total": (
        "counter",
        "Completed searches that returned no matches.",
    ),
    "searches_with_filters_total": (
        "counter",
        "Completed searches that used metadata filters.",
    ),
    "searches_without_filters_total": (
        "counter",
        "Completed searches that did not use metadata filters.",
    ),
    "searches_common_merge_total": (
        "counter",
        "Completed searches that merged a distinct common collection.",
    ),
    "searches_timeout_total": ("counter", "Searches that timed out."),
    "query_log_rows": ("gauge", "Stored query-log rows."),
    "query_log_oldest_age_seconds": (
        "gauge",
        "Age in seconds of the oldest stored query-log row.",
    ),
    "embedder_embeddings_total": (
        "counter",
        "Completed embedder encode calls by model.",
    ),
    "embedder_embeddings_samples_total": (
        "counter",
        "Embedded samples by model.",
    ),
    "chunks_orphaned_total": (
        "gauge",
        "Chunk sidecar files without a metadata row.",
    ),
    "chunks_missing_sidecar_total": (
        "gauge",
        "Chunk metadata rows whose sidecar file is missing.",
    ),
}

_last_error: str | None = None

# Latency tracking: keep last N samples per operation type
_LATENCY_WINDOW = 1000
_latencies: dict[str, deque] = {
    "search": deque(maxlen=_LATENCY_WINDOW),
    "ingest": deque(maxlen=_LATENCY_WINDOW),
}

def _metrics_path() -> str | None:
    if not _data_dir:
        return None
    return os.path.join(_data_dir, _METRICS_FILE)

def set_data_dir(path: str) -> None:
    """Set the data directory for metrics persistence and load existing metrics."""
    global _data_dir
    _data_dir = path
    load()

def load() -> None:
    """Load metrics from disk if available."""
    global _counters, _last_error, _latencies, _labeled_counters
    path = _metrics_path()
    if not path or not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        with _lock:
            for k, v in data.get("counters", {}).items():
                if k in _counters:
                    _counters[k] = float(v)
            _labeled_counters.clear()
            for name, entries in data.get("labeled_counters", {}).items():
                if not isinstance(entries, list):
                    continue
                series: dict[tuple[tuple[str, str], ...], float] = {}
                for entry in entries:
                    labels = entry.get("labels")
                    value = entry.get("value")
                    if not isinstance(labels, dict):
                        continue
                    key = tuple(
                        sorted((str(k), str(v)) for k, v in labels.items())
                    )
                    series[key] = float(value)
                _labeled_counters[str(name)] = series
            _last_error = data.get("last_error")
            for op, samples in data.get("latencies", {}).items():
                if op not in _latencies:
                    _latencies[op] = deque(maxlen=_LATENCY_WINDOW)
                else:
                    _latencies[op].clear()
                for s in samples[-_LATENCY_WINDOW:]:
                    _latencies[op].append(float(s))
    except Exception:
        pass  # ignore load errors, start fresh

def _snapshot_for_save_locked() -> dict[str, Any]:
    return {
        "counters": dict(_counters),
        "labeled_counters": _labeled_counters_json(),
        "last_error": _last_error,
        "latencies": {
            op: list(samples)
            for op, samples in _latencies.items()
        },
    }

def _write_metrics_file(directory: str, path: str, data: dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

def save() -> None:
    """Persist metrics to disk (atomic write via temp file + rename)."""
    global _dirty
    path = _metrics_path()
    if not path:
        return
    try:
        d = os.path.dirname(path)
        os.makedirs(d, exist_ok=True)
        with _lock:
            if not _dirty:
                return
            data = _snapshot_for_save_locked()
            _write_metrics_file(d, path, data)
            _dirty = False
    except Exception:
        with _lock:
            _dirty = True
        pass  # ignore save errors

def flush() -> None:
    """Persist metrics only if changed since last save."""
    save()

def flush_if_idle(min_idle_seconds: float) -> bool:
    global _dirty
    path = _metrics_path()
    if not path:
        return False
    try:
        d = os.path.dirname(path)
        os.makedirs(d, exist_ok=True)
        with _lock:
            if (
                not _dirty or
                time.monotonic() - _last_changed < min_idle_seconds
            ):
                return False
            data = _snapshot_for_save_locked()
            _write_metrics_file(d, path, data)
            _dirty = False
        return True
    except Exception:
        with _lock:
            _dirty = True
        return False

def reset() -> dict[str, Any]:
    """Reset all metrics to initial state and persist."""
    global _counters, _gauges, _last_error, _latencies, _started, _labeled_counters
    with _lock:
        _counters = {k: 0.0 for k in _counters}
        _labeled_counters = {}
        _gauges = {}
        _last_error = None
        for op in _latencies:
            _latencies[op].clear()
        _started = time.time()
        _mark_dirty_locked()
    save()
    return {"ok": True, "reset_at": time.time()}

def _mark_dirty_locked() -> None:
    global _dirty, _last_changed
    _dirty = True
    _last_changed = time.monotonic()

def inc(name: str, value: float = 1.0):
    with _lock:
        _counters[name] = _counters.get(name, 0.0) + value
        _mark_dirty_locked()

def inc_labeled(name: str, labels: dict[str, str], value: float = 1.0):
    key = tuple(sorted((str(k), str(v)) for k, v in labels.items()))
    with _lock:
        series = _labeled_counters.setdefault(name, {})
        series[key] = series.get(key, 0.0) + value
        _mark_dirty_locked()


def _set_gauge(name: str, value: float) -> None:
    with _lock:
        _gauges[name] = value

def record_http_request(method: str, path: str, status_code: int):
    status_class = f"{status_code // 100}xx"
    labels = {
        "method": method.upper(),
        "path": path,
        "status_class": status_class,
    }
    key = tuple(sorted(labels.items()))
    with _lock:
        _counters["requests_total"] = _counters.get("requests_total", 0.0) + 1
        series = _labeled_counters.setdefault("http_requests_total", {})
        series[key] = series.get(key, 0.0) + 1
        _mark_dirty_locked()

def set_error(msg: str):
    global _last_error
    with _lock:
        _last_error = msg
        _counters["errors_total"] = _counters.get("errors_total", 0.0) + 1.0
        _mark_dirty_locked()

def record_latency(op: str, duration_ms: float):
    """Record a latency sample for an operation type (search, ingest)."""
    with _lock:
        if op not in _latencies:
            _latencies[op] = deque(maxlen=_LATENCY_WINDOW)
        _latencies[op].append(duration_ms)
        _mark_dirty_locked()

@contextmanager
def timed(op: str):
    """Context manager to time an operation and record its latency."""
    start = time.perf_counter()
    try:
        yield
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        record_latency(op, duration_ms)

def _percentile(data: list[float], p: float) -> float:
    """Compute the p-th percentile of a sorted list."""
    if not data:
        return 0.0
    k = (len(data) - 1) * p / 100
    f = int(k)
    c = f + 1 if f + 1 < len(data) else f
    return data[f] + (k - f) * (data[c] - data[f])

def latency_percentiles(op: str) -> dict[str, float]:
    """Return p50, p95, p99 for an operation type."""
    with _lock:
        samples = list(_latencies.get(op, []))
    if not samples:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "count": 0}
    samples.sort()
    return {
        "p50": round(_percentile(samples, 50), 2),
        "p95": round(_percentile(samples, 95), 2),
        "p99": round(_percentile(samples, 99), 2),
        "count": len(samples),
    }

def _refresh_product_gauges(store: Any | None) -> None:
    if store is None:
        return
    try:
        rows, oldest = store.query_log_gauge_stats()
        _set_gauge("query_log_rows", float(rows))
        _set_gauge(
            "query_log_oldest_age_seconds",
            0.0 if oldest is None else max(0.0, time.time() - oldest),
        )
    except Exception:
        pass
    try:
        drift = store.chunk_drift_stats()
        _set_gauge("chunks_orphaned_total", float(drift["orphaned_sidecars"]))
        _set_gauge(
            "chunks_missing_sidecar_total",
            float(drift["missing_sidecars"]),
        )
    except Exception:
        pass


def snapshot(
    extra: dict[str, Any] | None = None,
    store: Any | None = None,
) -> dict[str, Any]:
    _refresh_product_gauges(store)
    with _lock:
        data = dict(_counters)
        data.update(_gauges)
        for name in _labeled_counters:
            data[name] = _labeled_counter_snapshot(name)
        data.update({
            "uptime_seconds": time.time() - _started,
            "last_error": _last_error,
        })
    # Add latency percentiles for each tracked operation
    for op in ("search", "ingest"):
        pcts = latency_percentiles(op)
        data[f"{op}_latency_p50_ms"] = pcts["p50"]
        data[f"{op}_latency_p95_ms"] = pcts["p95"]
        data[f"{op}_latency_p99_ms"] = pcts["p99"]
        data[f"{op}_latency_count"] = pcts["count"]
    if extra:
        data.update(extra)
    return data

def _labeled_counter_snapshot(name: str) -> list[dict[str, Any]]:
    series = _labeled_counters.get(name, {})
    return [
        {
            "labels": dict(labels),
            "value": value,
        }
        for labels, value in sorted(series.items())
    ]

def _labeled_counters_json() -> dict[str, list[dict[str, Any]]]:
    return {
        name: [
            {
                "labels": dict(labels),
                "value": value,
            }
            for labels, value in sorted(series.items())
        ]
        for name, series in sorted(_labeled_counters.items())
    }

def _prom_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')

def _prom_labels(labels: dict[str, str]) -> str:
    return ",".join(
        f'{key}="{_prom_label_value(str(value))}"'
        for key, value in sorted(labels.items())
    )


def _metric_meta(name: str) -> tuple[str, str]:
    return _METRIC_META.get(
        name,
        ("counter" if name.endswith("_total") else "gauge", name.replace("_", " ")),
    )


def _prom_help_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n")


def _prom_metric_header(name: str) -> list[str]:
    metric_type, help_text = _metric_meta(name)
    metric = f"pavedb_{name}"
    return [
        f"# HELP {metric} {_prom_help_value(help_text)}",
        f"# TYPE {metric} {metric_type}",
    ]

def to_prometheus(
    extra: dict[str, Any] | None = None,
    build: dict[str, str] | None = None,
    store: Any | None = None,
) -> str:
    s = []
    snap = snapshot(extra, store=store)
    for k, v in snap.items():
        if isinstance(v, (int, float)):
            s.extend(_prom_metric_header(k))
            s.append(f"pavedb_{k} {float(v)}")
        elif isinstance(v, list):
            s.extend(_prom_metric_header(k))
            for item in v:
                labels = _prom_labels(item["labels"])
                s.append(f"pavedb_{k}{{{labels}}} {float(item['value'])}")
    if build:
        s.extend(_prom_metric_header("build_info"))
        labels = _prom_labels(build)
        s.append(f"pavedb_build_info{{{labels}}} 1")
    return "\n".join(s) + "\n"
