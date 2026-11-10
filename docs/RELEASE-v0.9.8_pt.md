# PaveDB 0.9.8

<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

## Português (Brasil)

**Rascunho não publicado.** Esta versão ainda está em preparação; haverá mais
alterações antes da publicação.

### Cotas com zero explícito

Cotas de contagem, taxa e concorrência usam **0 = sem permissão de uso,
-1 = ilimitado**. Isso inclui RPM/RPH por tenant, limites de coleções e chunks,
orçamentos de arquivos/reindexação e concorrência de operações. Limites de
tamanho seguem a mesma convenção e mantêm MB fracionários. Valores negativos
inválidos, contagens fracionárias e booleanos são rejeitados na configuração.

Uma instância compartilhada pode impedir o início de reindexações por tenant
com `tenants.default_max_reindex_per_month: 0`. Um valor explícito por tenant
substitui o padrão da instância; um override ausente ou nulo herda o padrão.
Os padrões anteriormente ilimitados agora usam `-1`.

Orçamentos zero recusam novas requisições e operações sem consumir outras
janelas. Uma cota de chunks zero recusa ingestão antes do embedding, e escritas
recusadas preservam dados existentes. Uma cota de requisições zero não tem prazo
para nova tentativa. O mês de reindexação continua sendo uma janela móvel de
30 dias, não um mês do calendário. Caminhos de manutenção do operador/admin
mantêm seus privilégios existentes.

### Valores de metadados e filtros

Valores de metadados em texto agora são armazenados e comparados exatamente como
enviados. Até a 0.9.7 eles passavam por uma reescrita — `'` duplicado, `;`, `"`,
`` ` `` e `\` trocados por espaço, texto cortado em `--`, `/*` ou `*/` — que
unificava valores diferentes apenas nesses caracteres e fazia o pushdown de
`docid` discordar do filtro canônico. Uma lista de filtro que mistura texto e
números também não descarta mais os valores não textuais.

Documentos ingeridos antes da 0.9.8 mantêm o texto reescrito, então um filtro
pelo valor original não os encontra mais; faça a reingestão deles. Veja a
[nota de atualização](guides/running-it.md#metadata-values-stored-before-098).

### Cliente local

O provedor local implementa os métodos de coleção da 0.9.7 exigidos pelo SDK,
incluindo opções de criação e operações de arquivos/reindexação por tenant.
Isso restaura a paridade na interface suportada pelo cliente local.

### Migração da configuração antes de atualizar

Antes de iniciar a 0.9.8, guarde cópias seguras de `config.yml` e
`tenants.yml`. Confira as chaves de cotas no
[guia de migração](guides/running-it.md#quota-values-and-upgrading-from-097)
e altere para `-1` apenas valores `0` explícitos e antigos que significavam
ilimitado. Confira também overrides de ambiente e manifestos de implantação.
Se a configuração de tenants é gerada, atualize a origem, inclusive no Console,
e não apenas o `tenants.yml` emitido. Preserve zeros novos usados para negar
acesso e zeros sem relação com cotas. Confira os valores resultantes antes de
iniciar a 0.9.8.

Timeouts, chunking e quantidade de workers mantêm seus significados. Não há
reescrita do banco, e o uso acumulado das janelas é preservado. Não envie uma
política de zero como bloqueio a um servidor antigo, que trata zero como ilimitado.
