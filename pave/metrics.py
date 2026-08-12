# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

MetricKind = Literal["counter", "gauge"]
LabelKey = tuple[tuple[str, str], ...]


@dataclass
class _Metric:
    kind: MetricKind
    help: str
    label_names: tuple[str, ...] = ()
    values: dict[LabelKey, float] = field(default_factory=dict)

    def reset(self) -> None:
        self.values.clear()
        if self.kind == "counter" and not self.label_names:
            self.values[()] = 0.0


def _counter(help_text: str, *labels: str) -> _Metric:
    metric = _Metric("counter", help_text, labels)
    metric.reset()
    return metric


def _gauge(help_text: str) -> _Metric:
    return _Metric("gauge", help_text)


_METRICS = {
    "requests_total": _counter("Requests total."),
    "collections_created_total": _counter("Collections created total."),
    "collections_deleted_total": _counter("Collections deleted total."),
    "collections_moved_total": _counter("Collections moved total."),
    "documents_indexed_total": _counter("Documents indexed total."),
    "documents_deleted_total": _counter("Documents deleted total."),
    "chunks_indexed_total": _counter("Chunks indexed total."),
    "purge_total": _counter("Purges total."),
    "search_total": _counter("Searches total."),
    "matches_total": _counter("Search matches total."),
    "errors_total": _counter("Errors total."),
    "http_requests_total": _counter(
        "HTTP requests by route and status.",
        "method",
        "path",
        "status_class",
    ),
    "searches_zero_matches_total": _counter(
        "Completed searches that returned no matches.",
        "collection",
        "tenant",
    ),
    "searches_with_filters_total": _counter(
        "Completed searches that used metadata filters.",
        "collection",
        "tenant",
    ),
    "searches_without_filters_total": _counter(
        "Completed searches that did not use metadata filters.",
        "collection",
        "tenant",
    ),
    "searches_common_merge_total": _counter(
        "Completed searches that merged a distinct common collection."
    ),
    "searches_timeout_total": _counter("Searches that timed out."),
    "embedder_embeddings_total": _counter(
        "Completed embedder encode calls by model.",
        "model",
    ),
    "embedder_embeddings_samples_total": _counter(
        "Embedded samples by model.",
        "model",
    ),
    "cache_evictions_total": _counter("Cache evictions total.", "kind"),
    "query_log_rows": _gauge("Stored query-log rows."),
    "query_log_oldest_age_seconds": _gauge(
        "Age in seconds of the oldest stored query-log row."
    ),
    "chunks_orphaned_total": _gauge(
        "Chunk sidecar files without a metadata row."
    ),
    "chunks_missing_sidecar_total": _gauge(
        "Chunk metadata rows whose sidecar file is missing."
    ),
}

_LATENCY_WINDOW = 1000
_latencies: dict[str, deque[float]] = {
    "search": deque(maxlen=_LATENCY_WINDOW),
    "ingest": deque(maxlen=_LATENCY_WINDOW),
}
_last_error: str | None = None
_last_error_at: float | None = None
_generation: str | None = None
_started = time.time()
_lock = threading.Lock()
_flush_lock = threading.Lock()
_dirty = False


def _label_key(labels: dict[str, str]) -> LabelKey:
    return tuple(sorted((str(key), str(value)) for key, value in labels.items()))


def _metric_locked(
    name: str,
    *,
    label_names: tuple[str, ...] = (),
) -> _Metric:
    metric = _METRICS.get(name)
    if metric is None:
        metric = _counter(name.replace("_", " "), *label_names)
        _METRICS[name] = metric
    return metric


def _mark_dirty_locked() -> None:
    global _dirty
    _dirty = True


def _inc_locked(name: str, value: float, labels: LabelKey = ()) -> None:
    metric = _metric_locked(name, label_names=tuple(key for key, _ in labels))
    metric.values[labels] = metric.values.get(labels, 0.0) + value


def inc(name: str, value: float = 1.0) -> None:
    with _lock:
        _inc_locked(name, value)
        _mark_dirty_locked()


def inc_labeled(
    name: str,
    labels: dict[str, str],
    value: float = 1.0,
) -> None:
    with _lock:
        _inc_locked(name, value, _label_key(labels))
        _mark_dirty_locked()


def _set_gauge(name: str, value: float) -> None:
    with _lock:
        metric = _METRICS.get(name)
        if metric is None:
            metric = _gauge(name.replace("_", " "))
            _METRICS[name] = metric
        metric.values[()] = value


def record_http_request(method: str, path: str, status_code: int) -> None:
    labels = _label_key(
        {
            "method": method.upper(),
            "path": path,
            "status_class": f"{status_code // 100}xx",
        }
    )
    with _lock:
        _inc_locked("requests_total", 1.0)
        _inc_locked("http_requests_total", 1.0, labels)
        _mark_dirty_locked()


def set_error(msg: str) -> None:
    global _last_error, _last_error_at
    with _lock:
        _last_error = msg
        _last_error_at = time.time()
        _inc_locked("errors_total", 1.0)
        _mark_dirty_locked()


