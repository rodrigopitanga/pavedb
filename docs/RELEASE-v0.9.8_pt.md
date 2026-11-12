# PaveDB 0.9.8

<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

## Português (Brasil)

O PaveDB 0.9.8 torna os limites por tenant explícitos e aplicáveis, preserva os
valores dos metadados como foram enviados e completa no cliente Python local as
operações de coleção introduzidas na 0.9.7. É uma versão de manutenção da
prévia da 1.0. **Quem atualizar da 0.9.7 precisa revisar a configuração de
cotas antes.**

A versão 0.9.8.1 republica as notas de lançamento da 0.9.8, corrigidas e ampliadas,
e inclui correções na infraestrutura de CI/CD e nos processos de automação, sem 
alterações funcionais.

### O que mudou desde a 0.9.7

- **Uma cota agora pode negar acesso.** Todos os limites de contagem, taxa e
  concorrência seguem a mesma regra: `0` recusa novas operações, um inteiro
  positivo estabelece um limite, e `-1` significa ilimitado. Isso inclui
  requisições por minuto e hora, requisições simultâneas, quantidade de coleções
  e chunks, arquivos de coleção por dia, inícios de reindexação e os pools de
  busca, ingestão, arquivamento e reindexação. Os limites de tamanho para
  requisições, arquivos e lotes seguem a mesma convenção `0` / `-1` e continuam
  aceitando frações de megabyte.
- **Políticas por tenant com exceções explícitas.** O padrão da instância vale
  para quem não define um override; um tenant pode usar `0` para negar uma
  operação ou `-1` para retirar seu limite. Por exemplo,
  `tenants.default_max_reindex_per_month: 0` impede que tenants iniciem
  reindexações, mas é possível liberá-las para tenants específicos. O orçamento
  de arquivos usa uma janela móvel de 24 horas; o de reindexação, uma janela
  móvel de 30 dias. Os contadores continuam persistidos entre reinícios.
- **Uma recusa é um limite, não uma espera.** Uma cota de requisições igual a
  zero retorna `429 tenant_rate_limited` sem `Retry-After`, porque o passar do
  tempo não libera esse acesso. Requisições recusadas não consomem orçamento de
  outra janela. Uma cota de chunks igual a zero barra a ingestão antes do
  embedding; uma reingestão recusada preserva o documento existente. Definir
  uma cota zero não apaga coleções ou chunks já armazenados.
- **Metadados preservados integralmente.** Valores textuais são armazenados e
  devolvidos sem alterações em pontuação, aspas, barras invertidas, espaços ou
  trechos que se parecem com SQL. Por exemplo, o valor
  `O'Brien; notes -- revised` permanece idêntico após a ingestão. As *chaves*
  dos metadados continuam seguindo a normalização existente.
- **Filtros de metadados consistentes.** A comparação exata e o pushdown dos
  filtros ao SQLite agora concordam para esses valores, inclusive IDs de
  documento e metadados de chunk que sobrescrevem os do documento. Alternativas
  que misturam texto e números não perdem resultados válidos quando só alguns
  valores podem ser filtrados diretamente no banco. Isso corrige buscas que
  descartavam resultados aceitos pelo filtro canônico.
- **Paridade do cliente Python local.** O provedor local agora implementa as
  operações do SDK adicionadas na 0.9.7: criação com `search_mode`, `chunking` e
  `priority_key`; exportação e restauração de arquivos por coleção (inclusive
  substituição); e início, consulta de estado e cancelamento de reindexações.
  Os mesmos fluxos do SDK funcionam sobre um diretório local, sem servidor HTTP.

### Configuração e correção

Valores inválidos de cota agora provocam erro de validação em vez de desativar
silenciosamente um limite: booleanos, contagens fracionárias ou em texto e
inteiros abaixo de `-1` são rejeitados. Limites de tamanho rejeitam valores
inválidos ou não finitos; frações válidas de MB continuam aceitas. Um override
nulo herda o padrão da instância, e `ingest.max_batch_size_mb: null` continua
herdando o limite de um arquivo individual. Os privilégios de manutenção do
operador/admin permanecem os mesmos.

### Nota de atualização — ação necessária

**Na 0.9.7, uma cota explicitamente igual a `0` significava ilimitado; na
0.9.8, significa sem permissão.** Antes de atualizar:

1. Guarde cópias de `config.yml` e `tenants.yml`. Altere para `-1` **apenas**
   os zeros antigos usados com o sentido de ilimitado. Revise os campos
   `tenants.default_max_*` e os `max_*` por tenant; as configurações de
   concorrência em `search`, `ingest`, `archive` e `reindex`; e
   `ingest.max_file_size_mb`, `ingest.max_batch_size_mb` e
   `server.max_request_body_mb`.
2. Confira os overrides `PAVEDB_*`, manifestos Compose/Kubernetes e geradores
   de configuração, além dos arquivos YAML. Quando a configuração de tenants é
   gerada (inclusive por um console de administração), altere a origem, não só
   o arquivo gerado. Preserve os zeros intencionais de bloqueio e valores sem
   relação com cotas, como `search.timeout_ms: 0`.
3. Se documentos ingeridos antes da 0.9.8 contêm valores de metadados com
   caracteres anteriormente reescritos (`'`, `;`, `"`, `` ` ``, `\`, NUL, `--`,
   `/*` ou `*/`), **faça a reingestão desses documentos** para recuperar os
   valores originais. A atualização não reconstrói valores já alterados no
   disco. Coleções sem metadados afetados dispensam reingestão.

Não é necessário reescrever o banco de dados nem zerar as janelas de uso para
essas mudanças. Não aplique políticas de bloqueio com `0` a um servidor 0.9.7:
essa versão interpreta esse valor como ilimitado. O procedimento completo está
no [guia operacional](guides/running-it.md#quota-values-and-upgrading-from-097)
e na [nota de migração dos metadados](guides/running-it.md#metadata-values-stored-before-098).

### End-of-life do Python 3.10

O suporte ao Python 3.10 será descontinuado no próximo lançamento minor ou
major (o que vier primeiro).

O PaveDB continuará oferecendo suporte às versões do Python presentes no Debian
stable e oldstable, atualmente Python 3.11 ou superior.

### Links

- [Site do projeto](https://pavedb.org/)
- [Repositório no GitLab](https://gitlab.com/flowlexi/pavedb)
- [Espelho no GitHub](https://github.com/rodrigopitanga/pavedb)
- [Projeto no PyPI](https://pypi.org/project/pavedb/)
