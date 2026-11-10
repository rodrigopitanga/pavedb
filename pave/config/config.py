# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
import json
import logging
import math
import os, re, threading
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
import yaml  # type: ignore

load_dotenv()

_ENV_PREFIX = "PAVEDB_"
CONFIG_ENV = _ENV_PREFIX + "CONFIG"
HOME_ENV = _ENV_PREFIX + "HOME"
DEFAULT_HOME = "~/pavedb"
_RUNTIME_ENV_VARS = {CONFIG_ENV, HOME_ENV}


@dataclass(frozen=True)
class ConfigField:
    key: str
    default: Any
    description: str
    env_var: str


def _env_var_for(key: str) -> str:
    return _ENV_PREFIX + key.upper().replace(".", "__")


def _config_field(
    key: str,
    default: Any,
    description: str,
    env_var: str | None = None,
) -> ConfigField:
    return ConfigField(
        key=key,
        default=default,
        description=description,
        env_var=env_var or _env_var_for(key),
    )


_CONFIG_FIELDS_PATH = Path(__file__).with_name("config_fields.json")
_REQUIRED_FIELD_KEYS = ("key", "default", "description")


def _load_config_fields(
    path: Path = _CONFIG_FIELDS_PATH,
) -> tuple[ConfigField, ...]:
    # The config schema ships inside the package, so a failure here means a
    # broken install or a bad edit, not operator misconfiguration. It is fatal:
    # log at CRITICAL (stderr via the last-resort handler, since pave logging is
    # not configured this early in import) and re-raise to abort startup.
    log = logging.getLogger(__name__)

    def fail(message: str) -> "NoReturn":
        log.critical("Cannot load config schema: %s", message)
        raise ValueError(message)

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.critical("Cannot load config schema %s: %s", path, exc)
        raise

    if not isinstance(raw, dict) or not isinstance(raw.get("fields"), list):
        fail(f"{path.name} must be a JSON object with a 'fields' array")
    fields: list[ConfigField] = []
    for index, entry in enumerate(raw["fields"]):
        if not isinstance(entry, dict):
            fail(f"{path.name} fields[{index}] must be a JSON object")
        missing = [key for key in _REQUIRED_FIELD_KEYS if key not in entry]
        if missing:
            fail(
                f"{path.name} fields[{index}] is missing required keys: "
                f"{', '.join(missing)}"
            )
        if not isinstance(entry["key"], str) or not entry["key"].strip():
            fail(f"{path.name} fields[{index}].key must be a non-empty string")
        if (
            not isinstance(entry["description"], str)
            or not entry["description"].strip()
        ):
            fail(
                f"{path.name} fields[{index}].description must be a "
                "non-empty string"
            )
        if "env_var" in entry and (
            not isinstance(entry["env_var"], str)
            or not entry["env_var"].strip()
        ):
            fail(
                f"{path.name} fields[{index}].env_var must be a "
                "non-empty string"
            )
        fields.append(
            _config_field(
                entry["key"],
                entry["default"],
                entry["description"],
                entry.get("env_var"),
            )
        )
    return tuple(fields)


CONFIG_FIELDS: tuple[ConfigField, ...] = _load_config_fields()
_LIST_VALUE_KEYS = {
    field.key for field in CONFIG_FIELDS if isinstance(field.default, list)
}


def _config_defaults(
    fields: tuple[ConfigField, ...] = CONFIG_FIELDS,
) -> dict[str, Any]:
    defaults: dict[str, Any] = {}
    for field in fields:
        cur = defaults
        parts = field.key.split(".")
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = deepcopy(field.default)
    return defaults


def _env_flag(name: str) -> bool:
    return cfg_bool(os.environ.get(_ENV_PREFIX + name))


def cfg_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def should_warm_default_embedder(cfg: "Config") -> bool:
    raw = cfg.get("embedder.warmup_default")
    if raw is None:
        return not cfg_bool(cfg.get("dev"))
    return cfg_bool(raw)


def should_enable_ui(cfg: "Config") -> bool:
    raw = cfg.get("ui.enabled")
    if raw is None:
        return cfg_bool(cfg.get("dev"))
    return cfg_bool(raw)


def _default_config_path() -> str | None:
    configured = os.environ.get(CONFIG_ENV)
    if configured:
        return str(Path(configured).expanduser())
    configured_home = os.environ.get(HOME_ENV)
    if configured_home:
        return str(Path(configured_home).expanduser() / "config.yml")
    # In dev mode, do not implicitly load the installed user config file.
    if _env_flag("DEV"):
        return None
    return str(_runtime_home_path() / "config.yml")

_SCHEMA_DEFAULTS = _config_defaults()


