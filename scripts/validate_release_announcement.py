# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import re
import sys
from pathlib import Path


STANDARD_TAG = re.compile(r"v(\d{1,4})\.(\d{1,4})\.(\d{1,4})\Z")


def validate_release_announcement(tag: str, root: Path = Path(".")) -> str:
    match = STANDARD_TAG.fullmatch(tag)
    if not match:
        raise ValueError(f"not a standard release tag: {tag}")

    version = ".".join(match.groups())
    path = root / "docs" / f"RELEASE-{tag}.md"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as error:
        raise ValueError(f"missing release announcement: {path}") from error

    title = next((line[2:].strip() for line in lines if line.startswith("# ")), None)
    if title is None:
        raise ValueError(f"missing H1 release title: {path}")

    expected = f"PaveDB {version}"
    if int(match.group(3)) == 0:
        prefix = f"{expected} — "
        if not title.startswith(prefix) or not title.removeprefix(prefix).strip():
            raise ValueError(f"major/minor release title must be '{prefix}<name>'")
    elif title != expected:
        raise ValueError(f"patch release title must be exactly '{expected}'")
    return title


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        raise SystemExit("usage: validate_release_announcement.py vX.Y.Z")
    try:
        title = validate_release_announcement(argv[0])
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(title)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
