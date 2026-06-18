# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from collections import OrderedDict
import textwrap

from .config import CONFIG_FIELDS, ConfigField


def _section_for(key: str) -> str:
    if key in {"data_dir", "dev"} or key.startswith("common_"):
        return "General"
    if key.startswith("auth."):
        return "Authentication"
    if key.startswith("vector_store."):
        return "Vector Store"
    if key.startswith("embedder."):
        return "Embedding"
    if key.startswith("ingest."):
        return "Ingest"
    if key.startswith("search."):
        return "Search"
    if key.startswith("preprocess."):
        return "Text Preprocessing"
    if key.startswith("tenants."):
        return "Tenants"
    if key.startswith("server."):
        return "Server"
    if key.startswith("log."):
        return "Logging"
    if key.startswith("instance."):
        return "Instance"
    return "Other"


def _group_fields(
    fields: tuple[ConfigField, ...] = CONFIG_FIELDS,
) -> OrderedDict[str, list[ConfigField]]:
    grouped: OrderedDict[str, list[ConfigField]] = OrderedDict()
    for field in fields:
        grouped.setdefault(_section_for(field.key), []).append(field)
    return grouped


def _yaml_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _yaml_value(value) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _yaml_quote(value)
    if value == {}:
        return "{}"
    return str(value)


def _markdown_value(value) -> str:
    if value is None:
        rendered = "null"
    elif value is True:
        rendered = "true"
    elif value is False:
        rendered = "false"
    elif isinstance(value, str):
        rendered = '"' + value.replace('"', '\\"') + '"'
    elif value == {}:
        rendered = "{}"
    else:
        rendered = str(value)
    return f"`{rendered}`"


def _comment_lines(text: str, indent: int) -> list[str]:
    prefix = " " * indent + "# "
    return textwrap.wrap(
        text,
        width=88,
        initial_indent=prefix,
        subsequent_indent=prefix,
        break_long_words=False,
    )


def _append_blank(lines: list[str]) -> None:
    if lines and lines[-1] != "":
        lines.append("")


def _emit_parent_lines(
    *,
    lines: list[str],
    field: ConfigField,
    seen_parents: set[str],
) -> int:
    parts = field.key.split(".")
    for index, part in enumerate(parts[:-1]):
        path = ".".join(parts[: index + 1])
        if path not in seen_parents:
            _append_blank(lines)
            lines.append(" " * (index * 2) + f"{part}:")
            seen_parents.add(path)
    return max(0, (len(parts) - 1) * 2)


def _embedder_example_lines() -> list[str]:
    return [
        "    #",
        "    # Additional instance examples. Keep these commented until the",
        "    # provider is configured. If more than one instance is active, set",
        "    # embedder.default above to an instance key or vector-space selector.",
        "    #",
        "    # Credentials live on the instance as a sibling of 'config' (never",
        "    # inside it): 'api_key' is a secret and must not enter the vector-",
        "    # space config. Reference an env var with ${VAR} so the secret stays",
        "    # out of this file; each instance can carry its own key, so you can",
        "    # point several instances at the same provider with different",
        "    # credentials. If 'api_key' is omitted for an openai instance, the",
        "    # OpenAI SDK falls back to the OPENAI_API_KEY environment variable.",
        "    #",
        "    # local-small:",
        "    #   type: 'ollama'",
        "    #   model: 'nomic-embed-text'",
        "    #   config:",
        "    #     dim: 768",
        "    #     base_url: 'http://localhost:11434/v1'",
        "    #",
        "    # local-complex:",
        "    #   type: 'ollama'",
        "    #   model: 'bge-m3'",
        "    #   config:",
        "    #     dim: 1024",
        "    #     base_url: 'http://localhost:11434/v1'",
        "    #",
        "    # frontier-affordable:",
        "    #   type: 'openai'",
        "    #   model: 'text-embedding-3-small'",
        "    #   api_key: '${OPENAI_API_KEY_PRIMARY}'",
        "    #   config:",
        "    #     dim: 1536",
        "    #     batch_size: 256",
        "    #     # base_url: '${PAVEDB_OPENAI_BASE_URL}'",
        "    #",
        "    # frontier-secondary:",
        "    #   type: 'openai'",
        "    #   model: 'text-embedding-3-large'",
        "    #   api_key: '${OPENAI_API_KEY_SECONDARY}'",
        "    #   config:",
        "    #     dim: 3072",
    ]


def render_config_example(
    fields: tuple[ConfigField, ...] = CONFIG_FIELDS,
) -> str:
    lines = [
        "# PaveDB sample configuration",
        "# Generated from pave.config.CONFIG_FIELDS.",
        "# Regenerate with: make docs-refresh",
        "#",
        "# Config file location override:",
        "#   PAVEDB_CONFIG=/etc/pavedb/config.yml",
        "#",
        "# Every config key can also be overridden with an environment variable.",
        "# Nested keys use double underscores, for example:",
        "#   PAVEDB_AUTH__MODE=static",
        "#   PAVEDB_LOG__LEVEL=debug",
        "#",
        "# Tenant sidecar precedence:",
        "#   env vars > config.yml > tenants.yml > defaults",
    ]

    for section, section_fields in _group_fields(fields).items():
        lines.extend(["", "# " + "-" * 75, f"# {section}", "# " + "-" * 75])
        _append_blank(lines)
        seen_parents: set[str] = set()
        for field in section_fields:
            indent = _emit_parent_lines(
                lines=lines,
                field=field,
                seen_parents=seen_parents,
            )
            _append_blank(lines)
            lines.extend(_comment_lines(field.description, indent))
            lines.append(" " * indent + f"# Env: {field.env_var}")
            key = field.key.split(".")[-1]
            rendered = _yaml_value(field.default)
            commented = "# " if field.default is None else ""
            lines.append(" " * indent + f"{commented}{key}: {rendered}")
        if section == "Embedding":
            _append_blank(lines)
            lines.extend(_embedder_example_lines())
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _markdown_bullet(label: str, text: str) -> list[str]:
    return textwrap.wrap(
        f"- {label}: {text}",
        width=88,
        subsequent_indent="  ",
        break_long_words=False,
    )


def render_markdown(
    fields: tuple[ConfigField, ...] = CONFIG_FIELDS,
) -> str:
    lines = [
        "<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->",
        "<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->",
        "",
        "# PaveDB Config Reference",
        "",
        "Generated from `pave.config.CONFIG_FIELDS`. Regenerate with "
        "`make docs-refresh`.",
        "",
        "Environment variables use the `PAVEDB_` prefix. Nested config keys",
        "replace dots with double underscores, so `auth.mode` becomes",
        "`PAVEDB_AUTH__MODE`.",
        "",
    ]

    for section, section_fields in _group_fields(fields).items():
        lines.extend([f"## {section}", ""])
        if section == "Embedding":
            lines.extend([
                "Embedder instances are configured under `embedder.instances`.",
                "Instance names are operator-defined. `config.yml.example`",
                "shows commented Ollama and OpenAI examples; only the",
                "`native` instance is active by default.",
                "",
            ])
        for field in section_fields:
            lines.extend([
                f"### `{field.key}`",
                "",
                f"- Default: {_markdown_value(field.default)}",
                f"- Env var: `{field.env_var}`",
                *_markdown_bullet("Description", field.description),
                "",
            ])

    return "\n".join(lines).rstrip() + "\n"