def _runtime_home_path(path: str | Path | None = None) -> Path:
    configured_home = os.environ.get(HOME_ENV)
    if configured_home:
        return Path(configured_home).expanduser()
    if path:
        return Path(path).expanduser().parent
    configured = os.environ.get(CONFIG_ENV)
    if configured:
        return Path(configured).expanduser().parent
    return Path(DEFAULT_HOME).expanduser()


def _defaults_for_home(home: Path | None = None) -> dict[str, Any]:
    home_path = home or _runtime_home_path()
    base = deepcopy(_SCHEMA_DEFAULTS)
    base["data_dir"] = str(home_path / "data")
    if os.environ.get(HOME_ENV) or os.environ.get(CONFIG_ENV):
        auth = base.setdefault("auth", {})
        if isinstance(auth, dict):
            auth["tenants_file"] = str(home_path / "tenants.yml")
    return base


_DEFAULTS = _SCHEMA_DEFAULTS

# Path-type config keys: ~ is expanded after the full config merge.
# Supports dotted notation for nested keys (e.g. "log.ops_log").
_PATH_KEYS: tuple[str, ...] = ("data_dir", "log.ops_log", "log.access_log")

_ENV_PATTERN = re.compile(r"\$\{([^}:|]+)(?:\|([^}]*))?\}")

# ---------------- utils ----------------
def _deep_merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        elif v is not None:
            out[k] = v
    return out

