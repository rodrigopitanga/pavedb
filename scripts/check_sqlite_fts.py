# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Fail when the runtime SQLite lacks the FTS5 surfaces PaveDB requires."""

from __future__ import annotations

import sqlite3


def main() -> int:
    with sqlite3.connect(":memory:") as conn:
        conn.execute(
            "CREATE VIRTUAL TABLE words USING "
            "fts5(text, tokenize='unicode61')"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE substrings USING "
            "fts5(text, tokenize='trigram')"
        )
        conn.execute("INSERT INTO words(text) VALUES (?)", ("Captain Nemo",))
        conn.execute(
            "INSERT INTO substrings(text) VALUES (?)",
            ("Captain Nemo",),
        )
        prefix_count = conn.execute(
            "SELECT count(*) FROM words WHERE words MATCH ?",
            ('"captain ne" *',),
        ).fetchone()
        if prefix_count != (1,):
            raise RuntimeError("SQLite FTS5 unicode61 prefix query failed")
        contains_count = conn.execute(
            "SELECT count(*) FROM substrings WHERE substrings MATCH ?",
            ('"tain ne"',),
        ).fetchone()
        if contains_count != (1,):
            raise RuntimeError("SQLite FTS5 trigram query failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
