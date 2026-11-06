---
description: Salvar o canvas atual como uma imagem estática.
icon: save_icon.svg
---
## Exportação

A exportação salva o canvas atual como uma imagem estática, no formato e no tamanho escolhidos.

### Salvar uma imagem {#saving-an-image}

**Salvar Resultado**, na barra de ferramentas ou em **Localizar ação**, abre o diálogo de exportação, onde se escolhe o arquivo de saída e as opções.

- **Caminho** — o diretório de saída e o nome do arquivo.
- **Formato** — PNG, JPEG, WEBP, BMP, TIFF ou JXL.
- **Pré-visualização** — mostra o resultado composto antes de o arquivo ser gravado.

:::figure{side=block width=630}
![Diálogo de exportação](../assets/export_dialog.jpg)
O diálogo de exportação: a **Pré-visualização** mostra o resultado composto antes de o arquivo ser gravado.
:::

### Resolução e qualidade {#resolution-and-quality}

- **Tamanho** — a largura e a altura de saída; um bloqueio de proporção mantém as proporções.
- **Qualidade** — o nível de compressão; aparece apenas em formatos que armazenam a imagem com perdas, então uma exportação em PNG ou BMP não a oferece.
- **PNG** — o nível de compressão do PNG e a opção **Otimizar PNG**.
- **Preencher fundo** — preenche as bordas vazias ao redor da imagem com a **Cor de Fundo**; oferecido apenas para formatos que mantêm transparência (PNG, TIFF, WEBP, JXL) e apenas enquanto o resultado composto tem bordas vazias.
- **Cortar pelas imagens** — apenas no Image Compare: exporta o conteúdo das imagens. As janelas de corte detectadas são aplicadas (bordas pretas ficam de fora) e as margens vazias ao redor da comparação são cortadas. Os campos de tamanho seguem o tamanho cortado, então sempre mostram o arquivo que será gravado.

### Metadados e favoritos {#metadata-and-favorites}

- **Metadados** — o comentário é gravado no arquivo por padrão. Desmarque **Incluir metadados** para salvar a imagem sem ele; o campo de comentário então se oculta. **Lembrar por padrão** mantém o texto do comentário para a próxima exportação.
- **Favoritos** — **Definir como Favorito** fixa o diretório atual, e **Usar Favorito** o recupera.
- **Rótulos** — com **Sobreposição de nomes** ligada, os nomes de arquivo são desenhados na imagem exportada.

### Salvamento rápido {#quick-save}

- **`Ctrl+S`** — **Salvamento rápido** grava o arquivo com as últimas configurações de exportação sem reabrir o diálogo.
- **Bandeja** — o menu do ícone na bandeja reabre o último arquivo salvo e a sua pasta.

### Gravação e vídeo {#video-editor}

Para gravar uma sessão ou codificar vídeo e GIF, use o [Editor de vídeo](help://workspace.image_compare.video); esta página cobre apenas imagens estáticas.
