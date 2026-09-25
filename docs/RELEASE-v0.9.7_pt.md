# PaveDB 0.9.7

<!-- (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com> -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

## Português (Brasil)

O PaveDB 0.9.7 traz modos de busca, chunking por coleção e coleções que os
tenants podem levar consigo, e fecha dezenove defeitos. É uma versão de prévia
da 1.0; os dados persistidos continuam compatíveis para a frente, como desde a
0.9.5.

### O que há de novo desde a 0.9.6

- **Modos de busca.** Toda busca roda em um de três modos. `vector` ordena por
  similaridade. `boost` dá um empurrão nos resultados vetoriais cujo texto
  contém os tokens da consulta. `hybrid` funde as ordenações vetorial e de
  texto completo. Quem opera decide quais modos a instância serve, cada coleção
  escolhe o seu padrão na criação, e uma busca pode pedir outro modo. A
  resposta e o log de consultas dizem qual modo rodou.
- **Pontuações legíveis.** O `match_reason` de cada resultado diz como a
  pontuação foi feita: `vector: cosine 0.834`, `boost: cosine 0.712 + 0.067
  for exact tokens 2/3`, ou as posições vetorial e léxica de um resultado
  híbrido.
- **Prioridade.** Um número em [-1, 1] no metadado `prio_boost` de um
  resultado multiplica sua pontuação por `1 + valor` em todos os modos, e o
  `match_reason` mostra isso. Uma coleção pode escolher outro campo na criação.
- **Recuperação que enxerga o texto.** O texto dos chunks fica no SQLite com
  índices de texto completo e de trigramas. Os filtros de conteúdo (`exact`,
  `phrase`, `prefix`, `contains`) se combinam com os filtros de metadados, e
  filtros seletivos estreitam a própria busca vetorial em vez de só peneirar os
  resultados.
- **Chunking por coleção.** Cada coleção escolhe na criação janelas `fixed` ou
  `none` (quem chama faz o chunking), dentro dos tamanhos e sobreposições
  mínimos e máximos definidos por quem opera.
- **Coleções que são suas.** Um tenant pode baixar uma coleção como arquivo
  portátil, restaurá-la como coleção nova em qualquer instância da mesma versão
  ou voltar uma coleção existente a ela, e reconstruí-la em outro modelo de
  embedding como tarefa retomável, tudo com a própria chave. Os arquivos levam a
  versão do PaveDB e do esquema com que foram feitos.
- **Controle do embedding por quem opera.** As instâncias de embedder
  configuradas são escolhidas por políticas de roteamento, com uma preferência
  por operação, de modo que a ingestão em massa ou um reindex rodem numa
  instância com GPU enquanto a busca fica no padrão. Todo embedder roda num pool
  de workers com concorrência limitada.
- **Limites de taxa de requisições.** Orçamentos por tenant por minuto e por
  hora, persistidos entre reinícios, ao lado dos limites de concorrência e das
  cotas que já existiam.
- **A CLI nomeia coleções como `tenant/coleção`.** `pavecli search demo/books
  "captain nemo"`, o mesmo caminho que a API HTTP usa.

### Segurança

Esta versão fecha dezenove defeitos, quase todos vindos de uma revisão de
segurança. Quem opera a 0.9.6 deve atualizar.

- **Nenhum detalhe interno nos corpos de erro.** Falhas inesperadas respondem
  com uma mensagem fixa e registram o detalhe no log; falhas de embedder não
  revelam mais URLs internas nem respostas de gateway; erros de validação não
  ecoam mais o valor rejeitado.
- **A busca compartilhada é só por texto e privada.** Não aceita mais vetores
  de quem chama, o que, com pontuações exatas, permitia reconstruir os vetores
  armazenados, e não grava mais o texto da consulta de cada chamador no log de
  consultas do tenant compartilhado.
- **Chaves.** Os tokens bearer são comparados em tempo constante, e uma chave
  nula no `config.yml` agora revoga uma chave que só o arquivo de tenants
  fornece.
- **Contenção.** O contêiner roda com um usuário sem privilégios, e o log de
  operações é criado legível só pelo dono e pelo grupo.
- **Limites de recursos.** Uma coleção só usa um modelo de embedding que quem
  opera configurou, então ninguém faz a instância baixar outro; a restauração de
  arquivos recusa arquivos que encheriam o disco, os embedders são construídos
  fora das travas do store para que um modelo lento não trave todos os tenants,
  travas de coleções ociosas são liberadas, e a dimensão de uma coleção identity
  é limitada.
- **Durabilidade.** O par de arquivos do índice FAISS é gravado em duas fases e
  completado ou descartado na carga seguinte; um dump da instância inteira
  pula uma coleção danificada e a reporta em vez de falhar para todos os
  tenants; coleções novas são gravadas com o seu índice desde o início.

### Documentação

Os guias ganham uma lista de verificação para produção, a diferença entre o
backup da instância e uma coleção portátil, o que o log de consultas guarda, e
o ciclo de inspeção e replay, do resultado até a sua fonte.

### Nota de atualização

Os dados armazenados migram para a frente automaticamente na primeira
abertura. Estas mudanças pedem atenção:

- **Configuração.** `search.exact_token_boost` e o seu peso foram substituídos
  por `search.modes` e `search.boost_weight`; `preprocess.txt_chunk_size` e
  `txt_chunk_overlap` foram substituídos pelas chaves `chunking.*`. As coleções
  existentes mantêm o chunking 1000/200 com que foram construídas; uma
  instância que tinha personalizado as chaves antigas deve recriar essas
  coleções.
- **CLI.** Os comandos que recebiam `demo books` recebem `demo/books`.
- **Busca compartilhada.** `POST /v1/search` rejeita um vetor bruto (`v`).
- **Modelos de embedding.** Criar uma coleção ou iniciar um reindex com um
  modelo que nenhuma instância configurada nomeia é recusado; coleções
  existentes continuam carregando.
- **Match reasons.** O texto do `match_reason` mudou como descrito acima; se for
  interpretá-lo, use o prefixo do modo.
- **Contêineres.** A imagem roda como UID 1000 com o diretório da instância em
  `/data`. Monte os volumes ali, e rode `chown -R 1000:1000` num volume criado
  por uma imagem antiga, que era montado em `/root/pavedb`.

### Links

- [Site do projeto](https://pavedb.org/)
- [Repositório no GitLab](https://gitlab.com/flowlexi/pavedb)
- [Espelho no GitHub](https://github.com/rodrigopitanga/pavedb)
- [Projeto no PyPI](https://pypi.org/project/pavedb/)
