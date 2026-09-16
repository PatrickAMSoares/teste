# Segurança dos arquivos

A prioridade absoluta do PhotoDedupe é **não perder nenhuma foto do usuário**. Este
documento lista cada garantia e onde ela é aplicada no código, para que possa ser auditada.

## 1. A análise é somente leitura

Durante toda a análise os arquivos são abertos apenas para leitura. O aplicativo **não**
altera fotos, nomes, datas, EXIF nem a estrutura de pastas.

- `core/analyzer.py` e `core/imaging.py` só usam leitura binária;
- a única escrita é a **miniatura**, sempre dentro do cache do aplicativo
  (`%LOCALAPPDATA%\PhotoDedupe\thumbnails`), nunca junto das fotos.

## 2. Nada é removido sem confirmação explícita

`FileManager.execute()` recebe `confirmed: bool` e **levanta `PermissionError`** se for
chamado sem confirmação — não existe caminho de código que remova arquivos sem passar por
essa porta. Na interface, a confirmação vem de um diálogo que mostra:

- quantos arquivos serão movidos e quanto espaço será liberado;
- para onde vão (quarentena ou Lixeira);
- a lista completa, arquivo por arquivo, com a foto que será mantida em cada caso;
- botões **Cancelar** (em destaque, é o padrão) e **Confirmar remoção**.

Teste que garante: `tests/test_fileops.py::test_execucao_exige_confirmacao_explicita`.

## 3. Nada é apagado de verdade

O padrão é **mover para a pasta de quarentena**, preservando a estrutura de pastas de
origem. A alternativa é a **Lixeira do sistema**. O aplicativo nunca apaga definitivamente
um arquivo — exceto em *Configurações › Esvaziar quarentena*, uma ação manual, explícita e
com aviso de que não pode ser desfeita.

## 4. Nada é sobrescrito

Ao mover para a quarentena, conflitos de nome recebem sufixo numérico
(`foto.jpg` → `foto (1).jpg`), pela função `unique_path()`. O mesmo vale ao restaurar.

## 5. Validação arquivo por arquivo, duas vezes

Antes de mover, cada item é validado (e revalidado imediatamente antes da operação):

- o arquivo existe e é um arquivo comum;
- o tamanho é **o mesmo registrado na análise** (se mudou, o arquivo foi editado desde
  então e não é tocado);
- a foto que seria **mantida** no lugar dele existe de fato no disco;
- o arquivo não é ele próprio a foto recomendada;
- o arquivo não está dentro da quarentena.

Qualquer divergência bloqueia **apenas aquele item**; o restante do lote continua, e o
motivo aparece no resultado.

## 6. A melhor foto do grupo nunca é removida

A consulta que monta o plano (`Repository.selected_for_removal`) filtra
`is_reference = 0`: a foto principal de um grupo não entra em plano algum. Para remover a
foto que hoje é a principal, o usuário precisa antes **eleger outra como principal**
(botão ⭐ Principal) — assim, todo grupo sempre mantém ao menos uma foto.

## 7. Toda operação pode ser desfeita

Cada arquivo movido gera um registro em `actions` com origem, destino, tamanho e lote.
Em *Relatório › Desfazer lote*, os arquivos voltam para os locais originais. Um índice
legível (`_indice_quarentena.json`) também é gravado dentro da quarentena, permitindo
restauração manual mesmo sem o aplicativo.

## 8. Recomendações conservadoras

- Sugestão de remoção **apenas** nas categorias 🔴 duplicatas exatas e 🟠 duplicatas
  visuais. As categorias 🟡 e 🔵 existem para revisão e **nunca** marcam nada.
- Fotos em sequência (rajada), recortes e fotos de câmeras diferentes têm a similaridade
  limitada e não chegam às categorias que geram sugestão.
- A foto de referência de um grupo nunca é sugerida para remoção em outro grupo.

Verificação automatizada: `tools/benchmark.py` simula a aplicação de **todas** as
recomendações sobre uma biblioteca com gabarito e confere que **nenhuma cena fica sem
nenhuma foto**. No teste com 4.815 fotos: 2.236 arquivos marcados, **0 cenas perdidas**,
**0 fotos de rajada marcadas**.

## 9. Exportação antes de agir

O diálogo de confirmação oferece exportar a lista (CSV ou JSON) antes de executar. Pela
linha de comando: `photodedupe-cli plano --exportar plano.csv` mostra e exporta o plano
**sem** executá-lo (é preciso `--confirmar` para agir).

## 10. Decisões do usuário são permanentes

Marcar um grupo como "não são duplicatas" grava os pares na tabela `decisions`. Análises
futuras não voltam a sugerir aquele agrupamento. Escolhas manuais de manter/remover também
sobrevivem a um novo agrupamento.

## 11. Privacidade

- Nenhuma imagem é enviada para a internet; o aplicativo funciona offline.
- Não há chamadas de rede no código do aplicativo.
- Banco, miniaturas, logs e relatórios ficam no perfil do usuário.
- Os logs registram caminhos e mensagens de erro — nunca o conteúdo das fotos.
- Coordenadas de GPS só aparecem se o usuário ativar a opção.

## Em caso de dúvida

O aplicativo prefere **não sugerir** a sugerir errado. Se uma comparação é ambígua, ela cai
em uma categoria de revisão manual em vez de virar recomendação de remoção.
