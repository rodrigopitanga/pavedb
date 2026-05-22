# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from setuptools import setup, find_packages
from pathlib import Path

here = Path(__file__).parent.resolve()

def read_long_description():
    for candidate in ("ABOUT.md", "PYPI_DESCRIPTION.md", "README.md"):
        path = here / candidate
        if path.exists():
            return path.read_text(encoding="utf-8"), "text/markdown"
    return "PaveDB — A lightweight, pluggable vector search microservice.", "text/plain"

long_description, long_type = read_long_description()

setup(
    name="pavedb",                        # external name
    version="0.9.0rc0",
    description="PaveDB — A lightweight, pluggable vector search microservice.",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="Rodrigo Rodrigues da Silva",
    author_email="rodrigo@flowlexi.com",
    license="AGPL-3.0-or-later",
    python_requires=">=3.10,<3.15",
    packages=find_packages(include=["pave", "pave.*"]),  # internal package
    include_package_data=True,
    package_data={"pave.assets": ["*.png", "*.html", "*.yml.example"]},
    install_requires=[
        "fastapi>=0.115.0",
        "uvicorn[standard]>=0.30.6",
        "pydantic>=2.8.2",
        "python-multipart>=0.0.9",
        "pypdf>=5.0.0",
        "pyyaml>=6.0.2",
        "python-dotenv>=1.0.1",
        "faiss-cpu>=1.7.1",
        "torch>=2.10.0",
        "sentence-transformers>=2.7.0",
    ],
    extras_require={
        "cpu": [],
        "openai": [
            "openai>=1.0.0",
        ],
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
