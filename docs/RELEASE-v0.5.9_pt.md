# PatchVec 0.5.9 — Relevance

## Português (Brasil)

PatchVec 0.5.9 melhora a relevância, a filtragem de metadados e a
confiabilidade do armazenamento. O PatchVec agora é o PaveDB.

### Destaques

- Introduz `LocalStore` como orquestrador e ativa o caminho vetorial FAISS,
  removendo a dependência de execução do `txtai`.
- Melhora a filtragem de metadados com armazenamento por chunk, pré-filtragem
  de negação exata, validação, sanitização e uma correção de OR misto.
- Reforça o carregamento de índices, a E/S de chunks e arquivos compactados e
  o ciclo de vida do cache contra condições de corrida e TOCTOU.
- Adiciona limites de SLO para benchmarks e um job dedicado de CI de benchmark.

### Links

- [PaveDB](https://pavedb.org/)
- [Repositório no GitLab](https://gitlab.com/flowlexi/pavedb)
- [Espelho no GitHub](https://github.com/rodrigopitanga/pavedb)
- [Pacote histórico PatchVec](https://pypi.org/project/patchvec/)
