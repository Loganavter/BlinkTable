---
description: Alinhar duas imagens e examinar as diferenças.
icon: vertical_split.svg
---
## Comparação

A comparação mostra duas imagens lado a lado, para alinhar o mesmo detalhe nas duas.

### Linha divisória {#split-line}

**Divisor** — a fronteira móvel entre as duas imagens. Arraste-o sobre o par para trazer a mesma posição de cada imagem até a fronteira.

- **Orientação da divisão** — alterna a divisão entre horizontal e vertical.
- **Largura do divisor** — gire a rolagem sobre ele para mudar a espessura da fronteira.
- **Cor do divisor** — define a cor da fronteira.
- **Visibilidade do divisor** (`L`) — mostra ou oculta a fronteira.
- **Divisor** — reúne orientação, largura e cor em um único controle (rolagem / botão direito / botão do meio); passe o cursor para ver a dica de gestos.

:::figure{side=block width=280}
![Linha divisória](../assets/split_line.png)
Os controles do divisor na barra de ferramentas, nas formas compacta e expandida.
:::

### Percorrer imagens com a rolagem {#scroll-images}

- **Rolagem no canvas** — a rolagem avança a lista do lado sob o cursor: à esquerda de um divisor vertical ou acima de um horizontal fica o lado 1, a outra metade é o lado 2.
- **`Shift` + rolagem** — avança as duas listas juntas.
- **Pré-visualização de uma imagem** — com apenas uma imagem visível, a rolagem sempre avança o lado visível.
- **Rolagem no menu suspenso** — rolar sobre um menu suspenso troca aquele lado sem abrir o painel.
- **Trocar** (`X`) — um clique curto troca o par atual; um pressionamento longo troca as duas listas.

### Rótulos {#labels-and-metrics}

- **Sobreposição de nomes** (`N`) — mostra ou oculta os rótulos de nome de arquivo no canvas; os rótulos podem ser gravados nas imagens salvas.
- **Etiqueta de tamanho** — a etiqueta no canto, acima do nome do arquivo, mostra o tamanho da imagem; com um recorte ativo ela lê `512x512 → 312x512` (carregado → desenhado), então um par recortado nunca parece comprimido.
- **Zoom** — os rótulos somem enquanto a vista está ampliada e voltam no zoom de `100%`.
- **Configurações de texto do rótulo** — abre um painel para tamanho da fonte, peso, opacidade, cores, fundo e posicionamento.

### Métricas {#metrics}

- **PSNR** / **SSIM** — métricas de diferença, desligadas por padrão. Ligue o cálculo automático com **Calcular PSNR automaticamente** e **Calcular SSIM automaticamente** em **Comparar imagens** nas [Configurações](help://platform.settings#image_compare).

### Mover uma imagem entre abas {#moving-between-tabs}

**Mover** envia uma imagem para outra aba do espaço de trabalho sem copiá-la para o projeto primeiro.

1. No menu de contexto da imagem, no canvas ou na lista, escolha **Mover**.
2. Uma cópia semitransparente da imagem aparece ao lado do cursor.
3. Clique na aba do espaço de trabalho ou no canvas onde a imagem deve ficar — o mesmo posicionamento de **Duplicar** ou colar.
4. Pressione `Esc` ou clique com o botão direito para cancelar a movimentação.

### Ações do menu de contexto {#context-menu-actions}

- **Propriedades** — abre [Propriedades da imagem](help://platform.image_properties) a partir do menu de contexto de uma linha da lista, mostrando os metadados do arquivo e o lado e a avaliação na sessão atual.

### Modos de canal {#channel-modes}

**Modo de canal** (`C`) alterna entre RGB, Vermelho, Verde, Azul e Luminância, para inspecionar um único canal sem sair do canvas.

### Modos de diferença {#difference-modes}

**Modo de diferença** (`H`) percorre as vistas de diferença:

:::figure{side=block width=280}
![Modo de diferença](../assets/difference_modes.png)
O menu **Modo de diferença**: a lista percorrida com `H`.
:::

- **Diferença desligada** — nenhuma diferença é destacada.
- **Destaque** — colore as regiões em que as duas imagens diferem.
- **Escala de cinza** — transforma a diferença de intensidade em tons de cinza.
- **Bordas** — mostra a diferença ao longo das bordas da imagem.
- **SSIM** — mostra a similaridade estrutural do par.

O canvas ao vivo, as imagens exportadas e a gravação de vídeo mantêm a mesma aparência. Os modos de diferença combinam com as vistas de canal. Para inspeção local, use a [Lupa](help://workspace.image_compare.magnifier).
