---
description: Store the grid as a picture and keep it in a session.
icon: save_icon.svg
---
## Multi Compare Export

The grid saves as a single image, and the session keeps the grid so it returns when reopened.

### Save and export {#save-and-export}

- **Quick Save** (`Ctrl+S`) — writes the image with the settings used last, without a dialog.
- **Save Grid** — opens the save dialog through **Find Action**; see [Export](help://workspace.export).
- **Result** — the saved image shows the grid as it appears on screen.
- **Crop** — each slot's crop is applied before the slots are combined.
- **Save view** — writes only the region the viewport shows, zoom and pan included, at native resolution: what you see is the file's pixel grid. **Crop** and **Save view** replace each other, because both change what the saved picture is a crop of.
- **Empty slots** — slots without an image are left out of the saved image.

:::figure{side=block width=100%}
![Save grid dialog](../assets/export_save_dialog.png)
The save dialog for **Save Grid**: a preview of the combined grid, plus **Output directory**, **File name**, **Format**, and the **PNG** options.
:::

### Sessions {#sessions}

A session stores the image paths and labels of its slots, the layout, the focused slot, zoom and pan, and the label and divider styling. The image files themselves are not copied — the session reopens them from their paths on disk.

- **Reopening** — a reopened session shows its slots right away and loads its images from their paths; a session that is not active loads its images when it becomes active.
- **New sessions** — a new session starts with the divider and label styling used last.
