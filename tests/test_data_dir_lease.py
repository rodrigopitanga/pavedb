# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import importlib
import json
import multiprocessing

import pytest

import pave.main as main_mod
from pave.data_dir_lease import DataDirInUseError, DataDirLease
from pave.localclient import provider
from pavesdk.errors import Unavailable


def _hold_data_dir_lease(path, ready, release):
    with DataDirLease(path):
        ready.set()
        if not release.wait(10):
            raise RuntimeError("lease test timed out")


def test_local_owners_share_one_canonical_lease(tmp_path):
    data_dir = tmp_path / "instance" / "data"
    owner = DataDirLease(data_dir).acquire()

    assert owner.path == data_dir / ".pavedb.lock"
    with pytest.raises(DataDirInUseError):
        DataDirLease(data_dir / ".." / "data").acquire()
    with pytest.raises(Unavailable) as excinfo:
        provider(data_dir)
    assert excinfo.value.code == "data_dir_in_use"

    owner.close()
    with provider(data_dir):
        pass

    ephemeral = provider()
    try:
        with pytest.raises(Unavailable):
            provider(ephemeral.path)
    finally:
        ephemeral.close()


def test_data_dir_lease_excludes_another_process(tmp_path):
    data_dir = tmp_path / "data"
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_data_dir_lease,
        args=(data_dir, ready, release),
    )
    process.start()
    try:
        assert ready.wait(10)
        with pytest.raises(DataDirInUseError):
            DataDirLease(data_dir).acquire()
    finally:
        release.set()
        process.join(10)

    assert process.exitcode == 0
    with DataDirLease(data_dir):
        pass


def test_local_client_keeps_lease_when_close_fails(monkeypatch, tmp_path):
    import pave.localclient as localclient_mod

    class FailingStore:
        fail = True

        def close(self):
            if self.fail:
                raise RuntimeError("close failed")

    store = FailingStore()
    monkeypatch.setattr(localclient_mod, "_store_for_path", lambda _path: store)
    data_dir = tmp_path / "data"
    client = localclient_mod.LocalClient(data_dir)

    with pytest.raises(RuntimeError, match="close failed"):
        client.close()
    with pytest.raises(DataDirInUseError):
        DataDirLease(data_dir).acquire()

    store.fail = False
    client.close()
    with DataDirLease(data_dir):
        pass


def test_pavesrv_holds_and_releases_data_dir_lease(monkeypatch, tmp_path):
    home = tmp_path / "instance"
    data_dir = home / "data"
    observed = []

    def capture(*_args, **_kwargs):
        with pytest.raises(DataDirInUseError):
            DataDirLease(data_dir).acquire()
        observed.append(True)

    monkeypatch.setattr(main_mod, "_run_pave_server", capture)
    monkeypatch.setattr(main_mod, "enforce_policy", lambda _cfg: None)
    monkeypatch.setattr(
        main_mod, "resolve_bind", lambda _cfg: ("127.0.0.1", 8086)
    )

    main_mod.main_srv(["--home", str(home)])

    assert observed == [True]
    with DataDirLease(data_dir):
        pass


def test_cli_refuses_busy_data_dir_and_releases_its_lease(tmp_path, capsys):
    from pave import cli as cli_mod

    cli = importlib.reload(cli_mod)
    data_dir = tmp_path / "data"
    owner = DataDirLease(data_dir).acquire()

    rc = cli.main_cli(
        ["--compact", "--data-dir", str(data_dir), "list-tenants"]
    )
    result = json.loads(capsys.readouterr().err)
    assert rc == 1
    assert result["code"] == "data_dir_in_use"

    owner.close()
    assert cli.main_cli(
        ["--compact", "--data-dir", str(data_dir), "list-tenants"]
    ) == 0
    capsys.readouterr()
    with DataDirLease(data_dir):
        pass
