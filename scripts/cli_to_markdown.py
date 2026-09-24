# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import argparse
import contextlib
import os
from pathlib import Path
import sys
import textwrap

# Pin argparse's usage wrapping width so generated output is deterministic
# regardless of the terminal it runs in (argparse reads COLUMNS / terminal size).
_USAGE_COLUMNS = "80"


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pave.version import PRODUCT, PRODUCT_EMOJI

DEFAULT_OUTPUT = ROOT / "docs" / "reference" / "cli.md"


def _load_cli_parser():
    from pave.cli import build_parser

    return build_parser()


def _subparser_action(parser):
    for action in parser._actions:
        if _is_subparser_action(action):
            return action
    return None


def _is_subparser_action(action):
    return (
        getattr(action, "dest", None) == "cmd"
        and isinstance(getattr(action, "choices", None), dict)
    )


def _visible_actions(parser):
    return [
        action
        for action in parser._actions
        if not isinstance(action, argparse._HelpAction)
        and not _is_subparser_action(action)
        and action.help is not argparse.SUPPRESS
    ]


@contextlib.contextmanager
def _fixed_usage_width():
    previous = os.environ.get("COLUMNS")
    os.environ["COLUMNS"] = _USAGE_COLUMNS
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("COLUMNS", None)
        else:
            os.environ["COLUMNS"] = previous


def _usage(parser):
    if parser.prog == "pavecli":
        return (
            "pavecli [-h] [--compact] [--version] [--home HOME]\n"
            "        [--config CONFIG]\n"
            "        [--tenants TENANTS] [--data-dir DATA_DIR] <command> ..."
        )
    with _fixed_usage_width():
        usage = parser.format_usage().strip()
    return usage.removeprefix("usage: ").strip()


def _top_level_sections(parser):
    if not parser.epilog:
        return [], []
    sections = []
    current = []
    for raw in parser.epilog.splitlines():
        line = raw.strip()
        if line:
            current.append(line)
        elif current:
            sections.append(current)
            current = []
    if current:
        sections.append(current)

    pre_release = []
    legal = []
    for section in sections:
        if section[0].lower() == "pre-1.0 note:":
            pre_release = section[1:]
        else:
            legal.extend(section)
    return pre_release, legal


def _escape(value):
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _action_name(action):
    if action.option_strings:
        return ", ".join(f"`{option}`" for option in action.option_strings)
    return f"`{action.metavar or action.dest}`"


def _is_required(action):
    if action.option_strings:
        return bool(getattr(action, "required", False))
    return action.nargs not in ("?", "*")


def _action_description(action):
    help_text = _escape(action.help) or ""
    meta = []
    choices = getattr(action, "choices", None)
    if choices:
        meta.append("choices: " + ", ".join(str(choice) for choice in choices))
    default = getattr(action, "default", None)
    if default not in (None, False, argparse.SUPPRESS):
        meta.append(f"default: {default}")
    if not meta:
        return help_text
    suffix = "(" + "; ".join(meta) + ")"
    return f"{help_text} {suffix}".strip()


def _wrapped_bullet(name, required, description):
    prefix = f"- {name} ({required}): "
    return textwrap.wrap(
        description,
        width=88,
        initial_indent=prefix,
        subsequent_indent="  ",
        break_long_words=False,
    )


def _options_block(parser):
    actions = _visible_actions(parser)
    if not actions:
        return []

    lines = []
    for action in actions:
        required = "required" if _is_required(action) else "optional"
        lines.extend(
            _wrapped_bullet(
                _action_name(action),
                required,
                _action_description(action),
            )
        )
    return lines


def _examples(parser):
    if not parser.epilog:
        return []
    examples = []
    for raw in parser.epilog.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.strip() in {"example:", "examples:"}:
            continue
        if line.startswith("  pavecli "):
            line = line[2:]
        examples.append(line)
    return examples


def _command_help(sub_action):
    return {
        choice.dest: choice.help or ""
        for choice in sub_action._choices_actions
    }


def render_markdown(
    parser: argparse.ArgumentParser | None = None,
) -> str:
    parser = parser or _load_cli_parser()
    sub_action = _subparser_action(parser)
    commands = sub_action.choices if sub_action is not None else {}
    command_help = _command_help(sub_action) if sub_action is not None else {}

    lines = [
        "<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->",
        "<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->",
        "",
        f"# {PRODUCT_EMOJI} {PRODUCT} CLI Reference",
        "",
        "Command reference for `pavecli`, the local PaveDB administration and",
        "data-loading tool.",
        "",
        "Generated from `pave.cli.build_parser`. Regenerate with "
        "`make docs-refresh`.",
        "",
        "## Common Flow",
        "",
        "```bash",
        "pavecli init",
        "pavecli create-collection demo/books",
        "pavecli ingest demo/books ./book.txt --docid book-1",
        'pavecli search demo/books "captain nemo" -k 3',
        "```",
        "",
        "## Usage",
        "",
        "```bash",
        _usage(parser),
        "```",
        "",
    ]

    note, legal = _top_level_sections(parser)
    if note:
        lines.extend(["## Pre-1.0 Note", "", *note, ""])
    if legal:
        lines.extend(["## Legal Notice", "", *legal, ""])

    global_options = _options_block(parser)
    if global_options:
        lines.extend(["## Global Options", "", *global_options, ""])

    if commands:
        lines.extend([
            "## Commands",
            "",
            "| Command | Description |",
            "|---|---|",
        ])
        for name in commands:
            lines.append(f"| `{name}` | {_escape(command_help.get(name))} |")
        lines.append("")

    for name, command_parser in commands.items():
        lines.extend([
            f"## `{name}`",
            "",
        ])
        if command_parser.description:
            lines.extend([
                textwrap.fill(_escape(command_parser.description), width=88),
                "",
            ])
        lines.extend([
            "### Usage",
            "",
            "```bash",
            _usage(command_parser),
            "```",
            "",
        ])
        examples = _examples(command_parser)
        if examples:
            label = "Example" if len(examples) == 1 else "Examples"
            lines.extend([f"### {label}", "", "```bash"])
            lines.extend(examples)
            lines.extend(["```", ""])
        options = _options_block(command_parser)
        if options:
            lines.extend(["### Arguments And Options", "", *options, ""])

    return "\n".join(lines).rstrip() + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Render pavecli reference markdown.")
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help="Markdown output path",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the output file is stale",
    )
    args = parser.parse_args(argv)

    output = Path(args.output)
    rendered = render_markdown()
    if args.check:
        current = output.read_text(encoding="utf-8") if output.exists() else ""
        if current != rendered:
            print(f"{output} is stale; run `make docs-refresh`.", file=sys.stderr)
            return 1
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
