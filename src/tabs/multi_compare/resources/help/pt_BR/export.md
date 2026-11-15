---
description: Guardar a grade como imagem e mantê-la na sessão.
icon: save_icon.svg
---
## Exportação da Comparação múltipla

A grade é salva como uma única imagem, e a sessão mantém a grade, então ela volta quando a sessão é reaberta.

### Salvamento e exportação {#save-and-export}

- **Salvamento rápido** (`Ctrl+S`) — grava a imagem com as configurações usadas por último, sem diálogo.
- **Salvar grade** — abre o diálogo de salvamento por meio de **Localizar ação**; veja [Exportação](help://workspace.export).
- **Resultado** — a imagem salva mostra a grade como ela aparece na tela.
- **Crop** — o recorte de cada slot é aplicado antes de os slots serem combinados.
- **Salvar vista** — grava apenas a região que o visor mostra, com zoom e deslocamento, em resolução nativa: os pixels do arquivo são os pixels da tela de composição. **Crop** e **Salvar vista** se substituem, porque ambos mudam de que parte a imagem salva é um recorte.
- **Slots vazios** — slots sem imagem ficam de fora da imagem salva.

:::figure{side=block width=100%}
![Diálogo Salvar grade](../assets/export_save_dialog.png)
O diálogo de salvamento de **Salvar grade**: a prévia da grade combinada à esquerda e, à direita, a pasta de saída, o nome do arquivo, o formato e as opções de PNG.
:::

### Sessões {#sessions}

Uma sessão armazena os caminhos e rótulos das imagens dos slots, o layout, o slot em foco, o zoom e a panorâmica, e o estilo dos rótulos e divisores. Os próprios arquivos de imagem não são copiados — a sessão os reabre a partir de seus caminhos no disco.

- **Reabertura** — uma sessão reaberta mostra seus slots de imediato e carrega suas imagens a partir de seus caminhos; uma sessão que não está ativa carrega suas imagens quando fica ativa.
- **Novas sessões** — uma nova sessão começa com o estilo de divisor e rótulo usado por último.
