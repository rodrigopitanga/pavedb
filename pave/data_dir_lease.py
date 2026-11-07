# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import errno
import logging
import os
from pathlib import Path
from typing import BinaryIO

from pave.filesystem import create_private_file, mkdir_private


log = logging.getLogger(__name__)

DATA_DIR_LOCK_NAME = ".pavedb.lock"


class DataDirInUseError(RuntimeError):
    """Raised when another owner holds a local PaveDB data directory."""


class DataDirLease:
    """Non-blocking process lease for one canonical local data directory."""

    def __init__(self, data_dir: str | os.PathLike[str]) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.path = self.data_dir / DATA_DIR_LOCK_NAME
        self._handle: BinaryIO | None = None

    def acquire(self) -> DataDirLease:
        if self._handle is not None:
            return self
        mkdir_private(self.data_dir)
        if os.name == "posix" and self.data_dir.stat().st_mode & 0o077:
            log.warning(
                f"Data directory {self.data_dir} permits access by other users; "
                "existing permissions are unchanged. Restrict it to mode 0700 "
                "or protect it with an owner-only parent directory."
            )
        create_private_file(self.path)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if handle.seek(0, os.SEEK_END) == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if isinstance(exc, BlockingIOError) or exc.errno in {
                errno.EACCES,
                errno.EAGAIN,
            }:
                raise DataDirInUseError(
                    f"data directory is already in use: {self.data_dir}; "
                    "stop its current PaveDB owner before opening it locally, "
                    "or use an HTTP client"
                ) from None
            raise
        self._handle = handle
        return self

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> DataDirLease:
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.close()
