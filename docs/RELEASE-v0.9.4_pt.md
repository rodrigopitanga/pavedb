# PaveDB 0.9.4

## Português (Brasil)

PaveDB 0.9.4 reforça a operação com múltiplos workers, a recuperação de
arquivos e a correção da busca.

### Destaques

- **Métricas seguras entre workers.** Armazena contadores e amostras limitadas
  de latência no banco de catálogo para expor uma visão consistente e
  compartilhar o estado de reset.
- **Arquivos mais seguros.** Valida catálogo, metadados das coleções, índices
  FAISS, dimensões e métricas antes da restauração, com rollback de trocas
  incompletas.
- **Cobertura de concorrência.** Adiciona o harness de caos agendado, verifica a
  limitação controlada de ingestão, valida catálogo e disco e automatiza a
  triagem de falhas.
- **Correção da busca.** Preserva filtros de metadados com listas e a
  precedência entre documento e chunk, adiciona o harness de paridade SQL e
  encaminha consultas com vetores brutos pela busca compartilhada.
- **Limpeza operacional.** Limita caches de coleções ativas, melhora a
  reconciliação de issues, seleciona automaticamente uma versão compatível do
  Python e documenta os pontos de entrada do SDK Python e do cliente Elixir.

### Links

- [Site do projeto](https://pavedb.org/)
- [Repositório no GitLab](https://gitlab.com/flowlexi/pavedb)
- [Espelho no GitHub](https://github.com/rodrigopitanga/pavedb)
- [Projeto no PyPI](https://pypi.org/project/pavedb/)
