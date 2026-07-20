# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from setuptools import setup, find_packages
from pathlib import Path
import os

here = Path(__file__).parent.resolve()


def read_version():
    if os.environ.get("PAVEDB_BUILD_VERSION"):
        return os.environ["PAVEDB_BUILD_VERSION"]
    ns = {}
    exec((here / "pave" / "version.py").read_text(encoding="utf-8"), ns)
    return ns["VERSION"]


def read_long_description():
    for candidate in ("ABOUT.md", "PYPI_DESCRIPTION.md", "README.md"):
        path = here / candidate
        if path.exists():
            return path.read_text(encoding="utf-8"), "text/markdown"
    return "PaveDB — A lightweight, pluggable vector search microservice.", "text/plain"

long_description, long_type = read_long_description()

setup(
    name="pavedb",                        # external name
    version=read_version(),
    description="PaveDB — A lightweight, pluggable vector search microservice.",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="Rodrigo Rodrigues da Silva",
    author_email="rodrigo@flowlexi.com",
    license="AGPL-3.0-or-later",
    python_requires=">=3.10,<3.15",
    packages=find_packages(include=["pave", "pave.*"]),  # internal package
    include_package_data=True,
    package_data={
        "pave.config": ["config_fields.json"],
        "pave.assets": ["*.png", "*.ico", "*.html", "tenants.yml.example"],
    },
    install_requires=[
        "fastapi>=0.115.0,<1",
        "uvicorn[standard]>=0.30.6",
        "pydantic>=2.8.2,<3",
        "python-multipart>=0.0.9",
        "pypdf>=5.0.0",
        "pyyaml>=6.0.2",
        "python-dotenv>=1.0.1",
        "openai>=1.0.0,<3",
        "pavedb-sdk>=0.1.0,<0.2.0",
        "numpy>=2.0",
        "faiss-cpu>=1.7.1",
        "sentence-transformers>=5.0,<6",
    ],
    extras_require={
        "cpu": [],
        "test": [
            "pytest",
            "httpx",
            "datasets>=3.5.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "pavesrv=pave.main:main_srv",
            "pavecli=pave.cli:main_cli",
        ],
        "pavesdk.providers": [
            "pave=pave.localclient:provider",
        ],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Programming Language :: Python :: 3.14",
        "Framework :: FastAPI",
        "Topic :: Database",
        "Topic :: Internet :: WWW/HTTP :: HTTP Servers",
    ],
    project_urls={
        "Homepage": "https://github.com/rodrigopitanga/pavedb",
        "Source": "https://github.com/rodrigopitanga/pavedb",
        "Tracker": "https://github.com/rodrigopitanga/pavedb/issues",
    },
)
