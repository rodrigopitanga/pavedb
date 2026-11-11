#!/usr/bin/env python3
# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
update_changelog.py — Prepend a version section to CHANGELOG.md based on commit
subjects since the most recent tag referenced in CHANGELOG.md.

Fallback: if no changelog-referenced tag exists locally, use the last tag in
history.

Usage:
  python scripts/update_changelog.py <VERSION>
"""
from __future__ import annotations
import subprocess, sys, datetime, pathlib, re, os

def sh(*args: str, stderr: int | None = None) -> str:
    out = subprocess.check_output(args, text=True, stderr=stderr).strip()
    return out


def _version_base(v: str) -> str:
    m = re.match(r"^(\d+\.\d+\.\d+)(?!\.\d)(.*)$", v)
    return m.group(1) if m else v


def _tag_exists(tag: str) -> bool:
    try:
        subprocess.check_call(
            ["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def _tag_candidates(version: str) -> list[str]:
    v = version.strip()
    if not v:
        return []
    out = [f"v{v}", v] if not v.startswith("v") else [v, v[1:]]
    seen: set[str] = set()
    dedup: list[str] = []
    for t in out:
        if t and t not in seen:
            dedup.append(t)
            seen.add(t)
    return dedup


def _versions_in_changelog(path: pathlib.Path) -> list[str]:
    if not path.exists():
        return []
    txt = path.read_text(encoding="utf-8")
    versions: list[str] = []
    for line in txt.splitlines():
        m = re.match(r"^##\s+([0-9A-Za-z\.\-]+)\s+—", line)
        if m:
            versions.append(m.group(1).strip())
    return versions


def _anchor_tag_from_changelog(path: pathlib.Path, version: str) -> str:
    # Skip the current version family (e.g., 0.5.8rc0 when generating 0.5.8).
    base = _version_base(version)
    for ver in _versions_in_changelog(path):
        if _version_base(ver) == base:
            continue
        for cand in _tag_candidates(ver):
            if _tag_exists(cand):
                return cand
    return ""


def _anchor_tag_from_history() -> str:
    try:
        return sh(
            "git", "describe", "--tags", "--abbrev=0", stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError:
        # Fallback: newest tag by creation date (may be unrelated if history diverged)
        try:
            tags = sh(
                "git", "for-each-ref", "--sort=-creatordate",
                "--format=%(refname:short)", "refs/tags", stderr=subprocess.DEVNULL
            )
            return tags.splitlines()[0].strip() if tags else ""
        except subprocess.CalledProcessError:
            return ""


def _reporters(rev_range: str | None) -> list[str]:
    """Reported-by trailer values in the range, first appearance first."""
    args = ["git", "log", "--reverse",
            "--format=%(trailers:key=Reported-by,valueonly,separator=%x0a)"]
    out = sh(*args, rev_range) if rev_range else sh(*args)
    return list(dict.fromkeys(l.strip() for l in out.splitlines() if l.strip()))


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: update_changelog.py <VERSION>", file=sys.stderr)
        return 2
    version = sys.argv[1]
    path = pathlib.Path(
        os.environ.get("CHANGELOG_PATH", "CHANGELOG.md")
    )

    # Prefer anchor tag from CHANGELOG, then fallback to latest history tag.
    last_tag = _anchor_tag_from_changelog(path, version) or _anchor_tag_from_history()

    if last_tag:
        log = sh("git", "log", "--reverse", "--format=%s", f"{last_tag}..HEAD")
    else:
        log = sh("git", "log", "--reverse", "--format=%s")

    lines = [l.strip() for l in log.splitlines() if l.strip()]

    # Keep only lines that start with [tag]; skip chore: and other non-matching formats.
    tag_pat = re.compile(r"^\[([^\]]+)\]")
    norm_map = {
        "api": "API",
        "buid": "Build",
        "build": "Build",
        "bite": "Bite-sized tasks",
        "doc": "Documentation",
        "docs": "Documentation",
        "conf": "Config",
        "core": "Core",
        "sdk": "SDK",
        "store": "Store",
        "infra": "Infrastructure",
        "ci": "Infrastructure",
        "pkg": "Packaging",
        "ui": "UI",
        "fix": "Bug Fixes",
        "perf": "Performance",
        "performance": "Performance",
        "test": "Testing",
        "cli": "CLI",
    }
    merge_map = {
        "bench": "performance",
    }
    groups: dict[str, list[str]] = {}
    titles: dict[str, str] = {}
    seen_groups: set[str] = set()
    for line in lines:
        low = line.lower()
        if low.startswith("chore(release"):
            continue
        if low.startswith("chore:") or low.startswith("chore("):
            continue
        if not line.startswith("["):
            continue
        tags = tag_pat.findall(line)
        if not tags:
            continue
        grp = tags[0].strip()
        key = grp.lower()
        key = merge_map.get(key, key)
        grp = norm_map.get(key, grp)
        if grp == grp.lower():
            grp = grp[:1].upper() + grp[1:] if grp else grp
        grp_key = grp.lower()
        if grp_key not in titles:
            titles[grp_key] = grp
        remainder = line
        # Strip the first tag only; keep any secondary tags in the bullet.
        remainder = tag_pat.sub("", remainder, count=1).lstrip()
        if not remainder:
            continue
        # Crop to 80 chars total; if longer, crop at 77 and add "..."
        if len(remainder) > 80:
            remainder = remainder[:77].rstrip() + "..."
        # Capitalize first letter of the commit line
        if remainder:
            remainder = remainder[:1].upper() + remainder[1:]
        key = f"{grp_key}::{remainder.lower()}"
        if key in seen_groups:
            continue
        seen_groups.add(key)
        groups.setdefault(grp_key, []).append(remainder)

    # Sort groups by size (desc), then name (asc). Entries remain in commit order.
    sorted_groups = sorted(
        groups.items(),
        key=lambda kv: (-len(kv[1]), titles[kv[0]].lower())
    )

    today = datetime.date.today().isoformat()
    header = f"## {version} — {today}\n"
    body_lines: list[str] = []
    for grp_key, msgs in sorted_groups:
        title = titles.get(grp_key, grp_key)
        body_lines.append(f"\n### {title}")
        for msg in msgs:
            body_lines.append(f"- {msg}")
    reporters = _reporters(f"{last_tag}..HEAD" if last_tag else None)
    if reporters:
        body_lines.append(
            "\nThanks to the developers who reported or contributed issue "
            "reports: " + ", ".join(reporters) + "."
        )
    body = "\n".join(body_lines).lstrip("\n") + "\n"
    sep = "\n---\n"

    prev = path.read_text(encoding="utf-8") if path.exists() else ""

    # Insert after initial comment header (lines starting with <!-- ... -->).
    lines_prev = prev.splitlines()

    # If an entry for this version (or related rc/final variants) already exists,
    # remove it before inserting the new block. Keep distinct dotted patchlines
    # like X.Y.Z.W as separate versions.
    base = _version_base(version)
    block_start = None
    i = 0
    while i < len(lines_prev):
        line = lines_prev[i]
        if line.startswith("## "):
            m = re.match(r"^##\s+([0-9A-Za-z\.\-]+)\s+—", line)
            ver = m.group(1) if m else ""
            if ver and _version_base(ver) == base:
                block_start = i
                j = i + 1
                while j < len(lines_prev):
                    if lines_prev[j].strip() == "---":
                        j += 1
                        break
                    j += 1
                del lines_prev[i:j]
                continue
        i += 1

    insert_at = 0
    while insert_at < len(lines_prev) and lines_prev[insert_at].lstrip().startswith("<!--"):
        insert_at += 1
    # Preserve a single blank line after header block if present.
    if insert_at < len(lines_prev) and lines_prev[insert_at].strip() == "":
        insert_at += 1
    # Avoid duplicate separators if the next line is already a separator.
    next_is_sep = insert_at < len(lines_prev) and lines_prev[insert_at].strip() == "---"
    new_entry = header + "\n" + body + ("" if next_is_sep else sep)
    if prev:
        lines_prev.insert(insert_at, new_entry.rstrip("\n"))
        out = "\n".join(lines_prev).rstrip() + "\n"
    else:
        out = new_entry
    path.write_text(out, encoding="utf-8")

    if os.environ.get("CHANGELOG_SILENT", "").strip().lower() not in {"1", "true", "yes"}:
        print(f"{path.name} updated for {version}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