def record_latency(op: str, duration_ms: float) -> None:
    with _lock:
        samples = _latencies.setdefault(op, deque(maxlen=_LATENCY_WINDOW))
        samples.append(duration_ms)
        _mark_dirty_locked()


@contextmanager
def timed(op: str):
    start = time.perf_counter()
    try:
        yield
    finally:
        record_latency(op, (time.perf_counter() - start) * 1000)


def _series_snapshot(metric: _Metric) -> list[dict[str, Any]]:
    return [
        {"labels": dict(labels), "value": value}
        for labels, value in sorted(metric.values.items())
        if value
    ]


def _pending_snapshot_locked() -> dict[str, Any]:
    metrics = {}
    for name, metric in sorted(_METRICS.items()):
        if metric.kind != "counter":
            continue
        series = _series_snapshot(metric)
        if series:
            metrics[name] = series
    return {
        "metrics": metrics,
        "last_error": _last_error,
        "last_error_at": _last_error_at,
        "latencies": {
            op: list(samples)
            for op, samples in sorted(_latencies.items())
            if samples
        },
    }


def _series_map(payload: dict[str, Any]) -> dict[str, dict[LabelKey, float]]:
    out: dict[str, dict[LabelKey, float]] = {}
    saved_metrics = payload.get("metrics", {})
    if not isinstance(saved_metrics, dict):
        return out
    for name, entries in saved_metrics.items():
        if not isinstance(entries, list):
            continue
        series = out.setdefault(str(name), {})
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            labels = entry.get("labels")
            if not isinstance(labels, dict):
                continue
            try:
                value = float(entry["value"])
            except (KeyError, TypeError, ValueError):
                continue
            key = _label_key(labels)
            series[key] = series.get(key, 0.0) + value
    return out


def _merge_snapshots(
    stored: dict[str, Any],
    pending: dict[str, Any],
) -> dict[str, Any]:
    series = _series_map(stored)
    for name, values in _series_map(pending).items():
        target = series.setdefault(name, {})
        for labels, value in values.items():
            target[labels] = target.get(labels, 0.0) + value

    stored_latencies = stored.get("latencies", {})
    pending_latencies = pending.get("latencies", {})
    if not isinstance(stored_latencies, dict):
        stored_latencies = {}
    if not isinstance(pending_latencies, dict):
        pending_latencies = {}
    latencies = {
        op: [
            *stored_latencies.get(op, []),
            *pending_latencies.get(op, []),
        ][-_LATENCY_WINDOW:]
        for op in sorted(set(stored_latencies) | set(pending_latencies))
    }

    stored_error_at = float(stored.get("last_error_at") or 0.0)
    pending_error_at = float(pending.get("last_error_at") or 0.0)
    latest_error = pending if pending_error_at >= stored_error_at else stored
    return {
        "generation": stored.get("generation"),
        "metrics": {
            name: [
                {"labels": dict(labels), "value": value}
                for labels, value in sorted(values.items())
            ]
            for name, values in sorted(series.items())
        },
        "last_error": latest_error.get("last_error"),
        "last_error_at": latest_error.get("last_error_at"),
        "latencies": latencies,
    }


def _clear_pending_locked() -> None:
    global _dirty, _last_error, _last_error_at
    for metric in _METRICS.values():
        if metric.kind == "counter":
            metric.reset()
    for samples in _latencies.values():
        samples.clear()
    _last_error = None
    _last_error_at = None
    _dirty = False


