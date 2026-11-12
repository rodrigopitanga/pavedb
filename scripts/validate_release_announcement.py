# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import re
import sys
from pathlib import Path


STANDARD_TAG = re.compile(r"v(\d{1,4})\.(\d{1,4})\.(\d{1,4})(?:\.(\d{1,4}))?\Z")
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

    version = tag.removeprefix("v")
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
        if match.group(4) is not None:
            # A correction tag gets a version-specific title but can supply
            # standalone notes or reuse the corresponding X.Y.Z notes.
            base = tag.rsplit(".", 1)[0]
            product = "PatchVec" if base in HISTORICAL_PATCHVEC_TITLES else "PaveDB"
            expected = f"{product} {version}"
            if title != expected:
                raise ValueError(f"correction release title must be exactly '{expected}'")
        elif int(match.group(3)) == 0:
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


def release_notes_source(tag: str, root: Path = Path(".")) -> str:
    """Use a correction's own bilingual notes if complete, otherwise X.Y.Z.

    A partly written bilingual correction is an error, not an invitation to
    silently publish stale notes from the base release.
    """
    match = STANDARD_TAG.fullmatch(tag)
    if not match:
        raise ValueError(f"not a release tag: {tag}")
    if match.group(4) is None:
        return tag

    english = root / "docs" / f"RELEASE-{tag}.md"
    portuguese = root / "docs" / f"RELEASE-{tag}_pt.md"
    present = (english.is_file(), portuguese.is_file())
    if all(present):
        return tag
    if any(present):
        missing = portuguese if present[0] else english
        raise ValueError(f"incomplete correction release notes: missing {missing}")
    return tag.rsplit(".", 1)[0]


def prepare_release_announcement(tag: str, root: Path = Path(".")) -> str:
    """Validate bilingual source notes and create GitLab/GitHub release artifacts.

    For vX.Y.Z.W without its own note pair, reuse the X.Y.Z bodies but title
    the published release vX.Y.Z.W. No unvalidated fallback is allowed.
    """
    source_tag = release_notes_source(tag, root)
    source_title = validate_release_announcement(source_tag, root)
    if source_tag != tag:
        product = "PatchVec" if source_title.startswith("PatchVec ") else "PaveDB"
        title = f"{product} {tag.removeprefix('v')}"
        print(f"Reusing {source_tag} release notes for {tag}", file=sys.stderr)
    else:
        title = source_title

    english = (root / "docs" / f"RELEASE-{source_tag}.md").read_text(
        encoding="utf-8"
    )
    portuguese = (root / "docs" / f"RELEASE-{source_tag}_pt.md").read_text(
        encoding="utf-8"
    )
    # The PT file starts with the same H1; omit that duplicated heading.
    portuguese_without_title = "\n".join(portuguese.splitlines()[1:])
    if source_tag != tag:
        # Reuse the body verbatim, but show the *correction* version at its H1.
        english = "# " + title + "\n" + english.partition("\n")[2]
    (root / "release-title.txt").write_text(title + "\n", encoding="utf-8")
    (root / "release-notes.md").write_text(
        english.rstrip("\n") + "\n" + portuguese_without_title.rstrip("\n") + "\n",
        encoding="utf-8",
    )
    return title


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 2 and argv[0] == "--prepare":
            title = prepare_release_announcement(argv[1])
        elif len(argv) == 1:
            title = validate_release_announcement(argv[0])
        else:
            raise SystemExit(
                "usage: validate_release_announcement.py [--prepare] vX.Y.Z[.W]"
            )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(title)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
