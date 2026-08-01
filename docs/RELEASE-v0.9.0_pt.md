# PaveDB 0.9.0 — Inspectability

## Português (Brasil)

PaveDB 0.9.0, **Inspectability**, é a primeira versão após a mudança de marca
do PatchVec.

### API

- Inicia um congelamento tentativo da API.
- Padroniza o envelope de resposta com `ok`, `code`, `error`, `request_id` e
  `latency_ms`.
- Propaga `X-Request-ID` pelo cabeçalho da resposta, pelo `ops_event` e pelo
  `query_log`.
- Adiciona `GET /collections/{t}/{c}/documents[/{docid}]`; a listagem inclui
  contagens de chunks, e o detalhe é enriquecido com informações da coleção.

### Inspectabilidade

- Adiciona um inspetor de chunks com conteúdo, metadados e o offset no documento
  original.
- Adiciona um log persistente de consultas por coleção com texto da consulta,
  filtros, IDs retornados e tempos de execução.
- Expõe `query_id` em `SearchResponse`, para que o replay não varra o log.
- O replay executa novamente a consulta no índice atual.
- Adiciona `GET/POST /v1/admin/queries/{id}[/replay]`; esses endpoints de
  resolução continuam localizando a coleção após ela ser renomeada.
- Informa os campos exatos da resposta
  `timing.{embed,search,filter,hydrate}_ms`.

### Armazenamento e concorrência

- Armazena a configuração de backend e embedder por coleção.
- Usa um lock de leitura/escrita reentrante para permitir buscas concorrentes.
- Move as escritas do catálogo para dentro do lock de escrita da coleção,
  evitando inconsistência entre disco e catálogo.
- Corrige condições de corrida encontradas pelo `bench-stress`.

### Benchmarks

As novas suítes cobrem todos os endpoints, enquanto a suíte base continua
comparável. Em uma máquina Apple da série M4 com 10 núcleos:

- Busca: 1.200 consultas com concorrência 42, p50 de cerca de 224 ms e cerca de
  168 ops/s.
- Estresse misto: 60 segundos com concorrência 8, cerca de 120 ops/s e zero
  erros.

### Compatibilidade com macOS

- Executa o SBERT em um subprocesso para isolar os runtimes OpenMP do FAISS e
  do PyTorch.
- Executa a verificação de compatibilidade com GNU Make 4+ e Bash 4+ durante a
  análise do Makefile.

### Próximo: PaveDB 1.0

A próxima versão traz o modo biblioteca, com ou sem servidor, documentação de
referência para OpenAPI, CLI e variáveis de configuração, além do congelamento
da API.

### Links

- [Repositório no GitLab](https://gitlab.com/flowlexi/pavedb)
- [Espelho no GitHub](https://github.com/rodrigopitanga/pavedb)
- [Projeto no PyPI](https://pypi.org/project/pavedb/)
