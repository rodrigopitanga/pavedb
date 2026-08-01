# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import re
import sys
from pathlib import Path


STANDARD_TAG = re.compile(r"v(\d{1,4})\.(\d{1,4})\.(\d{1,4})\Z")
HISTORICAL_PATCHVEC_TITLES = {
    "v0.5.4": "PatchVec 0.5.4",
    "v0.5.5": "PatchVec 0.5.5",
    "v0.5.6": "PatchVec 0.5.6",
    "v0.5.7": "PatchVec 0.5.7 — Readiness",
    "v0.5.9": "PatchVec 0.5.9 — Relevance",
}


def validate_release_announcement(tag: str, root: Path = Path(".")) -> str:
    match = STANDARD_TAG.fullmatch(tag)
    if not match:
        raise ValueError(f"not a standard release tag: {tag}")

    version = ".".join(match.groups())
    english_path = root / "docs" / f"RELEASE-{tag}.md"
    portuguese_path = root / "docs" / f"RELEASE-{tag}_pt.md"
    documents = []
    for path in (english_path, portuguese_path):
        try:
            documents.append(path.read_text(encoding="utf-8").splitlines())
        except FileNotFoundError as error:
            raise ValueError(f"missing release announcement: {path}") from error

    english_lines, portuguese_lines = documents
    english_h1 = english_lines[0] if english_lines else ""
    portuguese_h1 = portuguese_lines[0] if portuguese_lines else ""
    if not english_h1.startswith("# "):
        raise ValueError(f"missing first H1 release title: {english_path}")
    if not portuguese_h1.startswith("# "):
        raise ValueError(f"missing first H1 release title: {portuguese_path}")
    if english_h1 != portuguese_h1:
        raise ValueError("release H1 titles must match exactly")
    title = english_h1[2:].strip()

    historical_title = HISTORICAL_PATCHVEC_TITLES.get(tag)
    if historical_title:
        if title != historical_title:
            raise ValueError(
                "historical PatchVec title must be exactly "
                f"'{historical_title}'"
            )
    else:
        expected = f"PaveDB {version}"
        if int(match.group(3)) == 0:
            prefix = f"{expected} — "
            if not title.startswith(prefix) or not title.removeprefix(prefix).strip():
                raise ValueError(
                    f"major/minor release title must be '{prefix}<name>'"
                )
        elif title != expected:
            raise ValueError(f"patch release title must be exactly '{expected}'")

    for lines, heading, wrong_heading in (
        (english_lines, "## English", "## Português (Brasil)"),
        (portuguese_lines, "## Português (Brasil)", "## English"),
    ):
        if lines.count(heading) != 1:
            raise ValueError(f"expected one locale section: {heading}")
        if wrong_heading in lines:
            raise ValueError(f"locale section belongs in companion: {wrong_heading}")
        if any(line.startswith("## ") and line != heading for line in lines):
            raise ValueError(f"unexpected locale section: {heading}")
        if not any(line.strip() for line in lines[lines.index(heading) + 1 :]):
            raise ValueError(f"empty locale section: {heading}")
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
