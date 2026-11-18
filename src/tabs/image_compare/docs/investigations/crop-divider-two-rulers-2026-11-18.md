# Investigation: the crop and the divider were measured with two rulers

Status: fixed 2026-11-18. Owner report: "512x512 shows up as 256x512 in Image
Compare, the export preview is fine — and the seam between the images lags
behind the split line, as if they were measured with different rulers."

## Symptom

A 512x512 pair, dark frames with a bright band. Image Compare painted the pair
narrower than the square it is, while the export preview and the exported file
were correct. Separately (and this is the real bug): dragging the split line put
the cut somewhere else, and the further from centre, the further off it was.

Both symptoms in one capture: the HUD read `512x512`, the canvas drew a 312:512
rectangle, and the seam sat ~50 px away from the line at `split ≈ 0.15`.

## Ladder

Working sibling: the same pair with the crop switched off draws square and the
line sits exactly on the cut, so the renderer and the export path are fine — the
divergence is between two geometry owners inside the live canvas.

Blast radius: both tabs (Multi Compare looked "worse" because its composition
fits per-slot rects against the same canvas basis), and every canvas-px overlay
(divider, guides, capture ring, magnifier anchor), not just the seam.

Discriminator: `get_canvas_scene` reports the plan's canvas basis next to the
runtime content rect. Healthy is `canvas == content_rect` in aspect; broken was
`canvas 312x512` vs `content_rect (270, 0, 484, 484)` in the same frame.

## Root cause

Two owners, one number:

- The **image** is drawn from the plan's canvas basis `. The plan builder clips
  both sides by the effective crop box, so a 312x512 crop is what gets painted.
- The **divider line, its clip, the canvas→widget projection and the drag→split
  mapping** read `runtime_state._content_rect_px` / `_inner_content_rect_px`.
  `refresh_live_content_rect` (the per-plan-application owner of that rect)
  falls back to `get_image_dims(plan.image1/2)` when the plan carries no
  envelope — and the installed display tier is a lazy full-frame store that
  still reports the **uncropped** size (`size=[512, 512]` for a 312x512 crop).
  The rect therefore fitted the source square.

At `split = 0.5` both rects are centred, so the line lands on the cut and the
bug hides. Off-centre the line is at `270 + 484·s` while the cut is at
`365 + 295·s`: up to ~60 px at `s = 0.25`, growing with the crop's width error.
The same stale rect feeds `apply_split_drag`
(`events/image_label/geometry.py` → `tab.get_canvas_content_rect_px()`), so the
drag itself computed the split fraction against the wrong ruler.

The rect also went stale *after* a resize: `resize_canvas` refits from
`_stored_pil_images` (boxless previews) whenever the crop boxes are not
resolvable yet, and `geometry_needs_bootstrap` stops the per-frame re-derivation
after the first write.

## Fix

1. The plan carries the **box-clipped envelope** (`envelope_dims`) whenever a
   crop box is active — the same numbers its canvas basis uses. The letterbox
   owner still ignores the kwarg under a box, so nothing is double-clipped.
2. `resize_canvas` passes that envelope through, so a resize cannot refit the
   uncropped preview.
3. `update_common_letterbox_geometry`'s wrapper grew the passthrough kwarg.
4. The crop result is cached on the service and dispatches nothing to the
   Store, so `CropService.warm_cache_async` gained an optional `on_done`
   (queued to the GUI thread) and the load path refreshes the chrome there —
   without it the HUD and the canvas kept the pre-crop state until some
   unrelated input.

Adjacent, same report:

- `auto_crop_black_borders` now defaults to **off** (a fresh install must not
  silently crop the pair it is asked to compare). A stored value still wins.
- The InfoHUD resolution chip names the crop: `512x512 → 312x512`
  (loaded → drawn), so a cropped pair never reads as a squeezed one.

## Evidence

Live, leased instance, red/blue 392x512 pair (~/tmp probe images):

| split | painted cut | divider line |
|---|---|---|
| 0.043 | 342 | 341–343 |
| 0.149 (crop off) | 342 | 341–343 |

Frame numbers from the broken build: plan `312x512`, `DIVDBG
content_rect=(270, 0, 484, 484)` — the 94 px offset that the fix removes.

Regression tests: `src/tabs/image_compare/tests/render/test_crop_divider_ruler.py`
(three geometry tests fail on the pre-fix tree, pass after),
`tests/render/test_autocrop_service.py::test_warmup_reports_completion_including_an_already_warm_cache`.

## Do not reintroduce

- Never fit the content rect from the display tier when a crop box is active:
  the tier lies about its size by design (lazy full-frame store).
- A canvas-px consumer must read the same basis the painter used. If a new
  overlay needs a rect, take it from the plan, not from a cached runtime field
  whose writer is another component.
- Off-centre is the only split that proves alignment. A 0.5-only check passes
  with two different rulers.
