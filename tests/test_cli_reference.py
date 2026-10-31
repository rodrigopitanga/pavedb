# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path
import re

import pytest

from pave.cli import build_parser
from scripts.cli_to_markdown import _fixed_usage_width, _usage, render_markdown


ROOT = Path(__file__).resolve().parents[1]


def _usage_without_command(usage):
    # Collapse the wrapping and cut the subcommand tail, which the pin writes as
    # <command> ... where argparse inlines every choice. The cut has to match:
    # an optional one would let the pin drop the tail and still compare equal,
    # publishing a synopsis that omits the subcommand the parser requires.
    collapsed = " ".join(usage.removeprefix("usage:").split())
    head, cuts = re.subn(r"\s(?:<command>|\{[^{}]*\}) \.\.\.$", "", collapsed)
    assert cuts == 1, f"no subcommand tail in usage: {collapsed!r}"
    return head


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


def test_cli_usage_block_matches_the_parser_options():
    # The pavecli synopsis is pinned by hand in scripts/cli_to_markdown.py so the
    # Usage block stays readable instead of inlining every subcommand choice. The
    # pin only diverges from argparse in wrapping and the <command> placeholder,
    # so every other part it shows must be one the parser accepts -- including
    # required options and positionals, which argparse renders unbracketed.
    parser = build_parser()
    with _fixed_usage_width():
        generated = parser.format_usage()

    assert _usage_without_command(_usage(parser)) == _usage_without_command(generated)


def test_cli_usage_comparison_requires_the_command_tail():
    # Cutting the tail is what lets the pin stay readable, so the cut must be
    # the only thing it hides. A pin without "<command> ..." has to fail the
    # comparison above instead of matching a parser that requires a subcommand.
    with pytest.raises(AssertionError):
        _usage_without_command("pavecli [-h] [--compact] [--home HOME]")


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
