# (C) 2025, 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Store implementations.

The store is the orchestrator that composes the vector backend
(`pave.backends`), embedder (`pave.embedders`), and per-collection
SQLite metadata (`pave.metadb`) into a single per-tenant/collection
interface. Service-layer code (`pave.service`) talks only to the
store; transport layers (HTTP, CLI, library) never reach below it.

See `pave.stores.base.BaseStore` for the abstract contract and
`pave.stores.local.LocalStore` for the on-disk implementation.
"""

from .local import LocalStore

__all__ = ["LocalStore"]
