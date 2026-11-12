<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# CLI syntax and Unix conventions audit

This records the [R2-93](https://gitlab.com/flowlexi/pavedb/-/issues/342)
review of `pavecli` against the POSIX Utility Syntax Guidelines and the Unix
rules of composition, least surprise, and silence. The audit covers the parser
and command entry point in `pave/cli.py`, the generated command reference, and
the documented examples. It does not require `pavecli` to be a strictly POSIX
utility: subcommands and descriptive long options are useful extensions.

## Current contract

- **Names:** `pavecli` is a lowercase seven-character name. Short options
  are single letters. Descriptive long options extend POSIX syntax.
- **Values:** Examples separate options and values. `argparse` also accepts
  attached forms; the examples use the clearer separated form.
- **Option names:** Long options require their complete spelling at every
  command level.
- **Order:** Instance selectors precede the command. Command options may
  follow operands, as in `search demo/books query -k 3`. Scripts should place
  options before operands to follow the POSIX guideline.
- **End of options:** `argparse` recognizes `--` within a command, so values
  beginning with `-` can be passed as operands.
- **Discovery:** `-h`/`--help` and `-V`/`--version` exit without opening the
  store. R2-92 owns the help text and man page.
- **Input:** `ingest-batch` and `restore-archive` accept `-` to read bytes from
  standard input. `ingest` requires a named file for source metadata.
- **Output:** Structured results, including mutation receipts, are JSON;
  `get-chunk-content`, `admin show-key`, and `admin rotate-key` emit raw
  bytes on success. Service and file errors emit JSON to stderr. `--compact`
  removes JSON indentation. The receipt is intentional output for scripts
  despite the Unix silence rule.
- **Status:** Success returns 0, service failures return 1, and argparse
  usage errors return 2, including `init` usage errors.
- **Side effects:** Mutations have explicit verbs. `init --force` is
  required to overwrite existing config files.

## Resolved findings

The findings below describe the audited behavior before
[R2-104](https://gitlab.com/flowlexi/pavedb/-/work_items/356).
All five are implemented; the current contract above reflects the result.

1. **Long option prefixes are accepted implicitly.** `argparse` defaults to
   `allow_abbrev=True`, so a unique prefix such as `--compa` currently means
   `--compact`. Adding another option can silently change that meaning. Set
   `allow_abbrev=False` on the root, every command parser, and the nested
   `admin` subparsers, then cover all three levels with parser tests.
   Document only the complete option names.
2. **Service errors go to stdout.** `main_cli()` prints a JSON error object
   through `_dump()` and then returns 1. Parser errors already go to stderr.
   `admin show-key` shows the cost directly: the key goes to stdout on
   success and a JSON error object goes to the same stream on failure.
   Send service and data-directory errors to stderr while retaining their JSON
   shape and status; reserve stdout for successful data and raw content.
   `init`'s own two usage errors already reach stderr, but as a bare
   `SystemExit` message with status 1; give them the status 2 that argparse
   uses for usage errors. Update CLI tests and scripting docs together
   because scripts may parse the current error stream.
3. **File operands do not accept `-` for standard input.** `_read()` treats it
   as a literal path. `ingest-batch` and `restore-archive` can read bytes from
   stdin without a filename dependency. `ingest` also passes the filename to
   the ingestion service, so its stdin contract needs an explicit source-name
   and format decision before adding `-` there. Add examples and tests for
   each supported command.
4. **Several examples put options after operands.** The parser accepts this,
   but POSIX recommends options first. Keep existing invocations working and
   use the options-first form in new examples. The command reference is
   generated from parser metadata, so change examples at their source in
   `pave/cli.py` rather than editing `docs/reference/cli.md` directly.
5. **File read errors reach the user as tracebacks.** `_read()` lets
   `FileNotFoundError` and `PermissionError` escape `main_cli()`, which
   only catches `ServiceError` and `DataDirInUseError`, so
   `pavecli restore-archive missing.zip` prints a Python traceback instead
   of an error object. Convert the failure into the same JSON error shape
   and status 1 when the stdin work in finding 3 touches this helper.
