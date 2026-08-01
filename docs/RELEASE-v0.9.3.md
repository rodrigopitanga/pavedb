# PaveDB 0.9.3

## English

PaveDB 0.9.3 is a patch release focused on raw-vector workflows and
operational hardening.

### Highlights

- **Raw vectors / BYOV.** Adds identity, vector-only collections and raw-vector
  ingest and search; validates vector dimensions and metadata; preserves BYOV
  vectors across restarts; and exposes vector add and search through the public
  Python client.
- **API and input safety.** Rejects invalid collection names, overlong document
  IDs, non-object metadata, invalid search limits, metadata-only multipart
  ingest, and oversized ingest requests with structured errors. The CLI also
  returns JSON errors for malformed options.
- **Recovery, concurrency, and authentication.** Preserves per-collection
  backend and embedder configuration during catalog recovery, keeps search
  capacity occupied until timed-out or cancelled workers finish, reuses the
  generated admin key across workers, validates authentication configuration,
  and protects metrics with admin authentication.
- **SDK and local-client alignment.** Shares the local client surface with
  `pavedb-sdk`, scopes query lookup and replay by tenant and collection, aligns
  collection-move responses and error mapping, and makes BYOV available
  through the public client API.
- **Distribution and containers.** Moves build-tool configuration to
  `pyproject.toml`, generates third-party license data, records a `pip freeze`
  snapshot for release tags, builds the CPU container in CI, and
  includes project metadata and the license in the image.

### Upgrade and operator notes

- PaveDB persists per-collection backend and embedder settings in
  `collection.json`. When upgrading a legacy custom-model collection, keep its
  existing `catalog.db` intact for the first 0.9.3 startup so PaveDB can
  backfill a missing sidecar before any later catalog recovery.
- The documented `docker run` example now passes `PAVEDB_AUTH__GLOBAL_KEY`
  into the container and mounts `/root/pavedb`, so it uses the
  operator-selected key and preserves instance data.

### Links

- [Project website](https://pavedb.org/)
- [GitLab repository](https://gitlab.com/flowlexi/pavedb)
- [GitHub mirror](https://github.com/rodrigopitanga/pavedb)
- [PyPI project](https://pypi.org/project/pavedb/)
