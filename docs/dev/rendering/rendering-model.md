# Rendering Model

There is one renderer backend and two state-preparation paths:

- **Live authoring path** — the interactive canvas reads live `Store`/
  `ViewState`/feature state every frame. The only path a user's drag,
  toolbar click, or shortcut should ever mutate.
- **Snapshot replay path** — preview, image export, video preview/export,
  thumbnails build a frozen snapshot, turn it into a render plan, and feed
  that plan through the *same* passes. No parallel rendering logic for "what
  export looks like."

**Rule:** a fix that only works in export/preview/video code with no
live-canvas counterpart is the wrong fix. It will drift the first time the
live path changes underneath it.

The distinction between modes is an explicit `mode` value (`"interactive"` /
`"preview"` / `"export"`) passed into render-context construction. That's a
convenience for expressing `SceneVisibility` decisions (see
[render-pass-contract.md](render-pass-contract.md)) — it is not license to
sprinkle `if mode == "export"` branches inside a pass.

## Snapshot Renderer Notes

Preferred export/video direction:
- one snapshot-driven render-plan builder
- one QRhi/offscreen renderer backend
- multiple snapshot producers (`live store -> snapshot`, `timeline -> snapshot(t)`)

Image export preview/final and video preview/export differ by snapshot
source and target surface, not by feature-specific scene assembly.

**Thumbnail strip fast path (GPU band).** Filmstrip tiles (~160x90) are the one
snapshot consumer that does not go through one render per tile:
`services/video_snapshot_rendering/strip_window.py` plans the visible blocks
into ONE window (per-tile plans, per-tile texture-array layers) and
`strip_pass.py` records them in a single `beginPass`/`endPass`, driven by
`presenter_parts/strip_band.py` on every view change. That is the strip's only
renderer — the per-frame QPainter thumbnail pipeline (a tile at a time on a
worker pool, then a pixmap per timeline index) was deleted in strip atlas
phase 3, so there is no second tile path to drift from the live canvas or the
export. A tile is still exactly the 160x90 `THUMBNAIL_TARGET_SIZE`, which is
what selects the full-bleed cover plan. Pinned by
`src/tabs/image_compare/tests/video/test_strip_window.py`,
`test_strip_band_wiring.py` and `tests/contracts/test_strip_single_renderer.py`.

Snapshot prepare must **not** bake letterbox or feature padding into image
pixels. Keep `TiledPixelStore` (or other unpadded sources) on the plan and
express pads via `CanvasGeometry` / `overlay_clip_rect` + shader letterbox,
matching the live canvas geometry model.

**LOD parity:** the mipmap pyramid level for a given on-screen scale is
picked by one pure function, `src/engine/cache/lod.py::select_level`. Live
canvas, export preview, final export and video snapshots all call it with
the same `dest_scale` derivation so they resolve to the same pyramid level
— see [tile-rendering-system.md](tile-rendering-system.md#mipmap-pyramid-and-lod-selection).
