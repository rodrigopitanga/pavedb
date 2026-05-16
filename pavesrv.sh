#!/usr/bin/env bash
# (C) 2025 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

# simple server wrapper: prefer project venv; fallback to system python
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV_PY="$ROOT/.venv-pave/bin/python"

if [ -x "$VENV_PY" ]; then
  exec "$VENV_PY" -m pave.main "$@"
fi

echo "WARN: .venv not found, using system python" >&2
exec python3 -m pave.main "$@"
