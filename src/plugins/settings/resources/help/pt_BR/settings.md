---
description: Encontrar e mudar opções do aplicativo, incluindo seções das abas.
icon: settings.svg
---
## Configurações

Configurações é o diálogo das opções do aplicativo: páginas internas como **Geral** e **Aparência**, além de seções contribuídas pelas abas. As seções das abas ficam visíveis independentemente da sessão ativa.

:::figure{side=block width=100%}
![Diálogo de Configurações](../assets/settings_general.png)
A lista de seções à esquerda com **Geral** aberta — **Idioma**, **Tema**, **Notificações do sistema** e **Ativar registro de depuração**.
:::

### Abrir Configurações {#open-settings}

- **Arquivo** → **Configurações** (`Ctrl+,`).
- **Localizar ação** — `Ctrl+Shift+P`, depois o nome de uma página (Geral, Aparência, Teclado, …).
- **Saiba mais** — abre o tópico de ajuda de uma configuração quando existe.

### Pesquisar nas Configurações {#search}

Um campo de pesquisa fica acima da lista lateral. Cada seção correspondente aparece como uma única linha, por mais profundo que seja o acerto (nome de controle, título de grupo, qualquer idioma). Ativar uma linha abre essa seção com os acertos destacados. `Esc` limpa a pesquisa e restaura a lista lateral completa.

### Geral {#general}

- **Idioma** — o idioma da interface: en / ru / zh / pt_BR.
- **Tema** — auto / light / dark.
- **Notificações do sistema** — liga ou desliga as notificações do sistema operacional.
- **Ativar registro de depuração** — grava logs detalhados para diagnóstico.

### Aparência {#interface}

- **Modo da UI** — **Iniciante**, **Avançado** ou **Especialista**; **Iniciante** é o padrão, e a escolha da primeira execução fica aqui.
- **Fonte da UI** — interna, padrão do sistema ou uma família personalizada.
- **Escala da interface** — redimensiona toda a interface (0,5–2,5).
- **Comp. Máx. Nome (UI)** — limita o comprimento de um nome exibido.

### Desempenho {#performance}

- **Backend** — o backend de renderização; as opções dependem da plataforma, e a troca pode exigir reinício.

### Comparar imagens {#image_compare}

Opções de comparação de imagens, sempre disponíveis:

- **Interpolação do Zoom** — a qualidade de reamostragem do zoom.
- **Movimento da lupa** — mantém suave o seguimento da lente pelo cursor durante o arrasto; a linha **Suavização de laser** abaixo suaviza as guias da lupa. Cada linha tem um método próprio.
- **Destaque vermelho para lupas sobrepostas** — enquanto uma lupa é arrastada, a parte do anel de captura escondida atrás de outras lupas é destacada; **Colorir novas lupas automaticamente** dá a cada nova lente uma cor distinta.
- **FPS de Gravação** — a taxa de captura para o [Editor de vídeo](help://workspace.image_compare.video).
- **Cortar automaticamente bordas pretas ao carregar** — corta as bordas pretas quando uma imagem é carregada; desligado por padrão, então uma instalação nova mostra o quadro inteiro.
- **Calcular PSNR automaticamente** / **Calcular SSIM automaticamente** — mostram a métrica automaticamente; desligadas por padrão.

### Galeria — IA {#image-gallery-ai}

Os recursos de IA na [Galeria](help://workspace.image_gallery.triage) usam as configurações abaixo. Uma requisição precisa do interruptor ligado, de uma chave de API e de um modelo; a chave sozinha não basta. A chave não é gravada no arquivo de configurações: ela fica no chaveiro do sistema quando disponível e, caso contrário, apenas durante a sessão. Execuções por cartão: a análise pode consultar o modelo 1, 2, 3 ou 5 vezes e usa a mediana — mais execuções dão notas mais estáveis com custo proporcionalmente maior, mas quase empates ainda podem trocar de lugar.

- **Ativar análise de imagem por IA** — liga ou desliga os recursos de IA.
- **URL base da API** — o endereço do serviço de IA.
- **Chave de API** — a credencial desse serviço.
- **Nome do modelo** — o modelo que a IA usa nas requisições.
- **Orçamento de tokens da resposta** e **Esforço de raciocínio** — limites da resposta: o teto de tokens (vazio = automático) e o quanto o modelo raciocina antes de responder.
- **Redução da imagem (lado maior)** — o lado maior enviado ao modelo; **Original** mantém os bytes do próprio arquivo.
- **Execuções por cartão** — quantas avaliações independentes cada cartão recebe (1, 2, 3 ou 5). Cada execução é outra chamada de imagem, então mais execuções dão notas mais estáveis a um custo proporcionalmente maior; quase empates ainda podem trocar de lugar mesmo com 5.

### Teclado {#keyboard}

Os atalhos são agrupados por dono — **Plataforma**, depois um grupo por aba, por exemplo **Image Compare**. **Redefinir** restaura um atalho ou todos. As teclas fixas do canvas `WASD` e `Space` não podem ser remapeadas — veja [Atalhos de Teclado](help://platform.hotkeys).
