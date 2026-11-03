# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Where the CPU install is allowed to look for the wheels it resolves.

Three CI jobs, the Dockerfile and `make install TORCH_WHEEL_CPU=1` install
through this one file, so what it configures is the install path of every
CPU build. pip queries every configured index for every project it
resolves, and download.pytorch.org carries torch and a few of torch's own
dependencies — not fastapi, not anyio, not scipy. Naming it as an
--index-url therefore bought 68 lookups there for this closure, 51 of them
answered 403, one serial round trip each. Bench job 17794168368 spent its
whole budget in that step: /whl/cpu/annotated-doc/, /whl/cpu/anyio/ and
/whl/cpu/scipy/ timed out between 04:56 and 05:16, the token expired at
71.4m, and no latency or stress run ever started.

The same page reached through --find-links costs 2 requests and resolves
the same torch 2.15.0+cpu wheel, since a +cpu local label outranks PyPI's
plain wheel of that version. Neither half of that is safe to drift: the
host cannot come back as an index without the fan-out coming back with it,
and it cannot leave either, because PyPI alone resolves 15 nvidia-* CUDA
packages on x86_64 and torch 2.1.0 on aarch64.
"""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

REQUIREMENTS = ROOT / "requirements-cpu.txt"

# The one index pip may query per project. Pinning the whole list rather
# than only the absence of the PyTorch host from --index-url: as an
# --extra-index-url that host is read for every project just the same.
INDEX_URL = "https://pypi.org/simple"

# A project page, not the index root: `.../whl/cpu` handed to --find-links
# is one page of no links, and handed to an index it is the fan-out again.
TORCH_FIND_LINKS = "https://download.pytorch.org/whl/cpu/torch/"

TORCH_HOST = "download.pytorch.org"

INDEX_OPTIONS = ("-i", "--index-url", "--extra-index-url")
FIND_LINKS_OPTIONS = ("-f", "--find-links")


def _lines():
    text = REQUIREMENTS.read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = re.sub(r"(?:^|\s)#.*$", "", raw).strip()
        if line:
            yield line


def _options():
    """The pip options the file sets, as (option, value) pairs, in order."""
    options = []
    for line in _lines():
        if line.startswith("-"):
            option, _, value = line.replace("=", " ", 1).partition(" ")
            options.append((option, value.strip()))
    return options


def _requirements():
    """What the file asks pip to install, options dropped."""
    return [line for line in _lines() if not line.startswith("-")]


def test_pypi_is_the_only_index_the_cpu_install_queries():
    """Every index named here is read for all 66 projects in the closure,
    so a second one is a second round trip per project — 68 of them on a
    host that answers 403 for all but torch and a handful of its deps."""
    indexes = [(o, v) for o, v in _options() if o in INDEX_OPTIONS]

    assert indexes == [("--index-url", INDEX_URL)]


def test_the_torch_wheels_come_from_one_project_page():
    """--find-links fetches the page it is given and nothing else, which is
    the whole difference: 2 requests to that host for the one project it
    actually serves, instead of one lookup per project in the resolve."""
    find_links = [(o, v) for o, v in _options() if o in FIND_LINKS_OPTIONS]

    assert find_links == [("--find-links", TORCH_FIND_LINKS)]
    assert [
        (o, v) for o, v in _options()
        if TORCH_HOST in v and o not in FIND_LINKS_OPTIONS
    ] == []


def test_the_cpu_extra_stays_the_only_requirement():
    """setup.py is the one place dependency versions are stated. A torch
    pin added here would fork that, and it is not what selects the CPU
    wheel anyway — the +cpu label on the page outranks PyPI's plain one."""
    assert _requirements() == [".[cpu]"]
