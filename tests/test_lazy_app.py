# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import sys
from contextlib import suppress
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from uvicorn.importer import import_from_string


def test_importing_build_app_and_version_does_not_build_app():
    """Importing build_app or VERSION must not trigger store/model init."""
    # Reload pave.main from scratch to clear any cached _app.
    for key in list(sys.modules):
        if key == "pave.main":
            del sys.modules[key]

    import pave.main as m
    # Force a fresh _app=None state (module may have been loaded by conftest).
    m._app = None

    # Accessing build_app and VERSION must leave _app untouched.
    _ = m.build_app
    _ = m.VERSION
    assert m._app is None, "_app must stay None after importing build_app/VERSION"


def test_uvicorn_import_requires_packaged_context(monkeypatch):
    import pave.main as m
    m._app = None
    monkeypatch.delenv(m._ASGI_CONTEXT_ENV, raising=False)

    with pytest.raises(SystemExit, match="run `pavesrv`"):
        import_from_string("pave.main:app")

    app = SimpleNamespace(state=SimpleNamespace(ui_enabled=False))
    with pytest.raises(TypeError):
        m.build_app()

    monkeypatch.setenv(m._ASGI_CONTEXT_ENV, "pavesrv")
    monkeypatch.setattr(m, "build_app", lambda _cfg: app)

    assert import_from_string("pave.main:app") is app
    assert import_from_string("pave.main:app") is app


def test_live_health_does_not_load_embedder(monkeypatch, tmp_path):
    import pave.main as m
    from pave.config import get_cfg

    cfg = get_cfg()
    cfg.set("data_dir", str(tmp_path))
    cfg.set("embedder.default", "native")
    cfg.set("embedder.instances.native.type", "sbert")
    cfg.set("embedder.instances.native.model", "fake")
    cfg.set("embedder.instances.native.config.runtime", "auto")
    cfg.set("auth.mode", "none")

    def fail_get_embedder():
        raise AssertionError("live health loaded an embedder")

    monkeypatch.setattr(m, "get_embedder", fail_get_embedder, raising=True)

    app = m.build_app(cfg)
    with TestClient(app, base_url="http://localhost") as client:
        r = client.get("/health/live")

    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_warmup_default_false_skips_embedder(monkeypatch, tmp_path):
    """`embedder.warmup_default=false` keeps the embedder lazy at startup."""
    import pave.main as m
    from pave.config import get_cfg

    cfg = get_cfg()
    cfg.set("data_dir", str(tmp_path))
    cfg.set("auth.mode", "none")
    cfg.set("embedder.warmup_default", False)

    def fail_get_embedder():
        raise AssertionError("warmup_default=false must not load the embedder")

    monkeypatch.setattr(m, "get_embedder", fail_get_embedder, raising=True)

    app = m.build_app(cfg)
    with TestClient(app, base_url="http://localhost"):
        pass


def test_warmup_default_auto_skips_dev_embedder(monkeypatch, tmp_path):
    """Implicit warmup stays lazy in dev mode."""
    import pave.main as m
    from pave.config import get_cfg

    cfg = get_cfg()
    cfg.set("data_dir", str(tmp_path))
    cfg.set("auth.mode", "none")
    cfg.set("dev", True)
    cfg.set("embedder.warmup_default", None)

    def fail_get_embedder():
        raise AssertionError("dev auto warmup must not load the embedder")

    monkeypatch.setattr(m, "get_embedder", fail_get_embedder, raising=True)

    app = m.build_app(cfg)
    with TestClient(app, base_url="http://localhost"):
        pass


def test_warmup_default_auto_warms_prod_embedder(monkeypatch, tmp_path):
    """Implicit warmup loads the default embedder outside dev mode."""
    import pave.main as m
    from pave.config import get_cfg

    cfg = get_cfg()
    cfg.set("data_dir", str(tmp_path))
    # A real non-dev config: auth.mode=none is refused outside dev, so using
    # it here would assert warmup against a configuration that cannot boot.
    cfg.set("auth.mode", "static")
    cfg.set("auth.global_key", "warmup-test-key")
    cfg.set("dev", False)
    cfg.set("embedder.warmup_default", None)

    calls = []
    app = m.build_app(cfg)
    app.state.store.warm = lambda: calls.append("warm")
    with TestClient(app, base_url="http://localhost"):
        pass

    assert calls == ["warm"]


def test_metrics_periodic_flush_task(monkeypatch):
    import pave.main as m

    calls = []
    store = object()

    async def run_probe():
        monkeypatch.setattr(m, "METRICS_FLUSH_INTERVAL_SECONDS", 0.01)
        monkeypatch.setattr(
            m,
            "metrics_flush",
            lambda got_store: calls.append(got_store),
        )
        task = asyncio.create_task(m._flush_metrics_periodically(store))
        try:
            await asyncio.sleep(0.025)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    asyncio.run(run_probe())

    assert calls
    assert all(call is store for call in calls)


def test_catalog_metrics_periodic_refresh_task(monkeypatch):
    import pave.main as m

    calls = []

    class Store:
        def refresh_catalog_metrics(self):
            calls.append("catalog")

    monkeypatch.setattr(
        m,
        "metrics_refresh_product_gauges",
        lambda store: calls.append("product"),
    )

    async def run_probe():
        task = asyncio.create_task(
            m._refresh_catalog_metrics_periodically(Store())
        )
        try:
            await asyncio.sleep(0.01)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    asyncio.run(run_probe())

    assert calls[:2] == ["catalog", "product"]


def test_lifespan_flushes_metrics_on_shutdown(monkeypatch, tmp_path):
    import pave.main as m
    from pave.config import get_cfg

    cfg = get_cfg()
    cfg.set("data_dir", str(tmp_path))
    cfg.set("auth.mode", "none")
    cfg.set("embedder.warmup_default", False)

    calls = []
    monkeypatch.setattr(m, "METRICS_FLUSH_INTERVAL_SECONDS", 3600.0)
    monkeypatch.setattr(
        m,
        "metrics_flush",
        lambda store: calls.append("flush"),
    )

    app = m.build_app(cfg)
    app.state.store.close = lambda: calls.append("close")
    with TestClient(app, base_url="http://localhost"):
        assert calls == []

    assert calls == ["flush", "close"]


def test_build_app_enforces_auth_policy(tmp_path):
    """The uvicorn entry point must not bypass the production auth policy."""
    import pave.main as m
    from pave.config import get_cfg

    cfg = get_cfg()
    cfg.set("data_dir", str(tmp_path))
    cfg.set("auth.mode", "none")
    cfg.set("dev", False)

    with pytest.raises(RuntimeError, match="not allowed in production"):
        m.build_app(cfg)
