# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import stat

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
import pytest

from pave import auth as auth_mod
from pave import cli as cli_mod
from pave import main as main_mod
from pave.config import Config, get_cfg


def _config() -> Config:
    return Config(data={"auth": {"mode": "static"}})


def test_first_boot_generates_and_persists_admin_key(tmp_path, capsys):
    key = auth_mod.ensure_admin_key(_config(), tmp_path)
    path = tmp_path / "admin.key"

    assert path.read_text(encoding="utf-8").strip() == key
    assert len(key) == 43
    assert "=" not in key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert f"prefix={key[:4]}..." in capsys.readouterr().err


def test_second_boot_reuses_admin_key(tmp_path):
    first = auth_mod.ensure_admin_key(_config(), tmp_path)

    assert auth_mod.ensure_admin_key(_config(), tmp_path) == first


def test_configured_admin_key_skips_file(tmp_path):
    path = tmp_path / "admin.key"
    path.write_text("file-key\n", encoding="utf-8")
    path.chmod(0o644)
    cfg = Config(data={"auth": {"global_key": "configured-key"}})

    assert auth_mod.ensure_admin_key(cfg, tmp_path) == "configured-key"
    assert path.read_text(encoding="utf-8") == "file-key\n"


def test_wide_admin_key_file_mode_refuses_start(tmp_path):
    path = tmp_path / "admin.key"
    path.write_text("key\n", encoding="utf-8")
    path.chmod(0o644)

    with pytest.raises(RuntimeError, match=r"chmod 600"):
        auth_mod.ensure_admin_key(_config(), tmp_path)


def test_rotate_invalidates_old_key(tmp_path, monkeypatch):
    cfg = _config()
    old = auth_mod.ensure_admin_key(cfg, tmp_path)
    app = FastAPI()

    @app.get("/")
    def endpoint(ctx=Depends(auth_mod.auth_ctx)):
        return {"admin": ctx.is_admin}

    monkeypatch.setattr(auth_mod, "get_cfg", lambda: cfg)
    client = TestClient(app)
    old_response = client.get("/", headers={"Authorization": f"Bearer {old}"})
    assert old_response.status_code == 200

    new = auth_mod.rotate_admin_key(cfg, tmp_path)
    assert new != old
    old_response = client.get("/", headers={"Authorization": f"Bearer {old}"})
    new_response = client.get("/", headers={"Authorization": f"Bearer {new}"})
    assert old_response.status_code == 403
    assert new_response.status_code == 200


def test_failed_rotation_leaves_existing_key_intact(tmp_path, monkeypatch):
    cfg = _config()
    old = auth_mod.ensure_admin_key(cfg, tmp_path)
    path = tmp_path / "admin.key"

    def fail_replace(*_args):
        raise OSError("replace failed")

    monkeypatch.setattr(auth_mod.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        auth_mod.rotate_admin_key(cfg, tmp_path)

    assert path.read_text(encoding="utf-8").strip() == old
    assert not list(tmp_path.glob(".admin.key.*"))


def test_bootstrap_then_enforce_static_accepts_generated_key(tmp_path, monkeypatch):
    # The sequence main_srv actually runs: bootstrap the file key, then enforce
    # startup policy. enforce_policy must accept the bootstrapped key (P1-27).
    cfg = _config()
    key = auth_mod.bootstrap_admin_key(cfg, tmp_path)
    assert key is not None
    assert not cfg.get("auth.global_key")
    assert not cfg.get("auth.api_keys")

    auth_mod.enforce_policy(cfg)  # does not raise: has_file recognized

    app = FastAPI()

    @app.get("/")
    def endpoint(ctx=Depends(auth_mod.auth_ctx)):
        return {"admin": ctx.is_admin}

    monkeypatch.setattr(auth_mod, "get_cfg", lambda: cfg)
    client = TestClient(app)
    ok = client.get("/", headers={"Authorization": f"Bearer {key}"})
    assert ok.status_code == 200
    assert ok.json()["admin"] is True
    wrong = client.get("/", headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 403
    missing = client.get("/")
    assert missing.status_code == 401


def test_worker_app_reuses_generated_admin_key(tmp_path, monkeypatch):
    monkeypatch.setenv("PAVEDB_HOME", str(tmp_path))
    key = auth_mod.bootstrap_admin_key(_config(), tmp_path)
    assert key is not None
    worker_cfg = _config()

    main_mod.build_app(worker_cfg)

    assert auth_mod._file_admin_key(worker_cfg) == key


def test_bootstrap_skips_key_file_when_auth_none(tmp_path):
    # Dev/no-auth boot: bootstrap must not mint or persist an admin.key, and
    # enforce_policy still passes while coercing the bind to loopback.
    cfg = Config(data={"auth": {"mode": "none"}, "dev": True, "server": {"host": "0.0.0.0"}})

    assert auth_mod.bootstrap_admin_key(cfg, tmp_path) is None
    assert not (tmp_path / "admin.key").exists()

    auth_mod.enforce_policy(cfg)  # does not raise
    assert cfg.get("server.host") == "127.0.0.1"


def test_cli_show_key_requires_an_existing_key(tmp_path, capsys):
    rc = cli_mod.main_cli(["--home", str(tmp_path), "admin", "show-key"])

    assert rc == 1
    assert json.loads(capsys.readouterr().out)["code"] == "admin_key_unavailable"
    assert not (tmp_path / "admin.key").exists()


def test_cli_rotate_key_prints_the_replacement_once(tmp_path, capsys):
    old = auth_mod.ensure_admin_key(_config(), tmp_path)
    capsys.readouterr()

    rc = cli_mod.main_cli(["--home", str(tmp_path), "admin", "rotate-key"])
    new = capsys.readouterr().out.strip()

    assert rc == 0
    assert new != old
    assert auth_mod.current_admin_key(get_cfg(), tmp_path) == new
