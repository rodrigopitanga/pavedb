# PaveDB 0.9.3

## Português (Brasil)

PaveDB 0.9.3 é uma versão de correção voltada a fluxos com vetores brutos e
ao reforço operacional.

### Destaques

- **Vetores brutos / BYOV.** Adiciona coleções exclusivas para vetores com o
  embedder `identity`, além de ingestão e busca com vetores brutos; valida
  dimensões e metadados; preserva vetores BYOV entre reinicializações; e expõe
  adição e busca por vetores no cliente Python público.
- **Segurança da API e das entradas.** Rejeita nomes de coleção inválidos,
  IDs de documento longos demais, metadados que não sejam objetos, limites de
  busca inválidos, ingestão multipart somente com metadados e requisições de
  ingestão grandes demais com erros estruturados. A CLI também retorna erros
  JSON para opções malformadas.
- **Recuperação, concorrência e autenticação.** Preserva a configuração de
  backend e embedder por coleção durante a recuperação do catálogo, mantém a
  capacidade de busca ocupada até que os workers cujo tempo limite expirou ou que foram
  cancelados terminem, reutiliza a chave administrativa gerada entre workers,
  valida a configuração de autenticação e protege as métricas com autenticação
  administrativa.
- **Alinhamento do SDK e do cliente local.** Compartilha a superfície do cliente
  local com o `pavedb-sdk`, restringe a busca e o replay de consultas por
  tenant e coleção, alinha as respostas de movimentação de coleções e o
  mapeamento de erros, e disponibiliza BYOV pela API pública do cliente.
- **Distribuição e contêineres.** Move a configuração das ferramentas de build
  para `pyproject.toml`, gera os dados de licenças de terceiros, emite um
  snapshot de `pip freeze` para tags de versão, constrói o contêiner de CPU no
  CI e inclui os metadados e a licença do projeto na imagem.

### Notas de atualização e operação

- PaveDB persiste configurações de backend e embedder específicas da coleção no
  `collection.json`. Ao atualizar uma coleção legada com
  modelo personalizado, preserve o `catalog.db` existente na primeira
  inicialização com a versão 0.9.3 para que PaveDB possa recriar o sidecar
  ausente antes de uma recuperação futura do catálogo.
- O exemplo documentado de `docker run` agora passa
  `PAVEDB_AUTH__GLOBAL_KEY` para o contêiner e monta `/root/pavedb`, usando
  assim a chave escolhida pelo operador e preservando os dados da instância.

### Links

- [Site do projeto](https://pavedb.org/)
- [Repositório no GitLab](https://gitlab.com/flowlexi/pavedb)
- [Espelho no GitHub](https://github.com/rodrigopitanga/pavedb)
- [Projeto no PyPI](https://pypi.org/project/pavedb/)
