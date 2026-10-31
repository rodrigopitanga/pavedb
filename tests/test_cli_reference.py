# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import argparse
from pathlib import Path
import re

import pytest

from pave.cli import build_parser, main_cli
from pave.version import CONTACT_EMAIL, WEBSITE_URL
from scripts.cli_to_markdown import (
    _fixed_usage_width,
    _roff,
    _top_level_sections,
    _usage,
    main,
    render_manpage,
    render_markdown,
)


ROOT = Path(__file__).resolve().parents[1]


def _usage_without_command(usage):
    # Both the concise synopsis and argparse's expanded usage must retain the
    # required command. Only normalize their different argument placeholders.
    collapsed = " ".join(usage.removeprefix("usage:").split())
    head, cuts = re.subn(r"\sCOMMAND (?:\.\.\.|\[ARG\]\.\.\.)$", "", collapsed)
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


def _usage_option_groups(usage):
    return [
        re.sub(r"\s+", " ", group).strip()
        for group in re.findall(r"\[([^][]+)\]", usage)
    ]


def test_cli_reference_usage_lists_the_options_the_parser_accepts():
    """The reference synopsis must stay in sync with `pavecli --help`."""
    parser = build_parser()
    with _fixed_usage_width():
        real_usage = parser.format_usage()

    assert _usage_option_groups(_usage(parser)) == _usage_option_groups(real_usage)
    assert _usage(parser) == real_usage.removeprefix("usage: ").strip()
    assert _usage_without_command(_usage(parser)) == "pavecli [OPTION]..."


def test_cli_reference_markdown_is_current():
    reference = ROOT / "docs" / "reference" / "cli.md"
    assert reference.read_text(encoding="utf-8") == render_markdown(build_parser())


def test_cli_usage_block_matches_the_parser_options():
    # Expand the concise root synopsis to keep checking every parser option,
    # including required options and positionals rendered without brackets.
    parser = build_parser()
    parser.usage = None
    with _fixed_usage_width():
        generated = parser.format_usage()

    assert _usage_without_command(_usage(parser)) == _usage_without_command(generated)


@pytest.mark.parametrize("usage", [
    "pavecli [OPTION]...",
    "pavecli [-h] [--compact] [--home HOME]",
])
def test_cli_usage_comparison_requires_the_command_tail(usage):
    # Neither synopsis may omit the subcommand the parser requires.
    with pytest.raises(AssertionError):
        _usage_without_command(usage)


def test_cli_help_and_command_help(capsys, monkeypatch):
    monkeypatch.setattr(
        "pave.cli._prepare_runtime",
        lambda args: pytest.fail("help must not open the instance"),
    )

    with pytest.raises(SystemExit) as exc:
        main_cli(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "usage: pavecli [OPTION]... COMMAND [ARG]..." in out
    assert "help" in out and "Show help for a command." in out
    assert f"Report bugs to: {CONTACT_EMAIL}" in out
    assert f"PaveDB home page: {WEBSITE_URL}" in out

    assert main_cli(["help", "search"]) == 0
    assert "pavecli search" in capsys.readouterr().out
    assert main_cli(["help", "admin", "show-key"]) == 0
    assert "pavecli admin show-key" in capsys.readouterr().out
    assert main_cli(["help"]) == 0
    assert "usage: pavecli" in capsys.readouterr().out

    with pytest.raises(SystemExit) as exc:
        main_cli(["help", "unknown"])
    assert exc.value.code == 2
    assert "unknown command: unknown" in capsys.readouterr().err


def test_cli_manpage_is_current_and_covers_nested_commands():
    reference = ROOT / "docs" / "reference" / "pavecli.1"
    text = reference.read_text(encoding="utf-8")
    assert text == render_manpage(build_parser())
    assert ".SS admin show\\-key" in text
    assert ".SH REPORTING BUGS" in text


def test_cli_manpage_options_keep_choice_metavars():
    # An OPTIONS entry must name the same metavar as the synopsis, so a
    # choice-valued option renders as {a,b,c} instead of its upper-case dest.
    text = (ROOT / "docs" / "reference" / "pavecli.1").read_text(encoding="utf-8")
    parser = build_parser()
    checked = []
    for command_parser in (parser, *_command_parsers(parser).values()):
        for action in command_parser._actions:
            if action.metavar or not (action.option_strings and action.choices):
                continue
            names = ", ".join(action.option_strings)
            choices = ",".join(str(choice) for choice in action.choices)
            assert f".B {_roff(names + ' {' + choices + '}')}\n" in text, names
            checked.append(names)
    assert checked


def test_cli_manpage_breaks_literal_trailer_lines():
    # roff fills adjacent input lines into one paragraph, so the pre-1.0 note
    # and the version banner need an explicit .br to keep their line breaks.
    text = (ROOT / "docs" / "reference" / "pavecli.1").read_text(encoding="utf-8")
    for heading in (".SH PRE-1.0 NOTE", ".SH LEGAL NOTICE"):
        body = text.split(f"{heading}\n", 1)[1].split("\n.SH ", 1)[0].splitlines()
        assert len(body) > 1, heading
        assert body[1::2] == [".br"] * (len(body) // 2), heading


def test_top_level_sections_without_epilog():
    parser = argparse.ArgumentParser(prog="pavecli")
    assert _top_level_sections(parser) == ([], [], [])
    assert "## Usage" in render_markdown(parser)
    assert ".SH SYNOPSIS" in render_manpage(parser)


@pytest.mark.parametrize("stale_name", ["cli.md", "pavecli.1"])
def test_cli_reference_generator_checks_both_outputs(tmp_path, capsys, stale_name):
    markdown = tmp_path / "cli.md"
    manpage = tmp_path / "pavecli.1"
    args = ["--output", str(markdown), "--man-output", str(manpage)]

    assert main(args) == 0
    assert main([*args, "--check"]) == 0
    assert capsys.readouterr().err == ""

    stale = tmp_path / stale_name
    stale.write_text("stale reference\n", encoding="utf-8")
    assert main([*args, "--check"]) == 1
    assert f"{stale} is stale" in capsys.readouterr().err

    stale.unlink()
    assert main([*args, "--check"]) == 1
    assert f"{stale} is stale" in capsys.readouterr().err


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
