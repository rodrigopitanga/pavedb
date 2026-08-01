# PaveDB 0.9.1

## English

PaveDB 0.9.1 expands configurable collection embeddings and tightens service
behavior around concurrent work.

### Highlights

- Moves embedder specifications onto their modules, adds convention-based
  dispatch, and adds a local Ollama-compatible embedder.
- Adds collection model specifications plus collection update and move APIs,
  returning a clear unavailable response when an embedder cannot be used.
- Treats concurrent deletion during search as not found, keeps health probes
  out of the per-collection lock walk, and moves query embedding out of the
  collection read lock.

### Links

- [Project website](https://pavedb.org/)
- [GitLab repository](https://gitlab.com/flowlexi/pavedb)
- [GitHub mirror](https://github.com/rodrigopitanga/pavedb)
- [PyPI project](https://pypi.org/project/pavedb/)
