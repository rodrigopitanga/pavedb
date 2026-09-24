<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# PaveDB guides

Short, hand-written orientation. Four pages, read in order or jumped into.

| Guide | Answers |
|---|---|
| [Overview](overview.md) | What PaveDB is, and the four nouns everything is built from |
| [Components](components.md) | Server, CLI, clients — which one you should be using |
| [Basic operations](basic-operations.md) | Create, ingest, search, inspect, replay |
| [Running it](running-it.md) | Production checklist, auth, backups, the query log, health, containers |

## How the documentation is split

Four sources, and they do not overlap by accident:

| Source | What it is | When it changes |
|---|---|---|
| **These guides** | Hand-written orientation. Deliberately short. | When the mental model changes |
| **[`docs/reference/`](../reference/)** | Generated from the code: every config key, every command, every protocol. | Every release, automatically |
| **[`openapi.json`](../../openapi.json)** | The wire contract: every endpoint, request and response. | Every release, automatically |
| **The PaveDB book** | Long-form: why it works this way, and the hard parts. | Independently |

The references are generated, so they are exhaustive and never drift. These
guides are not exhaustive on purpose — if a page here starts listing flags, it
is doing the reference's job badly. The book goes deep; these pages get you
oriented in a few minutes.

If you want to change PaveDB itself rather than use it, you want
[`CONTRIBUTING.md`](../../CONTRIBUTING.md), not this directory.
