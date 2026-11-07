# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Private creation defaults without changing existing installation permissions."""

import os
from pathlib import Path


def mkdir_private(path: Path) -> None:
    """Create missing directories with mode 0700, including missing parents."""
    try:
        path.mkdir(mode=0o700)
    except FileNotFoundError:
        mkdir_private(path.parent)
        mkdir_private(path)
    except FileExistsError:
        if not path.is_dir():
            raise


def create_private_file(path: Path) -> None:
    """Reserve a new file as 0600 before a library writes it; keep existing files."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    os.close(fd)


def make_private_tree(path: Path) -> None:
    """Restrict a newly staged data tree before publishing it."""
    if os.name != "posix":
        return
    for root, directories, files in os.walk(path):
        os.chmod(root, 0o700)
        for name in directories:
            os.chmod(Path(root) / name, 0o700)
        for name in files:
            os.chmod(Path(root) / name, 0o600)
