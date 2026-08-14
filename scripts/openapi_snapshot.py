# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pave.config import Config  # noqa: E402
import pave.main as main_mod  # noqa: E402


OPENAPI_SNAPSHOT = ROOT / "openapi.json"
JsonObject = dict[str, object]


@contextlib.contextmanager
def _quiet_schema_build():
    logger = logging.getLogger("pave")
    was_disabled = logger.disabled
    original_hw_info = main_mod._hw_info
    logger.disabled = True
    main_mod._hw_info = lambda: {}
    try:
        yield
    finally:
        main_mod._hw_info = original_hw_info
        logger.disabled = was_disabled


def render_openapi() -> JsonObject:
    cfg = Config(data={})
    cfg.set("auth.global_key", "openapi-snapshot")
    cfg.set("log.ops_log", None)
    cfg.set("log.access_log", None)
    with tempfile.TemporaryDirectory(prefix="pavedb-openapi-") as data_dir:
        cfg.set("data_dir", data_dir)
        with _quiet_schema_build():
            app = main_mod.build_app(cfg)
            return app.openapi()


def render_openapi_json() -> str:
    return json.dumps(
        render_openapi(),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def main(argv: list[str] | None = None) -> int:
    argv = argv or []
    check = "--check" in argv
    if any(arg != "--check" for arg in argv):
        print("usage: openapi_snapshot.py [--check]", file=sys.stderr)
        return 2

    rendered = render_openapi_json()
    if check:
        current = (
            OPENAPI_SNAPSHOT.read_text(encoding="utf-8")
            if OPENAPI_SNAPSHOT.exists()
            else ""
        )
        if current != rendered:
            print(
                f"{OPENAPI_SNAPSHOT} is stale; run `make docs-refresh`.",
                file=sys.stderr,
            )
            return 1
        return 0

    OPENAPI_SNAPSHOT.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
