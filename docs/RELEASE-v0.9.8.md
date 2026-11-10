# PaveDB 0.9.8

<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

## English

**Unreleased draft.** This release is still being assembled; more work will be
added before publication.

### Quotas with an explicit zero

Count, rate and concurrency quotas use **0 = no allowance, -1 = unlimited**.
This includes tenant RPM/RPH, collection/chunk limits, archive/reindex budgets
and operation concurrency. Size ceilings follow the same convention and retain
fractional MB support. Invalid negatives, non-integer counts and booleans are
rejected during configuration loading and updates.

A shared instance can disable tenant reindex starts with
`tenants.default_max_reindex_per_month: 0`. An explicit tenant value overrides
the instance default; an absent or null override inherits it. Previously
unlimited defaults now use `-1`, preserving the unconfigured experience.

Zero request and operation budgets deny new work without spending another
window's allowance. A zero chunk budget rejects ingest before encoding, and
refused writes preserve existing data. A zero request budget has no retry time;
positive budgets retain their existing moving windows. Reindex's monthly
window remains 30 days, not a calendar month. Trusted operator/admin maintenance
paths retain their existing privileges.

### Metadata values and filters

String metadata values are now stored and compared exactly as sent. Through
0.9.7 they were rewritten first — `'` doubled, `;`, `"`, `` ` `` and `\` turned
into spaces, text cut at `--`, `/*` or `*/` — which collapsed values that
differed only there and made `docid` pushdown disagree with the canonical
filter. A filter list that mixes strings with numbers no longer drops the
non-string values either.

Documents ingested before 0.9.8 keep the rewritten text, so a filter for the
original value no longer matches them; re-ingest those documents. See the
[upgrade note](guides/running-it.md#metadata-values-stored-before-098).

### Local client

The local provider implements the 0.9.7 collection hooks required by the SDK,
including collection creation options and tenant collection archive/reindex
operations. This restores parity for the supported local-client surface.

### Configuration migration before upgrading

Before starting 0.9.8, keep secure copies of `config.yml` and `tenants.yml`.
Review the quota keys in the
[migration guide](guides/running-it.md#quota-values-and-upgrading-from-097),
then change only explicit old `0` values that meant unlimited to `-1`. Check
environment overrides and deployment manifests too. If tenant configuration is
generated, update its source, including Console, rather than only the emitted
`tenants.yml`. Keep intentional new zero-as-deny values and unrelated zeros.
Verify the resulting values before starting 0.9.8.

Timeout, chunking and worker-count semantics are unchanged. No database rewrite
is required and accumulated rate-window usage is preserved. Do not send
zero-as-deny configuration to an older server, which treats zero as unlimited.
