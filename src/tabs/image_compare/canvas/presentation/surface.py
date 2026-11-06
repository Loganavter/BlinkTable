from __future__ import annotations

import logging
from core.log_names import APP_LOGGER_NAME

_log = logging.getLogger(f"{APP_LOGGER_NAME}.canvas_presentation.surface")

from tabs.image_compare.canvas.presentation.plan_builder import (
    build_canvas_plan,
    build_live_store_presentation,
)


def apply_store_to_canvas(
    canvas,
    store,
    image1,
    image2,
    *,
    fit_content: bool = False,
    source_image1=None,
    source_image2=None,
    source_key=None,
    display_cache_key=None,
    clip_overlays_to_image_bounds: bool = False,
):
    from ui.canvas_presentation.plan_applicator import apply_canvas_render_plan

    presentation = build_live_store_presentation(store)
    display_image1 = image1 or presentation.display_image1
    display_image2 = image2 or presentation.display_image2
    source_image1 = source_image1 or presentation.source_image1
    source_image2 = source_image2 or presentation.source_image2
    source_key = source_key or presentation.source_key
    display_cache_key = (
        display_cache_key
        if display_cache_key is not None
        else presentation.display_cache_key
    )

    # Crop boxes resolve through the canvas layer's single seam, exactly like
    # the upload/letterbox owner (``display_tier_service`` hooks). The Store's
    # pipeline slot does NOT carry the crop service in production (it holds the
    # reducer-owned pixel/preview/unify mirrors), so a plan built from the slot
    # alone kept the uncropped source aspect as its canvas basis while the
    # letterbox fitted the box — cropping black areas never refitted the
    # comparison (owner report 2026-11-06). ``resolve_slot_boxes`` is a per-frame
    # dict hit on a warmed service and kicks a warmup otherwise, exactly like the
    # upload path one frame earlier.
    try:
        from tabs.image_compare.canvas.texture_parts.crop_clip import resolve_slot_boxes

        crop_boxes = resolve_slot_boxes(canvas) if canvas is not None else None
    except Exception:
        crop_boxes = None
    plan = build_canvas_plan(
        store,
        display_image1,
        display_image2,
        source_image1=source_image1,
        source_image2=source_image2,
        source_key=source_key,
        display_cache_key=display_cache_key,
        preserve_zoom=True,
        crop_boxes=crop_boxes,
    )

    if getattr(plan, "envelope_hold", False):
        # One side's own source tier is still decoding and only its PREVIEW
        # (a 1024-long-edge size guess) is installed, so the pair envelope
        # cannot be stated without mixing that guess with the other side's real
        # dims — a frame describing neither pair (owner report 2026-10-27:
        # [1024,1400] between [1600,1400] frames). Hold the previously
        # presented frame and its overlays untouched; the settled plan follows
        # as soon as that side's own pixels land (the source key changes, so
        # the next frame is not skipped).
        from engine.cache.render_debug import rhi_render_debug

        rhi_render_debug(
            "hold frame: pair envelope is a preview guess on one side"
            " (display1=%s display2=%s); keeping the presented plan",
            _dims(display_image1),
            _dims(display_image2),
        )
        return

    apply_canvas_render_plan(
        canvas,
        plan,
        store=store,
        clip_overlays_to_image_bounds=clip_overlays_to_image_bounds,
    )


def _dims(image):
    """``(w,h)`` or ``None`` — cheap debug formatting, never raises."""
    try:
        if image is None:
            return None
        from shared.image_processing.image_dims import get_image_dims

        return tuple(get_image_dims(image))
    except Exception:
        return None
