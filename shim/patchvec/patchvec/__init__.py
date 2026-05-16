# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""patchvec has been renamed to pavedb. Install or import pavedb instead."""

import warnings

__version__ = "0.5.9.1"

warnings.warn(
    "The 'patchvec' package has been renamed to 'pavedb'. Depend on "
    "'pavedb' directly; the 'patchvec' name is a transitional shim and "
    "will not receive further updates.",
    DeprecationWarning,
    stacklevel=2,
)
