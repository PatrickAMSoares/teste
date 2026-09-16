# Arquitetura do PhotoDedupe

## Visão geral

```
                        ┌──────────────────────────────────────┐
  interface (PySide6)   │  main_window · pages/ · widgets/     │
                        └───────────────┬──────────────────────┘
                                        │ sinais Qt (nunca chamadas diretas de trabalho pesado)
                        ┌───────────────▼──────────────────────┐
  ponte                 │  ui/workers.py                       │
                        │  QThread + QThreadPool + cache       │
                        └───────────────┬──────────────────────┘
                                        │
                        ┌───────────────▼──────────────────────┐
  orquestração          │  core/pipeline.py                    │
                        │  descoberta → análise → agrupamento  │
                        └───┬───────────────┬──────────────────┘
                            │               │
        ┌───────────────────▼───┐   ┌───────▼──────────────────────────────┐
        │ core/scanner.py       │   │ core/analyzer.py (processos filhos)  │
        │ varredura do disco    │   │ imaging · hashing · embeddings ·     │
        └───────────────────────┘   │ exif · quality · badphotos           │
                                    └───────┬──────────────────────────────┘
                                            │
        ┌───────────────────────────────────▼──────────────────────────────┐
        │ db/ (SQLite em WAL)  ·  core/grouping.py  ·  core/similarity.py  │
        └───────────────────────────────────┬──────────────────────────────┘
                                            │
                     ┌──────────────────────┴───────────────────┐
                     │ core/fileops.py (quarentena, desfazer)   │
                     │ core/reports.py (CSV, Excel, JSON, PDF)  │
                     └──────────────────────────────────────────┘
```

Regra estrutural: **a interface não conhece algoritmo e o núcleo não conhece Qt**.
Todo o pacote `photodedupe.core` pode ser usado sem interface gráfica (é o que a CLI e os
testes fazem).

## Módulos

| Módulo | Responsabilidade |
|--------|------------------|
| `config.py` | preferências do usuário, limites de similaridade, extensões aceitas |
| `paths.py` | onde ficam banco, miniaturas, logs, relatórios e quarentena |
| `core/scanner.py` | varredura com `os.scandir`, filtros de extensão/tamanho/pastas |
| `core/imaging.py` | decodificação única e reduzida, métricas de imagem, miniaturas |
| `core/hashing.py` | SHA-256 e hashes perceptuais (aHash, dHash, pHash, wHash) |
| `core/embeddings.py` | descritor visual local (HOG + cor); back-end ONNX opcional |
| `core/exif.py` | leitura de metadados e identidade do instante de captura |
| `core/quality.py` | índice de qualidade 0–100 explicável |
| `core/badphotos.py` | desfoque, exposição, corrupção, prints, imagens não fotográficas |
| `core/analyzer.py` | junta tudo por arquivo; é o que roda nos processos de trabalho |
| `core/similarity.py` | comparação de duas fotos e classificação em categorias |
| `core/grouping.py` | candidatos, verificação e formação dos grupos |
| `core/pipeline.py` | orquestra as fases, paralelismo, pausa/retomada, progresso |
| `core/fileops.py` | plano de remoção, quarentena/Lixeira, histórico e desfazer |
| `core/reports.py` | exportação dos resultados |
| `db/database.py` | conexão, esquema versionado e migrações |
| `db/repository.py` | todas as consultas SQL do aplicativo |
| `ui/` | telas, widgets, tema e pontes para threads |

## Pipeline de análise

### 1. Descoberta
`os.scandir` recursivo, ignorando pastas de sistema e arquivos ocultos. Os arquivos são
gravados em lotes de 2.000 na tabela `files`. Arquivos já analisados cujo **tamanho e data
de modificação** não mudaram mantêm o resultado em cache — é a análise incremental.

### 2. Análise (paralela)
Cada arquivo pendente vai para um `ProcessPoolExecutor`. Por arquivo:

1. `SHA-256` do conteúdo (leitura em blocos de 1 MiB, memória constante);
2. **uma única decodificação**, já reduzida (`draft` do JPEG escolhe a escala na própria
   descompressão; HEIC via `pillow-heif`; RAW via `rawpy`, preferindo a miniatura embutida);
3. dessa decodificação saem todos os sinais:
   - `gray_hash` 32×32 → aHash, dHash, pHash, wHash e os hashes do **recorte central**,
   - `gray_desc`/`rgb_desc` 64×64 → descritor visual e assinatura de cor,
   - `rgb_mid` 256×256 → estatísticas de paleta (prints, memes),
   - `work_gray` (lado maior ≤ 1024) → nitidez, exposição e blocagem,
   - miniatura JPEG respeitando a proporção original;
4. EXIF, índice de qualidade e marcas de "foto ruim".

O número de tarefas em voo é limitado a `3 × workers`: isso mantém a memória constante
independentemente do tamanho da biblioteca.

Resultados voltam em lotes (padrão: 64) e são gravados em uma transação por lote.

### 3. Agrupamento

Comparar todas as fotos entre si seria O(n²) — 5 bilhões de comparações para 100 mil fotos.
O caminho é outro:

**a) Duplicatas exatas** — agrupadas por SHA-256 (custo linear, sem ambiguidade).

**b) Geração de candidatos** — *multi-index hashing*: cada hash de 64 bits é dividido em
4 faixas de 16 bits. Dois hashes a distância ≤ 3 compartilham, obrigatoriamente, pelo menos
uma faixa (princípio da casa dos pombos). São indexados pHash, dHash, wHash, assinatura de
cor **e os hashes do recorte central**, mais 3 tabelas LSH sobre os descritores visuais
(hiperplanos aleatórios determinísticos). Buckets muito grandes e o número de candidatos por
foto são limitados para o custo não explodir em bibliotecas com milhares de imagens quase
idênticas.

