"""Which pixels does a side's SOURCE role mean? (the loupe's raw material)

The image-compare render plan carries two roles per side:

* DISPLAY — what the canvas paints. After ``unify_pair`` both sides live on the
  common canvas size (max w/h per side), so the smaller image is an UPSALED copy.
* SOURCE — what the magnifier samples through ``widget._source_texture_ids``
  ("source_0"/"source_1"). It must be the side's OWN decoded pixels, or the
  loupe magnifies an upscaled copy: measured live 2026-10-26 with
  ``IMGSLI_TILE_DUMP=1`` on a 2816x2304 + 768x576 pair, both source grids were
  registered at 2816x2304 and the loupe over the small side was mush that got
  worse when the canvas was zoomed.

Why this module exists: the role used to be a silent default —
``build_canvas_plan`` filled an omitted source with the DISPLAY image, so every
caller that did not name a source implicitly asked the loupe to magnify the
unify result. One resolver, used by the plan default and by the presentation,
keeps the role honest in one place.
"""

from __future__ import annotations


def original_pixel_tier(store, slot: int):
    """The side's own decoded pixels (pipeline pixel tier), pre-unify.

    ``viewport.session_data.image_state.image1/2`` hold the UNIFY result, so they
    cannot answer "what are this side's real pixels". The pipeline cache keeps
    each side's own store keyed by path; that store is what a magnifier samples.
    ``None`` (no cache entry yet, closed store) falls back to the caller's
    existing resolution.
    """
    try:
        doc = store.get_session_state_slot("document")
        path = doc.image1_path if slot == 1 else doc.image2_path
        if not path:
            return None
        ps = store.get_session_state_slot("pipeline")
        if ps is None:
            return None
        from tabs.image_compare.pipeline.cache import _pixel_key

        pixel = getattr(ps, "pixel", None) or {}
        cand = None
        try:
            cand = pixel.get(_pixel_key(path, None, None))
        except Exception:
            cand = None
        if cand is None:
            import os

            norm = os.path.normpath(path)
            for key, value in pixel.items():
                try:
                    if key[0] == norm:
                        cand = value
                        break
                except (TypeError, IndexError):
                    continue
        if cand is None:
            return None
        # A preview tier is not a source: handing the loupe a preview-sized
        # QImage (1024/2048/4096 long edge) when the display path already has
        # the real store would make it blurrier.
        try:
            from engine.store.tiled_pixel_store import is_preview_tier

            if is_preview_tier(cand):
                return None
        except Exception:
            pass
        if hasattr(cand, "is_open") and not cand.is_open:
            return None
        if hasattr(cand, "isNull") and cand.isNull():
            return None
        return cand
    except Exception:
        return None


def source_role_image(store, slot: int, display_image):
    """What the SOURCE role should be for this side: own pixels, else display."""
    return original_pixel_tier(store, slot) or display_image


def is_preview_basis(image) -> bool:
    """True when ``image`` is a preview-tier QImage — a SIZE guess, not pixels.

    A preview is normalised per side to a 1024/2048/4096 long edge, chosen from
    the canvas (``pipeline.cache.PREVIEW_BUCKETS``), so its dims do not
    describe the side's source. The live plan uses this to refuse to mix one
    side's preview dims with the other side's real dims: the resulting envelope
    ``max(w1,w2) x max(h1,h2)`` would describe neither pair (owner report
    2026-10-27, canvas ``[1024,1400]`` between ``[1600,1400]`` frames).

    ``False`` for own pixel stores, unified stores and PIL images — their dims
    are real (an unify upscale is envelope-sized, so it cannot skew the max).
    Never raises.
    """
    try:
        from engine.store.pixel_ops.unify import is_preview_tier

        return bool(is_preview_tier(image))
    except Exception:
        return False


def source_role_pair(store):
    """Both sides' SOURCE images for the live render plan.

    One call keeps the comment burden at the call sites down (``bg_dirty`` is on
    the file-size budget) and makes the rule impossible to half-apply: you either
    resolve both sides or none.
    """
    state = store.viewport.session_data.image_state
    return (
        source_role_image(store, 1, state.image1),
        source_role_image(store, 2, state.image2),
    )
