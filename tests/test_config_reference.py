# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import logging
from importlib import resources

import pytest

from pave.config import (
    CONFIG_FIELDS,
    _DEFAULTS,
    _config_defaults,
    _load_config_fields,
)
from pave.config.render import render_config_example
from scripts.config_reference import render_outputs


def test_config_fields_json_is_packaged():
    resource = resources.files("pave.config").joinpath("config_fields.json")
    assert resource.is_file()


def test_invalid_config_fields_logs_critical_and_raises(tmp_path, caplog):
    bad = tmp_path / "config_fields.json"
    bad.write_text(
        json.dumps({"fields": [{"key": "x", "default": 1}]}), encoding="utf-8"
    )
    with caplog.at_level(logging.CRITICAL, logger="pave.config"):
        with pytest.raises(ValueError, match="missing required keys: description"):
            _load_config_fields(bad)
    assert any(record.levelno == logging.CRITICAL for record in caplog.records)


def test_config_field_description_must_be_string(tmp_path, caplog):
    bad = tmp_path / "config_fields.json"
    bad.write_text(
        json.dumps(
            {"fields": [{"key": "x", "default": 1, "description": None}]}
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.CRITICAL, logger="pave.config"):
        with pytest.raises(ValueError, match="description must be"):
            _load_config_fields(bad)
    assert any(record.levelno == logging.CRITICAL for record in caplog.records)


def test_config_fields_have_unique_keys_and_env_vars():
    keys = [field.key for field in CONFIG_FIELDS]
    env_vars = [field.env_var for field in CONFIG_FIELDS]

    assert len(keys) == len(set(keys))
    assert len(env_vars) == len(set(env_vars))
    assert all(env_var.startswith("PAVEDB_") for env_var in env_vars)


def test_defaults_are_built_from_config_fields():
    assert _DEFAULTS == _config_defaults()


def test_config_reference_outputs_are_current():
    for path, rendered in render_outputs().items():
        assert path.read_text(encoding="utf-8") == rendered


def test_config_example_spacing_between_blocks():
    text = render_config_example()

    assert "auth:\n\n  # Authentication mode" in text
    assert "mode: 'static'\n\n  # Tenant label" in text
    assert "# enabled: true\n\n\n# ------------------------" in text
    assert "# warmup_default: false\n\n  instances:" in text
    assert "native:\n\n      # Built-in native embedder" in text