**c) Verificação** — cada par candidato passa por `similarity.compare`, que combina:

| Sinal | Peso | Forte em | Fraco em |
|-------|------|----------|----------|
| Hashes perceptuais | 0,30 | reescala, recompressão | recorte, imagens lisas |
| Descritor visual (HOG + cor) | 0,28 | brilho/contraste, recorte | cenas repetitivas |
| Correlação estrutural (NCC 32×32) | 0,42 | confirmação pixel a pixel | recorte forte |

Quando há indício de recorte, os pesos mudam para `0,20 / 0,30 / 0,50` e apenas pHash e
dHash entram (aHash e wHash não têm versão recortada e deixam de fazer sentido).

**d) Ajustes conservadores** — reduzem a similaridade quando há sinal de que são fotos
diferentes: instantes de captura distintos no EXIF (limite 91%), proporções diferentes ou
indício de recorte (94,5%), câmeras diferentes (93,5%), imagens com pouca textura (96%).

**e) Formação dos grupos** — *star clustering* em cascata, do critério mais rígido ao mais
frouxo (exatas → visuais → muito semelhantes → semelhantes). Cada grupo se forma em torno da
foto de melhor qualidade, e só entram fotos comparadas **diretamente com ela** — é isso que
evita o efeito de corrente (A ~ B, B ~ C, mas A ≠ C). Duas salvaguardas adicionais:

- uma foto só entra em um grupo se aquele for (praticamente) o seu melhor par, o que impede
  que uma foto parecida "capture" as cópias de outra;
- instantes de captura conflitantes dentro de um grupo impedem a classificação como duplicata.

## Índice de qualidade

Pontuação 0–100 com sete componentes e pesos fixos: resolução (30), nitidez (22),
compressão (18), formato (10), EXIF (8), tamanho do arquivo (7) e originalidade (5).

Destaques de implementação:

- **nitidez**: variância do Laplaciano medida sempre na imagem normalizada para lado maior
  de 1024 px, para que fotos de resoluções diferentes possam ser comparadas;
- **compressão**: bits por pixel + estimativa da qualidade JPEG a partir das **tabelas de
  quantização** (invertendo a relação usada pelos codificadores; erro ≤ 3 pontos nos testes)
  + medida de blocagem 8×8;
- **originalidade**: EXIF de câmera presente, dimensões batendo com as registradas pela
  câmera, formato RAW/TIFF, marcas de cópia no nome (`(1)`, `cópia`, `-min`, `WA0001`…) e
  software de edição no EXIF.

Cada componente produz uma frase em português, exibida na interface e no relatório.

## Banco de dados

SQLite em modo **WAL** (a interface lê enquanto a análise grava), um arquivo único no perfil
do usuário, sem servidor.

| Tabela | Conteúdo |
|--------|----------|
| `folders` | pastas monitoradas |
| `files` | caminho, tamanho, `mtime_ns`, situação, erro |
| `photos` | dimensões, formato, SHA-256, 4 hashes + 2 hashes de recorte, assinatura 32×32, descritores, qualidade, EXIF, marcas de foto ruim, miniatura |
| `groups` / `group_members` | grupos, categoria, similaridade, recomendação e escolha do usuário |
| `decisions` | pares marcados como "não são duplicatas" |
| `actions` | histórico de cada arquivo movido (origem, destino, lote) — base do desfazer |
| `sessions` | análises realizadas e suas estatísticas |
| `meta` | versão do esquema e impressão digital do último agrupamento |

Custo de armazenamento: ~6 KB por foto (assinatura 32×32 = 1 KB, dois descritores em
float16 = 640 B, EXIF e qualidade em JSON). Para 100 mil fotos: ~600 MB de banco e
~170 MB de assinaturas em memória durante o agrupamento.

Migrações são versionadas (`SCHEMA_VERSION`): um banco antigo é atualizado na abertura,
preservando o que já foi analisado.

## Desempenho

Medições em uma biblioteca sintética com 4.815 fotos (2,7 GB, 4 processos, fotos de 1–3 MP):

| Fase | Tempo |
|------|-------|
| Descoberta | < 1 s |
| Análise (decodificação, hashes, qualidade, miniaturas) | ~90 s (≈ 53 fotos/s agregado) |
| Agrupamento (878 mil candidatos, 410 mil comparações) | ~100 s |
| **Total** | **191 s — 25 fotos/s** |
| Pico de memória | 220 MB |
| Segunda análise, sem mudanças | 0,1 s |

Decisões que mais pesaram no resultado:

1. decodificar **uma vez** por arquivo e em resolução reduzida (`draft` do JPEG);
2. limitar as tarefas em voo, em vez de enfileirar tudo;
3. candidatos por indexação, nunca todos contra todos;
4. descarte rápido (`quick_reject`) antes da comparação completa;
5. recorte central calculado uma vez por foto e reaproveitado em todas as comparações;
6. impressão digital do agrupamento: sem mudanças, os grupos gravados são reaproveitados.

## Pontos de extensão

- **Outro descritor visual**: implemente `EmbeddingProvider` em `core/embeddings.py`.
  Um modelo ONNX local já é suportado sem escrever código (Configurações › Detecção).
- **Outros formatos**: registre o decodificador em `core/imaging.py`; o resto do pipeline
  não muda.
- **Outros critérios de qualidade**: acrescente o componente e o peso em `core/quality.py`
  (`WEIGHTS`); a interface e os relatórios exibem automaticamente.
- **Outros relatórios**: adicione um exportador em `core/reports.py`.
