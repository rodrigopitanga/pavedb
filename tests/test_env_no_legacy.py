# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Regression: PATCHVEC_* env vars are ignored after the PaveDB rebrand."""

from pathlib import Path

from pave.config import reload_cfg


def test_patchvec_env_is_ignored_when_pavedb_is_set(monkeypatch, tmp_path):
    pavedb_path = tmp_path / "pavedb"
    patchvec_path = tmp_path / "patchvec"
    monkeypatch.setenv("PAVEDB_DATA_DIR", str(pavedb_path))
    monkeypatch.setenv("PATCHVEC_DATA_DIR", str(patchvec_path))

    cfg = reload_cfg()

    assert cfg.get("data_dir") == str(pavedb_path)


def test_patchvec_only_env_is_ignored(monkeypatch, tmp_path):
    monkeypatch.delenv("PAVEDB_DATA_DIR", raising=False)
    monkeypatch.setenv("PATCHVEC_DATA_DIR", str(tmp_path / "patchvec"))

    cfg = reload_cfg()

    assert cfg.get("data_dir") == str(Path("~/pavedb/data").expanduser())
