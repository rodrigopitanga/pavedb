# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

VERSION = "0.9.5"

PRODUCT = "PaveDB"
PRODUCT_EMOJI = "🛣️"
PRODUCT_ICON_PNG = "/assets/pavedb_icon_192.png"
PRODUCT_FAVICON = "/favicon.ico"
REPOSITORY_URL = "https://gitlab.com/flowlexi/pavedb"
CONTACT_EMAIL = "pavedb@flowlexi.com"
COPYRIGHT = (
    "Copyright (C) 2025, 2026 Rodrigo Rodrigues da Silva "
    "<rodrigo@flowlexi.com>"
)
LICENSE = "AGPL-3.0-or-later"
LICENSE_URL = "https://www.gnu.org/licenses/agpl-3.0.html"
NO_WARRANTY = "There is NO WARRANTY, to the extent permitted by law."

# Custom deployments may switch this off without touching parser code.
SHOW_LEGAL_NOTICE = True


def product_label(*, emoji: bool = False) -> str:
    return f"{PRODUCT_EMOJI}  {PRODUCT}" if emoji else PRODUCT


def product_display_name(instance_name: str | None = None) -> str:
    instance = str(instance_name or "").strip()
    if not instance or instance == PRODUCT:
        return PRODUCT
    return f"{PRODUCT} • {instance}"


def software_title(program: str, *, emoji: bool = False) -> str:
    return f"{product_label(emoji=emoji)} {VERSION} ({program})"


def legal_notice(*, include_license: bool = False) -> str:
    if not SHOW_LEGAL_NOTICE:
        return ""
    lines = [COPYRIGHT, NO_WARRANTY]
    if include_license:
        lines.insert(1, f"License {LICENSE} <{LICENSE_URL}>")
    return "\n".join(lines)


def help_notice(program: str) -> str:
    notice = legal_notice()
    if not notice:
        return ""
    return f"{software_title(program)}\n{notice}"


def version_banner(program: str) -> str:
    notice = legal_notice(include_license=True)
    if not notice:
        return software_title(program)
    return (
        f"{software_title(program)}\n"
        f"{notice}"
    )
