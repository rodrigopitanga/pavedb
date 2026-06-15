# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

import pytest

from pave.cli import build_parser
from scripts.cli_to_markdown import render_markdown


ROOT = Path(__file__).resolve().parents[1]


def _command_parsers(parser):
    for action in parser._actions:
        if getattr(action, "dest", None) == "cmd" and hasattr(action, "choices"):
            return action.choices
    return {}


def test_cli_subcommands_have_reference_metadata(capsys):
    commands = _command_parsers(build_parser())
    assert commands

    for name, parser in commands.items():
        assert parser.description, name
        assert parser.epilog and "pavecli " in parser.epilog, name
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["--help"])
        assert exc.value.code == 0
        assert name in capsys.readouterr().out


def test_cli_reference_markdown_is_current():
    reference = ROOT / "docs" / "reference" / "cli.md"
    assert reference.read_text(encoding="utf-8") == render_markdown(build_parser())


def test_cli_reference_respects_line_length_policy():
    reference = ROOT / "docs" / "reference" / "cli.md"
    long_lines = [
        (idx, line)
        for idx, line in enumerate(
            reference.read_text(encoding="utf-8").splitlines(),
            start=1,
        )
        if len(line) > 88
    ]
    assert long_lines == []
