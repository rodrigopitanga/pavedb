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
- **Tenant bearer tokens must be strings.** An unquoted YAML value became a
  guessable literal token: `acme: yes` authenticated as `True`, and all-digit
  or date-shaped values behaved the same way.
- **Duplicate bearer tokens are refused at startup.** One token configured for
  two tenants bound its holder to whichever appeared first, so the second
  tenant read and wrote the first tenant's data.
- **Batch ingest enforces the per-collection chunk quota.** The cap was applied
  to single-document ingest only, so the batch endpoint stepped around it.
- **Search `k` is bounded.** A single request could previously ask for an
  entire collection.
- **Unmatched routes no longer mint unbounded metrics series.** Any caller
  could grow the shared catalog permanently by varying the request path.
- **Search no longer logs matched chunk text.** Document content was copied
  into the operator's logs at INFO by default.
- **Readiness probes no longer race each other.** Concurrent probes shared one
  write-test filename and reported a healthy instance unready.

### Upgrade note

Three startup checks are breaking changes. Each fails loudly and names the
offending entry rather than degrading silently.

- **Tenant names** must match `^[a-z0-9][a-z0-9-]{0,62}$` — the same rule
  collection names already use. Renaming a tenant also requires moving its data
  directory from `<data_dir>/t_<old>` to `<data_dir>/t_<new>`; renaming without
  moving it orphans that tenant's data.
- **Tenant tokens** must be non-empty strings. Quote any value that YAML would
  otherwise read as a boolean, number or date.
- **Tenant tokens must be unique.** Two tenants sharing one token is refused.

Two runtime limits also tighten: search rejects `k` above the new bound with
422, and batch ingest that would exceed `max_chunks_per_collection` now returns
429 instead of being admitted. The metrics `path` label for unmatched requests
is now a single `<unmatched>` value rather than the raw request path.

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