def _counter_rows(payload: dict[str, Any]) -> list[tuple[str, str, float]]:
    return [
        (
            name,
            json.dumps(
                dict(labels),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            value,
        )
        for name, values in sorted(_series_map(payload).items())
        for labels, value in sorted(values.items())
    ]


def _take_pending_locked() -> dict[str, Any]:
    pending = _pending_snapshot_locked()
    _clear_pending_locked()
    return pending


def _restore_pending_locked(pending: dict[str, Any]) -> None:
    global _dirty, _last_error, _last_error_at
    for name, values in _series_map(pending).items():
        for labels, value in values.items():
            _inc_locked(name, value, labels)

    for operation, values in pending.get("latencies", {}).items():
        current = list(_latencies.get(operation, ()))
        _latencies[operation] = deque(
            [*values, *current][-_LATENCY_WINDOW:],
            maxlen=_LATENCY_WINDOW,
        )

    pending_error_at = float(pending.get("last_error_at") or 0.0)
    if pending_error_at > float(_last_error_at or 0.0):
        _last_error = pending.get("last_error")
        _last_error_at = pending_error_at
    _dirty = True


def initialize(store: Any) -> None:
    """Bind this worker's pending buffer to the catalog generation."""
    global _generation
    with _flush_lock:
        generation = store.metrics_generation()
        with _lock:
            _clear_pending_locked()
            _generation = generation


def flush(store: Any | None = None) -> bool:
    """Flush this worker's pending deltas without blocking request updates."""
    global _generation
    if store is None:
        return False

    with _flush_lock:
        with _lock:
            if not _dirty:
                return False
            generation = _generation

        if generation is None:
            try:
                generation = store.metrics_generation()
            except Exception:
                return False
            with _lock:
                _generation = generation

        with _lock:
            pending = _take_pending_locked()

        try:
            current_generation, applied = store.flush_metrics_delta(
                generation,
                _counter_rows(pending),
                pending["latencies"],
                (
                    (pending["last_error"], pending["last_error_at"])
                    if pending["last_error"] is not None
                    else None
                ),
                _LATENCY_WINDOW,
            )
        except Exception:
            with _lock:
                if _generation == generation:
                    _restore_pending_locked(pending)
            return False

        with _lock:
            if not applied:
                _clear_pending_locked()
            _generation = current_generation
        return applied


def reset(store: Any | None = None) -> dict[str, Any]:
    """Reset local and persisted metrics."""
    global _generation, _started
    with _flush_lock:
        with _lock:
            generation = store.reset_metrics() if store is not None else None
            _clear_pending_locked()
            for metric in _METRICS.values():
                if metric.kind == "gauge":
                    metric.reset()
            _generation = generation
            _started = time.time()
    return {"ok": True, "reset_at": time.time()}


def _percentile(data: list[float], percentile: float) -> float:
    if not data:
        return 0.0
    index = (len(data) - 1) * percentile / 100
    lower = int(index)
    upper = min(lower + 1, len(data) - 1)
    return data[lower] + (index - lower) * (data[upper] - data[lower])


def _latency_percentiles(samples: list[float]) -> dict[str, float | int]:
    if not samples:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "count": 0}
    samples.sort()
    return {
        "p50": round(_percentile(samples, 50), 2),
        "p95": round(_percentile(samples, 95), 2),
        "p99": round(_percentile(samples, 99), 2),
        "count": len(samples),
    }


def latency_percentiles(op: str) -> dict[str, float | int]:
    with _lock:
        samples = list(_latencies.get(op, []))
    return _latency_percentiles(samples)


def refresh_product_gauges(store: Any | None) -> None:
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
    *,
    store: Any | None = None,
) -> dict[str, Any]:
    global _generation
    with _flush_lock:
        try:
            stored = store.read_metrics() if store is not None else {}
        except Exception:
            stored = {}
        with _lock:
            stored_generation = stored.get("generation")
            if stored_generation and _generation != stored_generation:
                _clear_pending_locked()
                _generation = str(stored_generation)
            pending = _pending_snapshot_locked()
            gauges = {
                name: metric.values[()]
                for name, metric in _METRICS.items()
                if metric.kind == "gauge" and () in metric.values
            }
            uptime = time.time() - _started

    merged = _merge_snapshots(stored, pending)
    series = _series_map(merged)

    data: dict[str, Any] = {}
    for name, metric in _METRICS.items():
        if metric.kind != "counter":
            continue
        values = series.get(name, {})
        if metric.label_names:
            data[name] = [
                {"labels": dict(labels), "value": value}
                for labels, value in sorted(values.items())
            ]
        else:
            data[name] = values.get((), 0.0)
    for name, values in series.items():
        if name in data:
            continue
        data[name] = (
            [
                {"labels": dict(labels), "value": value}
                for labels, value in sorted(values.items())
            ]
            if any(values)
            else values.get((), 0.0)
        )
    data.update(gauges)
    data["uptime_seconds"] = uptime
    data["last_error"] = merged.get("last_error")

    latencies = merged.get("latencies", {})
    for op in ("search", "ingest"):
        percentiles = _latency_percentiles(list(latencies.get(op, [])))
        data[f"{op}_latency_p50_ms"] = percentiles["p50"]
        data[f"{op}_latency_p95_ms"] = percentiles["p95"]
        data[f"{op}_latency_p99_ms"] = percentiles["p99"]
        data[f"{op}_latency_count"] = percentiles["count"]
    if extra:
        data.update(extra)
    return data


def _prom_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _prom_labels(labels: dict[str, str]) -> str:
    return ",".join(
        f'{key}="{_prom_label_value(str(value))}"'
        for key, value in sorted(labels.items())
    )


def _metric_meta(name: str) -> tuple[str, str]:
    metric = _METRICS.get(name)
    if metric is not None:
        return metric.kind, metric.help
    return (
        "counter" if name.endswith("_total") else "gauge",
        name.replace("_", " "),
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
    *,
    store: Any | None = None,
) -> str:
    lines = []
    for name, value in snapshot(extra, store=store).items():
        if isinstance(value, (int, float)):
            lines.extend(_prom_metric_header(name))
            lines.append(f"pavedb_{name} {float(value)}")
        elif isinstance(value, list):
            lines.extend(_prom_metric_header(name))
            for item in value:
                labels = _prom_labels(item["labels"])
                lines.append(f"pavedb_{name}{{{labels}}} {float(item['value'])}")
    if build:
        lines.extend(_prom_metric_header("build_info"))
        labels = _prom_labels(build)
        lines.append(f"pavedb_build_info{{{labels}}} 1")
    return "\n".join(lines) + "\n"
