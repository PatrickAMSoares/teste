# Histórico de versões

## 1.0.0

Primeira versão completa do PhotoDedupe.

### Detecção
- Duplicatas exatas por SHA-256.
- Duplicatas visuais combinando pHash, dHash, aHash, wHash, descritor visual local
  (HOG + cor oponente) e correlação estrutural.
- Reconhecimento de fotos recortadas por assinaturas do recorte central.
- Regras conservadoras contra falsos positivos: instantes de captura, proporção,
  câmera e textura.
- Quatro categorias de resultado com limites configuráveis.

### Qualidade
- Índice explicável de 0 a 100 (resolução, nitidez, compressão, formato, EXIF,
  tamanho e originalidade), com justificativas em português.
- Estimativa da qualidade JPEG pelas tabelas de quantização.
- Detecção de fotos desfocadas, escuras, superexpostas, corrompidas, minúsculas,
  capturas de tela e imagens não fotográficas.

### Interface
- Sete telas (início, análise, duplicatas, revisar, fotos com problema, relatório,
  configurações) em tema claro ou escuro.
- Comparação lado a lado com zoom sincronizado, EXIF e mapa de diferenças.
- Filtros, ordenação e alternância de detalhes técnicos.

### Segurança
- Nenhuma remoção sem confirmação explícita.
- Quarentena com histórico e desfazer; nada é apagado definitivamente.
- Validação por arquivo antes de cada operação; a melhor foto do grupo nunca é removida.

### Desempenho
- Análise paralela com memória constante e cache incremental.
- 25 fotos/s e 220 MB de pico em uma biblioteca de 4.815 fotos (4 processos).
- Reanálise sem mudanças em 0,1 s.

### Distribuição
- Executável e instalador para Windows (PyInstaller + Inno Setup), sem exigir
  Python instalado.
- Linha de comando para análise, relatório, plano de remoção e desfazer.
