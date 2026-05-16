# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from setuptools import setup
from pathlib import Path

here = Path(__file__).parent.resolve()
long_description = (here / "README.md").read_text(encoding="utf-8")

setup(
    name="patchvec",
    version="0.5.9.1",
    description=(
        "Transitional shim — patchvec has been renamed to pavedb. "
        "Installing this package pulls in pavedb."
    ),
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="Rodrigo Rodrigues da Silva",
    author_email="rodrigo@flowlexi.com",
    license="AGPL-3.0-or-later",
    python_requires=">=3.10,<3.15",
    packages=["patchvec"],
    install_requires=["pavedb>=0.5.9"],
    project_urls={
        "Homepage": "https://github.com/rodrigopitanga/pavedb",
        "Source": "https://github.com/rodrigopitanga/pavedb",
    },
    classifiers=[
        "Development Status :: 7 - Inactive",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3 :: Only",
    ],
)