def _merge_env(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Merge env overrides, whose names arrive lowercased, onto `a`.

    Env paths are case-folded, so a segment matches an existing key
    case-insensitively; otherwise `..._INSTANCES__MyModel__...` would add a
    second `mymodel` entry instead of overriding the configured `MyModel`.
    """
    out = dict(a)
    for k, v in (b or {}).items():
        if k not in out:
            k = next((key for key in out if str(key).lower() == k), k)
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_env(out[k], v)
        elif v is not None:
            out[k] = v
    return out

def _revoked_api_keys(cfg: dict[str, Any]) -> list[str]:
    """Tenants whose `auth.api_keys` entry is explicitly null."""
    auth = cfg.get("auth")
    keys = auth.get("api_keys") if isinstance(auth, dict) else None
    if not isinstance(keys, dict):
        return []
    return [tenant for tenant, key in keys.items() if key is None]

def _coerce(s: str) -> Any:
    if isinstance(s, str):
        low = s.lower()
        if low in {"true", "false"}:
            return low == "true"
        try:
            if re.fullmatch(r"[+-]?\d+", s):
                return int(s)
            return float(s)
        except Exception:
            return s
    return s

def _subst_env(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    def repl(m: re.Match) -> str:
        key = m.group(1)
        default = m.group(2) if m.group(2) is not None else ""
        return os.environ.get(key, default)
    return _ENV_PATTERN.sub(repl, value)

def _resolve_env_in_obj(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _resolve_env_in_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_in_obj(v) for v in obj]
    return _subst_env(obj)

def _env_to_dict() -> dict[str, Any]:
    envmap: dict[str, Any] = {}
    api_keys_env = _ENV_PREFIX + "AUTH__API_KEYS"
    if api_keys_env in os.environ:
        try:
            api_keys = json.loads(os.environ[api_keys_env])
        except json.JSONDecodeError as exc:
            raise ValueError(f"{api_keys_env} must be a JSON object") from exc
        if not isinstance(api_keys, dict):
            raise ValueError(f"{api_keys_env} must be a JSON object")
        envmap["auth"] = {"api_keys": api_keys}
    for k, v in os.environ.items():
        if not k.startswith(_ENV_PREFIX):
            continue
        if k in _RUNTIME_ENV_VARS or k == api_keys_env:
            continue
        path = k[len(_ENV_PREFIX):].lower().split("__")
        cur = envmap
        for part in path[:-1]:
            cur = cur.setdefault(part, {})
        leaf = ".".join(path)
        if leaf in _LIST_VALUE_KEYS:
            text = v.strip()
            if not text:
                cur[path[-1]] = []
                continue
            parsed = None
            if text[:1] in "[{":
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    parsed = None
            if isinstance(parsed, list):
                cur[path[-1]] = parsed
            else:
                cur[path[-1]] = [
                    item.strip()
                    for item in text.split(",")
                    if item.strip()
                ]
            continue
        # never coerce auth values — API keys are always strings
        cur[path[-1]] = v if path[0] == "auth" else _coerce(v)
    return envmap

def _load_yaml(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if p.is_file():
        with p.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}

def _tenant_overlay(cfg: dict[str, Any]) -> dict[str, Any]:
    """Keep only tenant-related keys from a tenants sidecar file."""
    out: dict[str, Any] = {}
    auth = cfg.get("auth")
    if isinstance(auth, dict):
        api_keys = auth.get("api_keys")
        if isinstance(api_keys, dict):
            out = _deep_merge(out, {"auth": {"api_keys": api_keys}})
    tenants = cfg.get("tenants")
    if isinstance(tenants, dict):
        out = _deep_merge(out, {"tenants": tenants})
    return out


def _configured_embedder_instance_keys(cfg: dict[str, Any]) -> set[str]:
    embedder = cfg.get("embedder")
    if not isinstance(embedder, dict):
        return set()
    instances = embedder.get("instances")
    if not isinstance(instances, dict):
        return set()
    return {str(key) for key in instances}


def _base_defaults_for(
    file_cfg: dict[str, Any],
    env_cfg: dict[str, Any],
    *,
    home: Path | None = None,
) -> dict[str, Any]:
    base = _defaults_for_home(home)
    configured_keys = (
        _configured_embedder_instance_keys(file_cfg)
        | _configured_embedder_instance_keys(env_cfg)
    )
    default_keys = _configured_embedder_instance_keys(base)
    if configured_keys and not configured_keys <= default_keys:
        embedder = base.get("embedder")
        if isinstance(embedder, dict):
            instances = embedder.get("instances")
            if isinstance(instances, dict):
                embedder["instances"] = {
                    key: deepcopy(instances[key])
                    for key in configured_keys & default_keys
                    if key in instances
                }
    return base


SEARCH_MODES = ("vector", "boost", "hybrid")


def _validate_search(cfg: dict[str, Any]) -> None:
    search = cfg.get("search")
    if not isinstance(search, dict):
        return
    modes = search.get("modes")
    if (
        not isinstance(modes, list)
        or not set(modes) <= set(SEARCH_MODES)
        or "vector" not in modes
    ):
        raise ValueError(
            "search.modes must list modes from vector, boost, hybrid and "
            f"include vector; got {modes!r}"
        )
    weight = search.get("boost_weight")
    if (
        isinstance(weight, bool)
        or not isinstance(weight, (int, float))
        or not 0 < weight <= 1
    ):
        raise ValueError(
            f"search.boost_weight must be > 0 and <= 1; got {weight!r}"
        )


_SIZE_LIMIT_KEYS = (
    "ingest.max_file_size_mb",
    "ingest.max_batch_size_mb",
    "server.max_request_body_mb",
)


def _validate_size_limit(path: str, value: Any) -> None:
    if value is None and path == "ingest.max_batch_size_mb":
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or (isinstance(value, float) and not math.isfinite(value))
        or (value < 0 and value != -1)
        or (value > 0 and value * 1024 * 1024 < 1)
        or value > sys.float_info.max / (1024 * 1024)
    ):
        raise ValueError(
            f"{path} must be a finite MB value >= -1 "
            f"(at least one byte if positive); got {value!r}"
        )


def _validate_size_limit_update(path: str, value: Any) -> None:
    for limit_path in _SIZE_LIMIT_KEYS:
        if path == limit_path:
            _validate_size_limit(limit_path, value)
        elif limit_path.startswith(path + "."):
            nested = value
            for part in limit_path[len(path) + 1 :].split("."):
                nested = nested.get(part) if isinstance(nested, dict) else None
            _validate_size_limit(limit_path, nested)


def _validate_config(cfg: dict[str, Any]) -> None:
    _validate_search(cfg)
    _validate_chunking(cfg)
    quota_fields = (
        "max_concurrent", "max_rpm", "max_rph", "max_collections",
        "max_chunks_per_collection", "max_chunks_per_tenant",
        "max_archives_per_day", "max_reindex_per_month",
    )
    tenants = cfg.get("tenants", {})
    if isinstance(tenants, dict):
        for name, values in tenants.items():
            if name.startswith("default_") and name[8:] in quota_fields:
                _validate_quota(f"tenants.{name}", values)
            elif isinstance(values, dict):
                for field in quota_fields:
                    if values.get(field) is not None:
                        _validate_quota(
                            f"tenants.{name}.{field}", values[field]
                        )
    for section in ("search", "ingest", "archive", "reindex"):
        values = cfg.get(section, {})
        if isinstance(values, dict):
            for field in ("max_concurrent", "max_concurrent_per_tenant"):
                if field in values:
                    _validate_quota(f"{section}.{field}", values[field])
    for path in _SIZE_LIMIT_KEYS:
        section, name = path.split(".")
        _validate_size_limit(path, cfg[section][name])


def _validate_quota(path: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < -1:
        raise ValueError(f"{path} must be an integer >= -1; got {value!r}")


def _validate_quota_update(path: str, value: Any) -> None:
    parts = path.split(".")
    if isinstance(value, dict) and (
        path == "tenants"
        or (len(parts) == 2 and parts[0] == "tenants")
        or path in {"search", "ingest", "archive", "reindex"}
    ):
        for field, nested in value.items():
            _validate_quota_update(f"{path}.{field}", nested)
        return
    if parts[0] == "tenants":
        if len(parts) == 2 and parts[1].startswith("default_max_"):
            _validate_quota(path, value)
        elif len(parts) == 3 and parts[2].startswith("max_") and value is not None:
            _validate_quota(path, value)
    elif parts[0] in {"search", "ingest", "archive", "reindex"}:
        if len(parts) == 2 and parts[1] in {
            "max_concurrent", "max_concurrent_per_tenant"
        }:
            _validate_quota(path, value)


CHUNKING_STRATEGIES = ("fixed", "none")
# An overlap above a quarter of the chunk mostly re-embeds the same text.
CHUNK_OVERLAP_RATIO = 4


def chunking_pair_error(
    size: Any, overlap: Any, chunking: dict[str, Any]
) -> str | None:
    """Why a fixed (size, overlap) pair is outside the operator bounds."""
    for name, value in (("size", size), ("overlap", overlap)):
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{name} must be an integer; got {value!r}"
    if not chunking["min_size"] <= size <= chunking["max_size"]:
        return (
            f"size {size} is outside chunking.min_size "
            f"{chunking['min_size']} .. chunking.max_size {chunking['max_size']}"
        )
    if not chunking["min_overlap"] <= overlap <= chunking["max_overlap"]:
        return (
            f"overlap {overlap} is outside chunking.min_overlap "
            f"{chunking['min_overlap']} .. chunking.max_overlap "
            f"{chunking['max_overlap']}"
        )
    if overlap * CHUNK_OVERLAP_RATIO > size:
        return (
            f"overlap {overlap} is more than 1/{CHUNK_OVERLAP_RATIO} of "
            f"size {size}"
        )
    return None


def _validate_chunking(cfg: dict[str, Any]) -> None:
    chunking = cfg.get("chunking")
    if not isinstance(chunking, dict):
        return
    for key in ("min_size", "max_size", "min_overlap", "max_overlap"):
        value = chunking.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"chunking.{key} must be an integer; got {value!r}")
    if not 1 <= chunking["min_size"] <= chunking["max_size"]:
        raise ValueError(
            "chunking.min_size must be at least 1 and at most chunking.max_size"
        )
    if not 0 <= chunking["min_overlap"] <= chunking["max_overlap"]:
        raise ValueError(
            "chunking.min_overlap must be at least 0 and at most "
            "chunking.max_overlap"
        )
    strategies = chunking.get("strategies")
    if (
        not isinstance(strategies, list)
        or not strategies
        or not set(strategies) <= set(CHUNKING_STRATEGIES)
    ):
        raise ValueError(
            "chunking.strategies must list strategies from fixed, none; "
            f"got {strategies!r}"
        )
    if chunking.get("default_strategy") not in strategies:
        raise ValueError(
            "chunking.default_strategy must be one of chunking.strategies"
        )
    problem = chunking_pair_error(
        chunking.get("default_size"), chunking.get("default_overlap"), chunking
    )
    if problem:
        raise ValueError(f"chunking.default_size/default_overlap: {problem}")


# --------------- singleton wrapper ----------------
class Config:
    """
    Single backing dict; thread-safe. Can be constructed from a file path
    or from a pre-built dict overlaid on schema defaults.
    `get(path, default)` persists default.
    """
    def __init__(
            self,
            data: dict[str, Any] | None = None,
            path: str | Path | None = None):
        self._lock = threading.RLock()
        self._path_prefix = ""
        if data is None:
            if path is None:
                path = _default_config_path()
            data = self._load_dict(path)
        else:
            data = _deep_merge(_base_defaults_for(data, {}), data)
            _validate_config(data)
        self._cfg: dict[str, Any] = deepcopy(data)
        self._data = self._cfg  # shared backing store for child views/tests

    # --- main loader, now a static/class member ---
    @staticmethod
    def _load_dict(path: str | Path | None) -> dict[str, Any]:
        home = _runtime_home_path(path)
        file_cfg = _resolve_env_in_obj(_load_yaml(path))
        env_cfg = _env_to_dict()
        base_cfg = _base_defaults_for(file_cfg, env_cfg, home=home)
        file_auth = file_cfg.get("auth", {})
        if not isinstance(file_auth, dict):
            file_auth = {}
        env_auth = env_cfg.get("auth", {})
        if not isinstance(env_auth, dict):
            env_auth = {}
        # `auth.tenants_file` itself follows the normal precedence rules:
        # env > config.yml > defaults.
        base_auth = base_cfg.get("auth", {})
        auth_cfg = deepcopy(base_auth) if isinstance(base_auth, dict) else {}
        auth_cfg = _deep_merge(auth_cfg, file_auth)
        auth_cfg = _deep_merge(auth_cfg, env_auth)
        tenants_file = auth_cfg.get("tenants_file")
        sidecar_cfg: dict[str, Any] = {}
        if tenants_file:
            tenants_file = str(Path(tenants_file).expanduser())
            sidecar_cfg = _tenant_overlay(
                _resolve_env_in_obj(_load_yaml(tenants_file))
            )
        # Sidecar tenant data is loaded first as a base. Inline tenant config in
        # config.yml then overrides it, and env vars override both.
        merged = _deep_merge(base_cfg, sidecar_cfg)
        merged = _deep_merge(merged, file_cfg)
        # A null `auth.api_keys.<tenant>` in config.yml revokes that tenant's
        # key, including one that only the sidecar supplies; a plain merge
        # would skip the null and leave the sidecar key live. Env still
        # overrides the file, so it can set a new key for that tenant.
        merged_keys = merged.get("auth", {}).get("api_keys")
        if isinstance(merged_keys, dict):
            for tenant in _revoked_api_keys(file_cfg):
                merged_keys.pop(tenant, None)
        merged = _merge_env(merged, env_cfg)
        merged_keys = merged.get("auth", {}).get("api_keys")
        if isinstance(merged_keys, dict):
            for tenant in _revoked_api_keys(env_cfg):
                merged_keys.pop(tenant, None)
        for key in _PATH_KEYS:
            parts = key.split(".")
            cur: Any = merged
            for p in parts[:-1]:
                if not isinstance(cur, dict):
                    cur = None
                    break
                cur = cur.get(p)
            if isinstance(cur, dict):
                val = cur.get(parts[-1])
                if isinstance(val, str) and val:
                    cur[parts[-1]] = str(Path(val).expanduser())
        _validate_config(merged)
        return merged

    # -------- path ops --------
    def _get_from(self, store: dict[str, Any], path: str):
        cur: Any = store
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        return cur

    def _set_into(self, store: dict[str, Any], path: str, value: Any) -> None:
        cur = store
        parts = path.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value

    # -------- public API --------
    def get(self, path: str, default: Any = None) -> Any:
        with self._lock:
            val = self._get_from(self._cfg, path)
            if val is not None:
                return val
            if default is not None:
                # persist default on first read (previous behavior)
                self._set_into(self._cfg, path, default)
                return default
            return None

    def set(self, path: str, value: Any) -> None:
        with self._lock:
            _validate_size_limit_update(self._path_prefix + path, value)
            _validate_quota_update(self._path_prefix + path, value)
            self._set_into(self._cfg, path, value)

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._cfg)

    def snapshot(self) -> dict[str, Any]:
        return self.as_dict()

    # replace all config in place while keeping object identity for live refs
    def replace(self, data: dict[str, Any] | None = None,
                path: str | Path | None = None) -> None:
        fresh = Config(data=data, path=path)

        def _apply(target: dict[str, Any], source: dict[str, Any]) -> None:
            # Update in place, preserving nested dict identity so child views
            # from __getattr__ stay valid after replace().
            for key in [k for k in target if k not in source]:
                del target[key]
            for key, value in source.items():
                existing = target.get(key)
                if isinstance(value, dict) and isinstance(existing, dict):
                    _apply(existing, value)
                else:
                    target[key] = value

        with self._lock:
            _apply(self._cfg, fresh._cfg)
            self._data = self._cfg

    # attribute sugar (cfg.instance_name, cfg.auth, ...)
    def __getattr__(self, item):
        with self._lock:
            v = self._cfg.get(item)
            if isinstance(v, dict):
                # lightweight view (shares the same store via a child Config)
                child = Config({})
                # point child to the same backing dict (no copy)
                child._cfg = v
                child._path_prefix = self._path_prefix + item + "."
                return child
            return v

# --- singleton access (API + CLI + tests share this) ---
_CFG_SINGLETON = Config()

def get_cfg() -> Config:
    return _CFG_SINGLETON

def reload_cfg(path: str | None = None) -> Config:
    # hard reload from disk/env; keep the same object for live references
    _CFG_SINGLETON.replace(path=path)
    return _CFG_SINGLETON
