# PaveDB 0.9.6

<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

## Português (Brasil)

O PaveDB 0.9.6 continua o endurecimento de segurança iniciado na 0.9.5 e
acrescenta guias escritos à mão para quem opera. É uma versão de prévia da 1.0.

### Segurança

Esta versão fecha defeitos no tratamento de requisições, isolamento entre
tenants, credenciais de provedor e segurança de arquivos. Quem opera a 0.9.5
deve atualizar.

- **Corpos interpretados em memória e uploads de documentos são limitados antes
  da autenticação.** Antes o corpo era lido e interpretado por inteiro antes de
  qualquer credencial ser conferida, então um chamador não autenticado podia
  fazer o processo bufferizar uma carga arbitrária. JSON e documentos grandes
  demais passam a responder `413 request_too_large` antes do roteamento. A
  restauração da instância confere a credencial de administrador antes de
  consumir o upload e não herda o teto de um documento.
- **A ingestão em lote é limitada no total.** O limite valia por documento,
  então um lote com vários documentos logo abaixo do teto passava enquanto o
  total era várias vezes o limite.
- **Tipos de embedder ficam restritos aos módulos que o PaveDB entrega.** Uma
  coleção podia nomear um módulo arbitrário e fazê-lo ser importado dentro do
  processo da API.
- **Modelos de provedor não configurados não herdam credenciais do operador.**
  Um modelo OpenAI que não corresponda a uma instância configurada é rejeitado
  na criação da coleção, em vez de receber uma chave explícita ou herdada do
  ambiente.
- **Caminhos de leitura não criam mais coleções.** Buscar ou apagar em um nome
  inexistente trazia aquela coleção à existência, fora da cota de coleções por
  tenant.
- **Slots de concorrência são liberados exatamente uma vez.** Uma busca que
  estourava o tempo ou uma ingestão com cliente desconectado devolvia o slot
  enquanto o trabalho seguia em uma thread do pool, então os contadores
  divergiam da realidade e um tenant podia passar do próprio limite.
- **A busca de escopo compartilhado entra no gate por tenant.** Essas rotas
  ignoravam o limite de concorrência por completo.
- **Chamadas a embedder remoto têm orçamento total limitado.** Um timeout por
  requisição não é orçamento quando o cliente refaz a tentativa em timeout; um
  provedor lento podia prender um worker por minutos.
- **Rótulos de métrica não crescem sem limite.** Um chamador não autenticado
  podia criar séries permanentes no catálogo compartilhado variando o caminho
  da requisição ou o verbo HTTP.
- **Durabilidade do índice.** Uma reconstrução não descarta mais em silêncio
  vetores crus (BYOV), e o `flush()` recusa sobrescrever um índice populado com
  um vazio. Um par FAISS/mapa de IDs inválido ou inconsistente agora falha de
  modo seguro, e uma reconstrução com qualquer trecho irrecuperável é recusada
  antes de publicar um índice parcial.
- **O dump preserva o estado vivo do SQLite.** Um dump não remove mais os
  arquivos WAL e de memória compartilhada do catálogo mantidos por outro
  worker. O catálogo é consolidado para o arquivo sem apagar arquivos de outro
  processo.
- **O servidor empacotado limita a produção a um worker.** Múltiplos workers
  mantêm caches FAISS independentes sobre o mesmo diretório de dados e podem
  perder vetores ou servir versões desatualizadas. `server.workers>1` agora é
  recusado fora do modo dev; o modo dev o permite apenas com um aviso explícito
  de risco de corrupção de dados. O alvo empacotado `pave.main:app` agora recusa
  o carregamento ASGI direto; use `pavesrv`. Múltiplas réplicas compartilhando
  um diretório de dados continuam sem suporte.

### Justiça em instâncias compartilhadas

Dois ajustes novos limitam quanto um tenant toma de um pool que é de todos. Os
dois vêm **desligados por padrão**, porque a instância típica serve um tenant só
e ali uma cota é perda pura de vazão:

- `ingest.max_concurrent_per_tenant`
- `search.max_concurrent_per_tenant`

Ligue-os quando tenants que você não controla dividem a instância. O raciocínio,
e por que o `tenants.default_max_concurrent` existente não cobre isso, está no
guia novo.

### Documentação

Guias novos, escritos à mão, em `docs/guides/`: o que é o PaveDB, qual
componente usar, as operações básicas e o que quem opera precisa acertar,
incluindo uma seção sobre rodar instância compartilhada. Eles orientam; as
referências geradas respondem por completo; o livro aprofunda.

### Nota de atualização

Sem mudança no esquema das respostas de sucesso e sem migração de dados.
Requisições grandes demais agora podem responder `413`; há outros
comportamentos a conhecer:

- Buscar em uma coleção inexistente continua respondendo `200` sem resultados —
  mas não cria mais aquela coleção como efeito colateral. Se você dependia da
  busca para trazer uma coleção à existência, crie-a explicitamente.
- Com `common_enabled` ligado, o corpus compartilhado configurado passa a ser
  criado na inicialização, em vez de pela primeira busca compartilhada.
- Um modelo OpenAI precisa corresponder a uma instância de embedder configurada.
  Configure cada modelo que tenants podem selecionar; nomes não configurados
  são rejeitados antes da criação de qualquer cliente do provedor.
- A inicialização empacotada em produção agora recusa `server.workers>1`.
  Defina-o como `1`; o modo dev continua disponível para experimentos
  multi-worker com dados descartáveis.
- O carregamento direto de `pave.main:app`, como em
  `uvicorn pave.main:app`, é recusado. Use `pavesrv`; em testes, chame
  `build_app(get_cfg())` diretamente.
- Um par FAISS/mapa de IDs inválido, ou qualquer trecho impossível de
  reconstruir, agora deixa a coleção indisponível em vez de arriscar uma
  reconstrução parcial. Repare ou restaure o índice antes de servir a coleção.

As verificações de inicialização introduzidas na 0.9.5 seguem iguais: nomes de
tenant precisam casar com `^[a-z0-9][a-z0-9-]{0,62}$`, tokens precisam ser
strings não vazias entre aspas, e dois tenants não podem dividir um token.

### Links

- [Site do projeto](https://pavedb.org/)
- [Repositório no GitLab](https://gitlab.com/flowlexi/pavedb)
- [Espelho no GitHub](https://github.com/rodrigopitanga/pavedb)
- [Projeto no PyPI](https://pypi.org/project/pavedb/)
