# Investigation: Multi Compare painted black squares — mipmapped sampler on a single-level texture

Status: fixed 2026-11-18. Owner report: "why does Multi Compare make Malevich
squares for exactly these images?" Two 512x512 frames showed as opaque black
rectangles with only the filename label visible, while the same pair painted
correctly in Image Compare and in the export preview.

## Symptom

Any Multi Compare slot whose tile grid is **1x1** painted black: a 512x512
pair, a 64x64 pair, the owner's 512x512 `triplet_*` frames. A 2048x2048 slot
in the same canvas painted fine. Nothing in the log — no decode error, no
toast, `TileStore` lines present, slots and rects correct in
`get_canvas_scene`.

## Ladder

**Blast radius**: Multi Compare only; Image Compare and export painted the same
files. So the pipeline, the cache and the decoder are innocent — the tab's own
draw is not.

**Working-sibling diff**: inside MC, a 2048x2048 slot (multi-tile grid) painted
and a 512x512 slot (1x1 grid) did not. That is the array path vs the plain
path, so the split is the *draw branch*, not the image.

**Discriminators actually run** (leased instance, `IMGSLI_MC_FIRST_FRAME_DEBUG`
+ `--debug`):

| probe | result |
|---|---|
| `textures_resident` in every frame | `0` (but the plain path never uses tile residency, so this alone proved nothing) |
| the source QImage handed to the upload | `512x512 Format_RGBA8888`, pixel `(200,30,30,255)` — content is fine |
| the plain draw | ran: `draws=1`, non-null SRB, texture bound, `512x512` |
| force the array path for the same slot | **painted correctly** |

Forcing the array path was the one-line answer: the plain branch is broken,
independently of content.

**Mechanism**: the plain branch uploads a *single-level* `QRhiTexture` and bound
`renderer.sampler`, whose mipmap filter is `Linear`. A texture with one level
sampled through a mipmapped filter is **mipmap-incomplete** — GL returns black.
The array path is unaffected because its textures get a real mip chain
(`generate_all_dirty_mips`). The old comment in
`scene/renderer.py` asserted the opposite ("a texture with only one real mip
level samples level 0 regardless of this filter"), which is what let this
through: it is true of some drivers and false of others, and false on both the
host that hit it and this one.

## Fix

`MultiCompareRhiRenderer` owns a second sampler, `plain_sampler` (mag Nearest,
min Linear, **mipmap filter None**), and `SlotResources.ensure_tile_srb` binds
it for plain tiles. The array path keeps `renderer.sampler`. Headless fakes
without `plain_sampler` fall back to the old binding, so no test double needs
to know.

## Evidence

Live, leased instance, one slot each:

| slot | before | after |
|---|---|---|
| 512x512 pair | black rectangle, label only | image painted |
| 64x64 pair | black rectangle | (same branch, fixed by construction) |
| the owner's `triplet_2` / `triplet_1` 512x512 | black | painted |
| 2048x2048 (array path) | painted | painted (unchanged) |

Regression tests:
`src/tabs/multi_compare/tests/render/test_mc_plain_sampler_no_mips.py` — the
binding test fails on the pre-fix tree (verified in an isolated clone) and
passes after.

## Do not reintroduce

- A sampler whose mipmap filter is set must only ever see textures with a
  complete mip chain. A single-level texture needs `Filter.None_`.
- "One sampler for everything" is not safe across paths; the plain path is the
  one that carries no mips.
- A 1x1 grid is a *different draw branch*, not a degenerate case of the
  multi-tile one. Test the branch for every image size that lands on it —
  512x512 and smaller are ordinary photo sizes, not an edge case.
