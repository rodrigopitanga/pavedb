<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Extra Benchmarks (P1-63)

P1-63 is delivered outside PaveDB core by the
`pavedb-extra-benchmarks` distribution. It depends on PaveDB; PaveDB never
depends on it. Installing the base package installs the comparison dependencies:

```bash
pip install pavedb-extra-benchmarks
```

`bench-vector` provisions pinned VectorDBBench v1.0.20 in a disposable isolated
runtime because its Pydantic 1 dependency conflicts with PaveDB's Pydantic 2
contract. This remains an implementation detail: the package has one base
install and no named extras.

The package exposes one umbrella and two benchmark families:

| Target | Contract |
|--------|----------|
| `bench-extra` | Run all extra benchmark families. |
| `bench-comp` | Run `bench-comp-latency` and `bench-comp-stress`. |
| `bench-comp-latency` | Run the Chroma comparison latency suite. |
| `bench-comp-stress` | Run the Chroma comparison stress suite. |
| `bench-comp-vector` | Run the cheap native raw-vector comparison. |
| `bench-vector` | Run VectorDBBench through the PaveDB adapter. |

There are no public `bench`, `benchmark`, `bench-latency`, or `bench-stress`
aliases, and no `official`, `[official]`, or `[bench]` install extras.

`bench-vector` remains callable from the extra-benchmarks harness. The
repository also keeps an upstream-ready handoff under
`pavedb_vectordbbench/`: a root README lists the exact source/config files to
copy into VectorDBBench, their destinations, the colocated registry/CLI patch,
and the reproduction command. The packaged wheel and sdist include that README
and patch. Official `result_*.json` files survive the disposable checkout under
the existing ignored `benchmarks/results/` hierarchy.

The adapter uses public HTTP BYOV only and preserves bearer-token support,
integer-ID mapping, numeric `>=` filters, exact label filters, cosine-only
metric validation, the pinned VectorDBBench v1.0.20 patch, and a
current-upstream compatibility check. PaveDB core gains no VectorDBBench
runtime dependency or benchmark-only endpoint.

## Version boundary

P1-63 completes the v1.0 adapter contract with standard cosine cases, upstream
numeric and label filters, and durable result artifacts. PaveDB 1.0 does not
require non-cosine metrics or an ANN index.

P2-51 adds exact flat inner-product and L2 metrics in v1.1. The adapter then
forwards the upstream case metric through the shared collection
`backend_config`. P2-52 adds HNSW benchmark coverage in v1.2, and P2-53 adds
IVF plus the public optimize operation in v1.8. See
[`PLAN-ANN.md`](PLAN-ANN.md) for the core index and lifecycle contract.
