# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from pathlib import Path

from pave.config import get_cfg
import pave.main as main_mod


def test_main_srv_accepts_home_flag(monkeypatch, tmp_path):
    calls: dict[str, object] = {}
    home = tmp_path / "instance"

    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    monkeypatch.setattr(main_mod, "enforce_policy", lambda _cfg: None)
    monkeypatch.setattr(main_mod, "resolve_bind", lambda _cfg: ("127.0.0.1", 8086))
    monkeypatch.setattr(
        main_mod.uvicorn,
        "run",
        lambda *args, **kwargs: calls.update({"args": args, "kwargs": kwargs}),
    )

    main_mod.main_srv(["--home", str(home)])
    cfg = get_cfg()

    assert cfg.get("data_dir") == str(home / "data")
    assert cfg.get("auth.tenants_file") == str(home / "tenants.yml")
    assert calls["kwargs"]["host"] == "127.0.0.1"
    assert calls["kwargs"]["port"] == 8086


def test_dev_mode_respects_explicit_log_level_env(monkeypatch, tmp_path):
    calls: dict[str, object] = {}
    home = tmp_path / "instance"

    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    monkeypatch.setenv("PAVEDB_DEV", "1")
    monkeypatch.setenv("PAVEDB_LOG__LEVEL", "warning")
    monkeypatch.setattr(main_mod, "enforce_policy", lambda _cfg: None)
    monkeypatch.setattr(main_mod, "resolve_bind", lambda _cfg: ("127.0.0.1", 8086))
    monkeypatch.setattr(
        main_mod.uvicorn,
        "run",
        lambda *args, **kwargs: calls.update({"args": args, "kwargs": kwargs}),
    )

    main_mod.main_srv(["--home", str(home)])

    assert calls["kwargs"]["log_level"] == "warning"


def test_dev_mode_defaults_to_debug_without_log_level_env(monkeypatch, tmp_path):
    calls: dict[str, object] = {}
    home = tmp_path / "instance"

    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    monkeypatch.setenv("PAVEDB_DEV", "1")
    monkeypatch.delenv("PAVEDB_LOG__LEVEL", raising=False)
    monkeypatch.setattr(main_mod, "enforce_policy", lambda _cfg: None)
    monkeypatch.setattr(main_mod, "resolve_bind", lambda _cfg: ("127.0.0.1", 8086))
    monkeypatch.setattr(
        main_mod.uvicorn,
        "run",
        lambda *args, **kwargs: calls.update({"args": args, "kwargs": kwargs}),
    )

    main_mod.main_srv(["--home", str(home)])

    assert calls["kwargs"]["log_level"] == "debug"
