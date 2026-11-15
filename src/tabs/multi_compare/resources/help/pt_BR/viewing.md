---
description: Zoom, panorâmica e foco na grade sem alterar arquivos.
icon: move.svg
---
## Visualização da Comparação múltipla

A visualização controla como a grade é exibida e nunca altera os arquivos de origem.

### Modo de foco {#focus-mode}

Clique em um **slot** (uma célula da grade que contém uma imagem) para focalizá-lo: o resto da grade escurece e esse slot preenche a vista. Um pressionar que vira arrasto move o slot, e clicar na área escurecida ou pressionar `Esc` sai do foco. O zoom e a panorâmica continuam funcionando enquanto um slot está em foco.

### Zoom e panorâmica {#zoom-and-pan}

- **Visão compartilhada** — o zoom e a panorâmica se aplicam à grade inteira, não a cada slot separadamente.
- **Ctrl+roda** — aplica zoom à cena inteira: a grade e as margens ao redor escalam juntas, então o layout mantém as proporções. A roda sozinha dá zoom no slot sob o cursor.
- **Salvar vista** — opção de exportação que grava exatamente esta região visível em resolução nativa; veja [Exportação](help://workspace.multi_compare.export#save-and-export).
- **Roda** — aplica zoom de 1,0x a 50x em passos de 1,1x; o ponto sob o cursor permanece no lugar.
- **Limites da panorâmica** — a panorâmica para na borda da imagem; no tamanho ajustado ou menor, a vista volta ao lugar.
- **Arrastar com o botão do meio** — arraste com o botão do meio do mouse para fazer a panorâmica.
- **`0`** — redefine o zoom e a panorâmica.

### Teclado {#keyboard}

Os atalhos desta aba estão listados em [Atalhos de Teclado](help://platform.hotkeys).

### Grade e rótulos {#grid-and-labels}

As configurações dos divisores estão em [Layouts da Comparação múltipla](help://workspace.multi_compare.layouts#dividers). **Configurações de rótulo** abre um painel que estiliza o texto do rótulo do slot.

### Recorte do slot {#slot-crop}

O recorte remove as bordas pretas da imagem de um slot. O **Crop** no menu de botão direito de um slot o ativa ou desativa para aquele slot; até ser alternado, um slot segue **Cortar automaticamente bordas pretas ao carregar** em [Configurações](help://platform.settings#image_compare). Um slot vazio não tem nada a recortar. O recorte aparece na grade e nas imagens salvas.
