---
description: Line up two images and study their differences.
icon: vertical_split.svg
---
## Comparison

Comparison shows two images side by side, so you can line up the same detail on both.

### Split line {#split-line}

**Divider** — the movable border between the two images. Drag it across the pair to bring the same position of each image to the border.

- **Split Orientation** — switches the split between horizontal and vertical.
- **Divider Width** — scroll over it to change how thick the border is.
- **Divider Color** — sets the border color.
- **Divider Visibility** (`L`) — shows or hides the border.
- **Divider** — packs orientation, width, and color into one control (scroll / right-click / middle-click); hover it to see the gesture tooltip.

:::figure{side=block width=280}
![Split line](../assets/split_line.png)
The divider controls on the toolbar, in their compact and expanded forms.
:::

### Scroll through images {#scroll-images}

- **Canvas wheel** — scrolling steps the list on the side under the cursor: left of a vertical divider or above a horizontal one is side 1, the other half is side 2.
- **`Shift` + wheel** — steps both lists together.
- **Single-image preview** — with only one image shown, the wheel always steps the visible side.
- **Dropdown wheel** — scrolling over a dropdown steps that side without opening its panel.
- **Swap** (`X`) — a short click exchanges the current pair; a long press swaps both lists.

### Labels {#labels-and-metrics}

- **Filename Overlay** (`N`) — shows or hides file-name labels on the canvas; the labels can be written into saved images.
- **Size chip** — the corner chip above the file name shows the image size; while a crop is active it reads `512x512 → 312x512` (loaded → drawn), so a cropped pair never looks like a squeezed one.
- **Zoom** — the labels disappear while the view is zoomed and come back at `100%` zoom.
- **Label Text Settings** — opens a panel for font size, weight, opacity, colors, background, and placement.

### Metrics {#metrics}

- **PSNR** / **SSIM** — difference metrics, off by default. Turn on the automatic calculation with **Auto-calculate PSNR** and **Auto-calculate SSIM** under **Image Compare** in [Settings](help://platform.settings#image_compare).

### Moving an image between tabs {#moving-between-tabs}

**Move** sends an image to another workspace tab without copying it into the project first.

1. In the image context menu on the canvas or in the list, choose **Move**.
2. A translucent copy of the image appears next to the cursor.
3. Click the workspace tab or the canvas where you want to place the image — the same placement as Duplicate or paste.
4. Press `Esc` or right-click to cancel the move.

### Context menu actions {#context-menu-actions}

- **Properties** — opens [Image Properties](help://platform.image_properties) from a list-row context menu, showing file metadata and the side and rating in the current session.

### Channel modes {#channel-modes}

**Channel Mode** (`C`) cycles RGB, Red, Green, Blue, and Luminance, so you can inspect a single channel without leaving the canvas.

### Difference modes {#difference-modes}

**Difference Mode** (`H`) cycles through the difference views:

:::figure{side=block width=280}
![Difference mode](../assets/difference_modes.png)
The **Difference Mode** menu: the list you cycle through with `H`.
:::

- **Off** — no difference is highlighted.
- **Highlight** — colors the regions where the two images differ.
- **Grayscale** — turns the intensity difference into shades of gray.
- **Edge Comparison** — shows the difference along image edges.
- **SSIM Map** — shows the structural similarity of the pair.

The live canvas, exported stills, and the video recording keep the same look. Difference modes combine with channel views. For local inspection, use the [Magnifier](help://workspace.image_compare.magnifier).
