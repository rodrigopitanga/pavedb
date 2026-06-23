# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
import json
import logging
import os, re, threading
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
import yaml  # type: ignore

load_dotenv()

_ENV_PREFIX = "PAVEDB_"


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
    value = os.environ.get(_ENV_PREFIX + name)
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _default_config_path() -> str | None:
    configured = os.environ.get(_ENV_PREFIX + "CONFIG")
    if configured:
        return str(Path(configured).expanduser())
    # In dev mode, do not implicitly load the installed user config file.
    if _env_flag("DEV"):
        return None
    return str(Path("~/pavedb/config.yml").expanduser())

_DEFAULTS = _config_defaults()

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

def _coerce(s: str) -> Any:
    if isinstance(s, str):
        low = s.lower()
        if low in {"true", "false"}:
            return low == "true"
        try:
            if s.isdigit():
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
    for k, v in os.environ.items():
        if not k.startswith(_ENV_PREFIX):
            continue
        path = k[len(_ENV_PREFIX):].lower().split("__")
        cur = envmap
        for part in path[:-1]:
            cur = cur.setdefault(part, {})
        cur[path[-1]] = _coerce(v)
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
) -> dict[str, Any]:
    base = deepcopy(_DEFAULTS)
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
        if data is None:
            if path is None:
                path = _default_config_path()
            data = self._load_dict(path)
        else:
            data = _deep_merge(_base_defaults_for(data, {}), data)
        self._cfg: dict[str, Any] = deepcopy(data)
        self._data = self._cfg  # shared backing store for child views/tests

    # --- main loader, now a static/class member ---
    @staticmethod
    def _load_dict(path: str | Path | None) -> dict[str, Any]:
        file_cfg = _resolve_env_in_obj(_load_yaml(path))
        env_cfg = _env_to_dict()
        file_auth = file_cfg.get("auth", {})
        if not isinstance(file_auth, dict):
            file_auth = {}
        env_auth = env_cfg.get("auth", {})
        if not isinstance(env_auth, dict):
            env_auth = {}
        # `auth.tenants_file` itself follows the normal precedence rules:
        # env > config.yml > defaults.
        auth_cfg = _deep_merge(file_auth, env_auth)
        tenants_file = auth_cfg.get("tenants_file")
        sidecar_cfg: dict[str, Any] = {}
        if tenants_file:
            tenants_file = str(Path(tenants_file).expanduser())
            sidecar_cfg = _tenant_overlay(
                _resolve_env_in_obj(_load_yaml(tenants_file))
            )
        # Sidecar tenant data is loaded first as a base. Inline tenant config in
        # config.yml then overrides it, and env vars override both.
        merged = _deep_merge(_base_defaults_for(file_cfg, env_cfg), sidecar_cfg)
        merged = _deep_merge(merged, file_cfg)
        merged = _deep_merge(merged, env_cfg)
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
        with self._lock:
            self._cfg.clear()
            self._cfg.update(fresh._cfg)
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
