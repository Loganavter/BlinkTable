---
description: Turn the current canvas into a saved still image.
icon: save_icon.svg
---
## Export

Export saves the current canvas as a still image, in the format and size you choose.

### Save a still {#saving-an-image}

**Save Result** on the toolbar or in **Find Action** opens the export dialog, where you pick the output file and its settings.

- **Path** — the output directory and file name.
- **Format** — PNG, JPEG, WEBP, BMP, TIFF, or JXL.
- **Preview** — shows the composed result before the file is written.

:::figure{side=block width=630}
![Export dialog](../assets/export_dialog.jpg)
The export dialog: **Preview** shows the composed result before the file is written.
:::

### Resolution and quality {#resolution-and-quality}

- **Size** — the output width and height; an aspect-ratio lock keeps the proportions.
- **Quality** — the compression level; it appears only for formats that store the image with loss, so a PNG or BMP export does not offer it.
- **PNG** — the PNG compression level and the **Optimize PNG** toggle.
- **Fill background** — fills the empty edges around the image with the **Background Color**; offered only for formats that keep transparency (PNG, TIFF, WEBP, JXL) and only while the composed result has empty edges.
- **Crop to images** — Image Compare only: keeps the area the images occupy and trims the empty margins around it. The size fields follow the trimmed size, so they always show the file you get.

### Metadata and favorites {#metadata-and-favorites}

- **Metadata** — the comment is written into the file by default. Clear **Include metadata** to save the image without it; the comment field then hides. **Remember by default** keeps the comment text for the next export.
- **Favorites** — **Set as Favorite** pins the current directory, and **Use Favorite** recalls it.
- **Labels** — with **Filename Overlay** on, file names are drawn into the exported image.

### Quick save {#quick-save}

- **`Ctrl+S`** — **Quick Save** writes the file with the last export settings without reopening the dialog.
- **Tray** — the tray-icon menu reopens the last saved file and its folder.

### Recording and video {#video-editor}

To record a session or encode video and GIF, use the [Video Editor](help://workspace.image_compare.video); this page covers still images only.
