# PaveDB 0.9.1

## Português (Brasil)

PaveDB 0.9.1 amplia a configuração de embeddings por coleção e reforça o
comportamento do serviço sob trabalho concorrente.

### Destaques

- Move as especificações de embedder para seus módulos, adiciona despacho por
  convenção e um embedder local compatível com Ollama.
- Adiciona especificações de modelo por coleção e APIs para atualizar e mover
  coleções, retornando uma resposta clara de indisponibilidade quando um
  embedder não pode ser usado.
- Trata como não encontrada uma coleção excluída durante a busca, evita percorrer
  os locks das coleções nas verificações de integridade e move a geração do
  embedding da consulta para fora do lock de leitura da coleção.

### Links

- [Site do projeto](https://pavedb.org/)
- [Repositório no GitLab](https://gitlab.com/flowlexi/pavedb)
- [Espelho no GitHub](https://github.com/rodrigopitanga/pavedb)
- [Projeto no PyPI](https://pypi.org/project/pavedb/)
