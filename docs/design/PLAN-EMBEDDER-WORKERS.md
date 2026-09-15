<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Embedder Worker Pools

Plan ID: P1-61

## Objective

Generalize bounded embedder worker pools across all embedder implementations.
The SBERT subprocess pool is the first concrete slice; the target behavior is
that every embedder can advertise safe concurrency without leaking provider
details into `LocalStore`.

## Goals

- Keep `Embedder.encode(texts)` as the stable protocol boundary.
- Add a shared worker-pool wrapper that can run multiple embedder instances
  behind one `Embedder`.
- Preserve vector-space identity: worker counts and routing are operational
  config, not catalog state.
- Make bounded concurrency the default for embedders where parallel requests
  are safe and useful.
- Keep provider rate limits explicit for remote embedders.

## Configuration Shape

The instance mapping owns warmup policy; the per-instance `config` map remains
the home for provider runtime knobs:

```yaml
embedder:
  instances:
    native:
      type: sbert
      model: sentence-transformers/paraphrase-MiniLM-L3-v2
      warmup: true
      config:
        workers: 4
```

Future provider-specific knobs should stay operational:

- `workers`: maximum concurrent encode calls for one configured instance.
- `warmup`: optional per-instance override for `embedder.warmup_default`.
  When omitted, the global/default warmup policy applies.
- `queue_size`: optional bound for local backpressure.
- `rate_limit`: optional remote-provider request budget.

None of these keys should enter `SPACE_KEYS`.

## Implementation Notes

1. Promote the SBERT process-pool selection logic into a reusable wrapper.
2. Let each embedder module declare `CLONE_SAFE` so the factory knows whether
   it may construct multiple instances.
3. For local SBERT, keep process workers to isolate PyTorch/OpenMP runtime
   conflicts.
4. For OpenAI-compatible and Ollama embedders, use thread workers with shared
   immutable config and per-call timeout/rate-limit handling.
5. Keep worker selection round-robin at first; routing policy expansion belongs
   with P1-60.

## Not In Scope

- Changing the `Embedder` protocol.
- Adding provider-specific APIs to `LocalStore`.
- Making worker count part of collection vector-space identity.
- Adaptive autoscaling or distributed embedding workers.
