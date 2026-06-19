# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from pave.config import get_cfg
import pave.main as main_mod


def _capture_server_run(monkeypatch):
    calls: dict[str, object] = {}

    def capture(*args, **kwargs):
        calls.update({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(main_mod, "_run_pave_server", capture)
    return calls


def test_main_srv_accepts_home_flag(monkeypatch, tmp_path):
    calls = _capture_server_run(monkeypatch)
    home = tmp_path / "instance"

    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    monkeypatch.setattr(main_mod, "enforce_policy", lambda _cfg: None)
    monkeypatch.setattr(main_mod, "resolve_bind", lambda _cfg: ("127.0.0.1", 8086))

    main_mod.main_srv(["--home", str(home)])
    cfg = get_cfg()

    assert cfg.get("data_dir") == str(home / "data")
    assert cfg.get("auth.tenants_file") == str(home / "tenants.yml")
    assert calls["kwargs"]["host"] == "127.0.0.1"
    assert calls["kwargs"]["port"] == 8086


def test_dev_mode_respects_explicit_log_level_env(monkeypatch, tmp_path):
    calls = _capture_server_run(monkeypatch)
    home = tmp_path / "instance"

    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    monkeypatch.setenv("PAVEDB_DEV", "1")
    monkeypatch.setenv("PAVEDB_LOG__LEVEL", "warning")
    monkeypatch.setattr(main_mod, "enforce_policy", lambda _cfg: None)
    monkeypatch.setattr(main_mod, "resolve_bind", lambda _cfg: ("127.0.0.1", 8086))

    main_mod.main_srv(["--home", str(home)])

    assert calls["kwargs"]["log_level"] == "warning"


def test_dev_mode_defaults_to_debug_without_log_level_env(monkeypatch, tmp_path):
    calls = _capture_server_run(monkeypatch)
    home = tmp_path / "instance"

    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    monkeypatch.setenv("PAVEDB_DEV", "1")
    monkeypatch.delenv("PAVEDB_LOG__LEVEL", raising=False)
    monkeypatch.setattr(main_mod, "enforce_policy", lambda _cfg: None)
    monkeypatch.setattr(main_mod, "resolve_bind", lambda _cfg: ("127.0.0.1", 8086))

    main_mod.main_srv(["--home", str(home)])

    assert calls["kwargs"]["log_level"] == "debug"


def test_main_srv_routes_access_log_to_file(monkeypatch, tmp_path):
    calls = _capture_server_run(monkeypatch)
    home = tmp_path / "instance"
    access_log = tmp_path / "logs" / "access.log"

    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    monkeypatch.setenv("PAVEDB_LOG__ACCESS_LOG", str(access_log))
    monkeypatch.setattr(main_mod, "enforce_policy", lambda _cfg: None)
    monkeypatch.setattr(main_mod, "resolve_bind", lambda _cfg: ("127.0.0.1", 8086))

    main_mod.main_srv(["--home", str(home)])

    log_config = calls["kwargs"]["log_config"]
    access_handler = log_config["handlers"]["access"]
    assert calls["kwargs"]["access_log"] is True
    assert access_handler["class"] == "logging.FileHandler"
    assert access_handler["filename"] == str(access_log)
    assert access_log.parent.is_dir()


def test_main_srv_leaves_stdout_access_log_on_uvicorn_default(
    monkeypatch, tmp_path
):
    calls = _capture_server_run(monkeypatch)
    home = tmp_path / "instance"

    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    monkeypatch.setenv("PAVEDB_LOG__ACCESS_LOG", "stdout")
    monkeypatch.setattr(main_mod, "enforce_policy", lambda _cfg: None)
    monkeypatch.setattr(main_mod, "resolve_bind", lambda _cfg: ("127.0.0.1", 8086))

    main_mod.main_srv(["--home", str(home)])

    assert calls["kwargs"]["access_log"] is True
    assert "log_config" not in calls["kwargs"]
