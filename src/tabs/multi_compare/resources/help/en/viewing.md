---
description: Zoom, pan, and focus the grid without touching files.
icon: move.svg
---
## Multi Compare Viewing

Viewing controls how the grid is displayed and never touches the source files.

### Focus mode {#focus-mode}

Click a **slot** (a grid cell holding one image) to focus it: the rest of the grid dims and that slot fills the view. A press that turns into a drag moves the slot instead, and clicking the dimmed area or pressing `Esc` leaves focus. Zoom and pan keep working while a slot is focused.

### Zoom and pan {#zoom-and-pan}

- **Shared view** — zoom and pan apply to the whole grid, not to each slot separately.
- **Ctrl+wheel** — zooms the whole scene: the grid and the margins around it scale together, so the layout keeps its proportions. A plain wheel zooms the slot under the cursor.
- **Save view** — an export option writes exactly this visible region at native resolution; see [Export](help://workspace.multi_compare.export#save-and-export).
- **Wheel** — zooms from 1.0x to 50x in 1.1x steps; the point under the cursor stays in place.
- **Pan limits** — panning stops at the edge of the image; at fit size or smaller the view snaps back.
- **Middle-drag** — drag with the middle mouse button to pan.
- **`0`** — resets zoom and pan.

### Keyboard {#keyboard}

The shortcuts for this tab are listed in [Hotkeys](help://platform.hotkeys).

### Grid and labels {#grid-and-labels}

The divider settings live in [Multi Compare Layouts](help://workspace.multi_compare.layouts#dividers). **Label Settings** opens a panel that styles the slot label text.

### Slot crop {#slot-crop}

Crop trims the black borders off a slot's image. **Crop** in a slot's right-click menu turns it on or off for that one slot; until toggled, a slot follows **Auto-crop black borders on load** in [Settings](help://platform.settings#image_compare). An empty slot has nothing to crop. The crop shows in the grid and in saved images.
