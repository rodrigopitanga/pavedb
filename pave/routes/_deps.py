# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from fastapi import Request

from pave.stores.base import BaseStore


def current_store(request: Request) -> BaseStore:
    return request.app.state.store
