# PaveDB 0.9.5

## Português (Brasil)

O PaveDB 0.9.5 é um patch planejado e a primeira prévia da 1.0. Traz correções
de segurança, a base durável de compatibilidade do SQLite e a primeira
superfície de cotas para planos compartilhados.

### Segurança

Esta versão corrige defeitos na fronteira multi-tenant. Quem opera a 0.9.4 ou
anterior deve atualizar.

- **A política de autenticação passa a valer em todos os pontos de entrada.**
  O `enforce_policy` só rodava sob `pavesrv`, então subir por
  `uvicorn pave.main:app` ignorava a recusa de `auth.mode=none` em produção e a
  fixação do bind em loopback.
- **O replay de consultas não reexecuta mais um escopo compartilhado revogado.**
  O replay passa a derivar o escopo da configuração vigente, de modo que
  desligar `common_enabled` — ou apontá-lo para outro lugar — vale também para
  consultas já registradas.
- **Nomes de tenant são validados na inicialização.** Precisam casar com
  `^[a-z0-9][a-z0-9-]{0,62}$`, a mesma regra já usada para coleções. Sem
  validação, o nome virava componente de diretório enquanto a autorização o
  comparava byte a byte, então dois nomes que diferiam apenas por caixa ou
  normalização Unicode compartilhavam um mesmo diretório de dados em sistemas
  de arquivos sem distinção de maiúsculas.
- **O modelo de uma coleção não herda mais permissão de execução alheia.** Um
  `embed_model` informado pelo chamador podia herdar o `trust_remote_code` do
  operador e carregar um repositório de modelo arbitrário.
- **A ingestão rejeita identificadores de chunk inválidos antes de gravar.** Um
  caractere de controle no `docid` podia falhar no meio de um lote, deixando
  linhas de metadados sem vetores e travando os arquivos da instância.
- **Tokens de tenant precisam ser strings.** Um valor YAML sem aspas virava um
  token literal adivinhável: `acme: yes` autenticava como `True`, e valores só
  com dígitos ou em formato de data se comportavam do mesmo jeito.
- **Tokens duplicados são recusados na inicialização.** Um token configurado
  para dois tenants prendia quem o possuísse ao primeiro da lista, então o
  segundo tenant lia e escrevia os dados do primeiro.
- **A ingestão em lote aplica a cota de chunks por coleção.** O limite valia
  apenas para a ingestão de documento único, então o endpoint de lote passava
  por fora.
- **O `k` da busca tem teto.** Antes, uma única requisição podia pedir uma
  coleção inteira.
- **Rotas não encontradas não criam mais séries de métricas sem limite.**
  Qualquer chamador podia inflar o catálogo compartilhado variando o caminho.
- **A busca não registra mais o texto do chunk encontrado.** O conteúdo dos
  documentos era copiado para os logs do operador em INFO por padrão.
- **As sondas de readiness não competem mais entre si.** Sondas concorrentes
  compartilhavam um mesmo arquivo de teste de escrita e marcavam como indisponível
  uma instância saudável.

### Nota de atualização

Três verificações de inicialização são mudanças incompatíveis. Todas falham de
forma explícita e apontam a entrada irregular, em vez de degradar em silêncio.

- **Nomes de tenant** precisam casar com `^[a-z0-9][a-z0-9-]{0,62}$` — a mesma
  regra já usada para coleções. Renomear um tenant exige também mover o
  diretório de dados de `<data_dir>/t_<antigo>` para `<data_dir>/t_<novo>`;
  renomear sem mover deixa os dados daquele tenant órfãos.
- **Tokens de tenant** precisam ser strings não vazias. Coloque aspas em
  qualquer valor que o YAML leria como booleano, número ou data.
- **Tokens de tenant precisam ser únicos.** Dois tenants com o mesmo token é
  recusado.

Dois limites de execução também apertam: a busca recusa `k` acima do novo teto
com 422, e a ingestão em lote que ultrapassaria `max_chunks_per_collection`
passa a retornar 429 em vez de ser aceita. O rótulo `path` das métricas para
requisições sem rota vira um único valor `<unmatched>`, não mais o caminho
recebido.

### Destaques

- **Base de compatibilidade da prévia.** O schema de catálogo v5 e o de coleção
  v6 são o piso suportado. Daqui em diante, mudanças de schema publicadas levam
  migrações incrementais, transacionais e preservadoras de dados; migrações que
  falham revertem e schemas novos recusam downgrade.
- **Cotas para planos compartilhados.** Limites de coleções por tenant e de
  chunks por coleção são aplicados na camada de store e retornam 429.
- **Opções de embedder.** Inclui `trust_remote_code`, prefixos de tarefa como
  identidade do espaço vetorial e runtime remoto compatível com TEI.
- **Controle da UI.** O `ui.enabled` governa a UI do OpenAPI, que segue
  desligada por padrão em produção.

### Links

- [Site do projeto](https://pavedb.org/)
- [Repositório no GitLab](https://gitlab.com/flowlexi/pavedb)
- [Espelho no GitHub](https://github.com/rodrigopitanga/pavedb)
- [Projeto no PyPI](https://pypi.org/project/pavedb/)
