---
description: Find and change application options, including tab sections.
icon: settings.svg
---
## Settings

Settings is the dialog for the application's options: built-in pages such as **General** and **Appearance**, plus sections contributed by tabs. Tab sections stay visible regardless of the active session.

:::figure{side=block width=100%}
![Settings dialog](../assets/settings_general.png)
The section list on the left with **General** open — **Language**, **Theme**, **System notifications**, and **Enable debug logging**.
:::

### Open Settings {#open-settings}

- **File** → **Settings** (`Ctrl+,`).
- **Find Action** — `Ctrl+Shift+P`, then a page name (General, Appearance, Keyboard, …).
- **Learn more** — opens the help topic for a setting when one is available.

### Search inside Settings {#search}

A search field sits above the sidebar. Each matching section appears as a single row, however deep the match is (a control name, a group title, any language). Activating a row opens that section with the matches highlighted. `Esc` clears the search and restores the full sidebar.

### General {#general}

- **Language** — the interface language: en / ru / zh / pt_BR.
- **Theme** — auto / light / dark.
- **System notifications** — turns OS-level notifications on or off.
- **Enable debug logging** — writes verbose logs for troubleshooting.

### Appearance {#interface}

- **UI Mode** — **Beginner**, **Advanced**, or **Expert**; **Beginner** is the default and the first-run pick lands here.
- **UI Font** — built-in, system default, or a custom family.
- **Interface Scale** — resizes the whole interface (0.5–2.5).
- **Maximum Name Length (UI)** — caps the length of a displayed name.

### Performance {#performance}

- **Backend** — the rendering backend; the choices depend on the platform, and switching may require a restart.

### Image Compare {#image_compare}

Image-compare options, always available:

- **Zoom Interpolation** — the resampling quality for zoom.
- **Magnifier movement** — keeps the lens following the pointer smooth while you drag it; the **Laser smoothing** row below smooths the magnifier guides. Both rows carry a method of their own.
- **Red highlight for overlapping magnifiers** — while you drag a magnifier, the part of its capture ring hidden behind other magnifiers is highlighted; **Auto-color new magnifiers** gives each new lens a distinct color.
- **Recording FPS** — the capture rate for the [Video Editor](help://workspace.image_compare.video).
- **Auto-crop black borders on load** — trims black borders when an image is loaded; off by default, so a fresh install shows the full frame.
- **Auto-calculate PSNR** / **Auto-calculate SSIM** — show the metric automatically; off by default.

### Image Gallery — AI {#image-gallery-ai}

AI features in [Image Gallery](help://workspace.image_gallery.triage) use the settings below. A request needs the switch on, an API key, and a model; the key alone is not enough. The key is not written to the app's settings file: it is kept in the system keyring when one is available, otherwise only for the session. Runs per card: an analysis can ask the model 1, 2, 3 or 5 times and takes the median — more runs give steadier scores at a proportionally higher cost, but near-identical variants can still swap places.

- **Enable AI image analysis** — turns the AI features on or off.
- **API base URL** — the address of the AI service.
- **API key** — the credential for that service.
- **Model name** — the model the AI uses for requests.
- **Answer token budget** and **Reasoning effort** — limits on the answer: the token cap (empty = auto) and how much the model reasons before answering.
- **Image downscale (max edge)** — the largest side sent to the model; **Original** keeps the file's own bytes.
- **Runs per card** — how many independent judgments each card gets (1, 2, 3 or 5). Every run is another image call, so more runs mean steadier scores at a proportionally higher cost; near-ties can still swap places even at 5.

### Keyboard {#keyboard}

Shortcuts are grouped by owner — **Platform**, then a group per tab, for example **Image Compare**. **Reset** restores one shortcut or all of them. The fixed canvas keys `WASD` and `Space` cannot be remapped — see [Hotkeys](help://platform.hotkeys).
