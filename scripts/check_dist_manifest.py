# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
import re
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


SDIST_REQUIRED = {
    "ABOUT.md",
    "CHANGELOG.md",
    "LICENSE",
    "README.md",
    "THIRD_PARTY_LICENSES.md",
    "config.yml.example",
    "docs/guides/README.md",
    "docs/guides/basic-operations.md",
    "docs/guides/components.md",
    "docs/guides/overview.md",
    "docs/guides/running-it.md",
    "docs/reference/cli.md",
    "docs/reference/config.md",
    "docs/reference/pavecli.1",
    "docs/reference/protocols.md",
    "openapi.json",
}


def fail(message: str) -> None:
    raise SystemExit(f"distribution manifest check failed: {message}")


def sdist_files(path: Path) -> set[str]:
    with tarfile.open(path, "r:gz") as archive:
        members = [PurePosixPath(item.name) for item in archive if item.isfile()]
    roots = {item.parts[0] for item in members if item.parts}
    if len(roots) != 1:
        fail(f"{path} must contain one top-level directory")
    return {PurePosixPath(*item.parts[1:]).as_posix() for item in members}


def wheel_files(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as archive:
        return {name for name in archive.namelist() if not name.endswith("/")}


def private_sdist_file(name: str) -> bool:
    return (
        name in {"BLUEPRINT.md", "ROADMAP.md"}
        or name.startswith((".hanzup/", "docs/PLAN-", "docs/design/"))
    )


def wheel_doc_file(name: str) -> bool:
    license_metadata = ".dist-info/licenses/" in name
    return (
        name.startswith("docs/")
        or PurePosixPath(name).name == "openapi.json"
        or (name.lower().endswith(".md") and not license_metadata)
    )


def main(argv: list[str]) -> int:
    paths = [Path(arg) for arg in argv]
    sdists = [path for path in paths if path.name.endswith(".tar.gz")]
    wheels = [path for path in paths if path.suffix == ".whl"]
    if len(sdists) != 1 or len(wheels) != 1 or len(paths) != 2:
        fail("pass exactly one .tar.gz sdist and one .whl wheel")

    sdist, wheel = sdists[0], wheels[0]
    sdist_names = sdist_files(sdist)
    wheel_names = wheel_files(wheel)
    required = set(SDIST_REQUIRED)
    tag = os.environ.get("CI_COMMIT_TAG", "")
    if re.fullmatch(r"v\d{1,4}\.\d{1,4}\.\d{1,4}", tag):
        required.update(
            f"docs/RELEASE-{tag}{suffix}.md" for suffix in ("", "_pt")
        )
    missing = sorted(required - sdist_names)
    if missing:
        fail(f"{sdist} is missing: {', '.join(missing)}")

    leaked = sorted(filter(private_sdist_file, sdist_names))
    if leaked:
        fail(f"{sdist} contains private planning: {', '.join(leaked)}")

    wheel_leaks = sorted(filter(wheel_doc_file, wheel_names))
    if wheel_leaks:
        fail(f"{wheel} contains distribution docs: {', '.join(wheel_leaks)}")

    print(
        f"distribution manifests OK: {sdist.name} "
        f"({len(sdist_names)} files), {wheel.name} ({len(wheel_names)} files)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
