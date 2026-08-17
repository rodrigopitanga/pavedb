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

### Nota de atualização

A validação de nomes de tenant é uma mudança incompatível. Uma instância cujos
nomes configurados fiquem fora de `^[a-z0-9][a-z0-9-]{0,62}$` não inicia e
informa qual tenant está irregular. Renomear um tenant exige também mover o
diretório de dados de `<data_dir>/t_<antigo>` para `<data_dir>/t_<novo>`;
renomear sem mover deixa os dados daquele tenant órfãos.

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
