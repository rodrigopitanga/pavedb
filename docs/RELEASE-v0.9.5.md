# PaveDB 0.9.5

## English

PaveDB 0.9.5 is a planned patch and the first 1.0 preview. It carries security
fixes, the durable SQLite compatibility baseline, and the first shared-plan
quota surface.

### Security

This release fixes several defects in the multi-tenant boundary. Operators
running 0.9.4 or earlier should upgrade.

- **Startup auth policy is enforced on every entry point.** `enforce_policy`
  previously ran only under `pavesrv`, so launching through
  `uvicorn pave.main:app` skipped the production refusal of `auth.mode=none`
  and the loopback coercion.
- **Query replay no longer re-executes a revoked shared-corpus scope.** Replay
  now derives the shared scope from live configuration, so turning
  `common_enabled` off — or re-pointing it — takes effect for stored queries.
- **Tenant names are validated at startup.** They must match
  `^[a-z0-9][a-z0-9-]{0,62}$`, the same rule collection names already use.
  Unvalidated names became directory components while authorization compared
  them byte-exactly, so two names differing only by case or Unicode
  normalization shared one data directory on case-insensitive filesystems.
- **A collection's model no longer inherits an unrelated execution grant.** A
  caller-supplied `embed_model` could inherit the operator's
  `trust_remote_code` and load an arbitrary model repository.
- **Ingest rejects unusable chunk identifiers before it commits.** A control
  character in a `docid` could fail partway through a batch, leaving metadata
  rows without vectors and blocking instance archives.

### Upgrade note

Tenant-name validation is a breaking change. An instance whose configured
tenant names fall outside `^[a-z0-9][a-z0-9-]{0,62}$` refuses to start and
names the offending tenant. Renaming a tenant also requires moving its data
directory from `<data_dir>/t_<old>` to `<data_dir>/t_<new>`; renaming without
moving it orphans that tenant's data.

### Highlights

- **Preview compatibility baseline.** Catalog schema v5 and collection schema
  v6 are the supported floor. From here, released schema changes carry
  append-only, transactional, data-preserving forward migrations; failed
  migrations roll back and newer schemas reject downgrades.
- **Shared-plan quotas.** Per-tenant collection limits and per-collection chunk
  limits are enforced at the store layer and return 429.
- **Embedder options.** Adds `trust_remote_code`, task prefixes as vector-space
  identity, and a TEI-compatible remote runtime.
- **UI gating.** `ui.enabled` controls the OpenAPI UI, which stays off by
  default in production.

### Links

- [Project website](https://pavedb.org/)
- [GitLab repository](https://gitlab.com/flowlexi/pavedb)
- [GitHub mirror](https://github.com/rodrigopitanga/pavedb)
- [PyPI project](https://pypi.org/project/pavedb/)
