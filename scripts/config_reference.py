# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pave.config.render import render_config_example, render_markdown  # noqa: E402


ROOT_CONFIG = ROOT / "config.yml.example"
CONFIG_REFERENCE = ROOT / "docs" / "reference" / "config.md"


def render_outputs() -> dict[Path, str]:
    return {
        ROOT_CONFIG: render_config_example(),
        CONFIG_REFERENCE: render_markdown(),
    }


def _check_file(path: Path, expected: str) -> bool:
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    return current == expected


def main(argv: list[str] | None = None) -> int:
    argv = argv or []
    check = "--check" in argv
    if any(arg != "--check" for arg in argv):
        print("usage: config_reference.py [--check]", file=sys.stderr)
        return 2

    outputs = render_outputs()
    if check:
        stale = [path for path, text in outputs.items() if not _check_file(path, text)]
        if stale:
            for path in stale:
                print(f"{path} is stale; run `make docs-refresh`.", file=sys.stderr)
            return 1
        return 0

    for path, text in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
