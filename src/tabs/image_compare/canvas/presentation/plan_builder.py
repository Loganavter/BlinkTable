from __future__ import annotations

from PySide6.QtGui import QColor

from ui.canvas_infra.scene.frame_geometry import resolve_canvas_clip_rect_px
from ui.canvas_infra.scene.layout_requirements import resolve_feature_virtual_layout
from ui.canvas_infra.scene.property_access import (
    read_canvas_feature_color_by_setting_key,
    read_canvas_feature_setting_by_key,
)
from tabs.image_compare.canvas.registry import registry
from tabs.image_compare.canvas.scene import build_render_scene
from ui.canvas_presentation.plan import CanvasRenderPlan

from tabs.image_compare.canvas.presentation.geometry import (
    CanvasGeometry,
    _FallbackGuidesState,
    compute_canvas_plan,
)
from tabs.image_compare.canvas.presentation.live_presentation import (
    build_live_store_presentation,
)
from tabs.image_compare.canvas.presentation.snapshot_frame import (
    build_render_frame_presentation,
)
from tabs.image_compare.canvas.presentation.snapshot_store import (
    _build_snapshot_store,
    _get_unified_images,
    build_snapshot_store_presentation,
)

__all__ = [
    "CanvasGeometry",
    "build_canvas_plan",
    "build_live_store_presentation",
    "build_render_frame_presentation",
    "build_snapshot_store_presentation",
    "compute_canvas_plan",
    "_get_unified_images",
]

def _box_clipped_live_inputs(store, display_image1, display_image2, crop_boxes=None):
    """Box-clipped live display inputs, or ``None`` when no box is active.

    W3e: with full-frame stores the live plan must size from the box-clipped
    content (canvas, pads, clip rect), not the full frame — the envelope,
    uploads and Store geometry already fit the box. Resolves through the
    canvas layer's single box seam (``texture_parts.crop_clip``: document
    paths + per-image overrides + the session crop service); any failure — no
    service, unwarmed service (GUI-thread warmup kick instead of blocking), no
    box — returns ``None`` and the caller keeps today's full-frame behavior
    bit-identical. PIL/QImage inputs are clipped to views;
    ``TiledPixelStore`` stays lazy (tile residency crops) while sizes still
    follow the box. Slot 1 is the sizing basis (as today); without its box
    the whole helper stays ``None``. Sources/keys/plan math untouched.

    ``crop_boxes`` — ``(raw_box1, raw_box2)`` in full-source coords, already
    resolved by the caller through ``resolve_slot_boxes`` (the same seam the
    upload/letterbox owner uses). The LIVE caller MUST pass it: production
    ``PipelineCacheState`` carries the pixel/preview/unify mirrors only, so the
    legacy ``pipeline.crop_service`` lookup below is always ``None`` there —
    reading it alone left every cropped pair with the uncropped source aspect
    on the canvas basis (owner report: "cropping black areas does not
    recalculate the aspect ratio of the whole displayed comparison"). ``None``
    keeps the legacy slot lookup for test fakes that install the service
    there.
    """
    try:
        from shared.image_processing.image_dims import get_image_dims
        from tabs.image_compare.canvas.texture_parts.crop_clip import (
            box_dims,
            clip_image_for_upload,
            scaled_box_for_source,
        )
    except Exception:
        return None
    try:
        doc = store.get_session_state_slot("document")
        path1 = getattr(doc, "image1_path", None)
        path2 = getattr(doc, "image2_path", None)
    except Exception:
        doc, path1, path2 = None, None, None
    if crop_boxes is not None:
        try:
            raw1, raw2 = crop_boxes[0], crop_boxes[1]
        except Exception:
            raw1 = raw2 = None
    else:
        try:
            from tabs.image_compare.canvas.texture_parts.crop_clip import (
                resolve_box_for_path,
            )
            from tabs.image_compare.state.document import crop_override_for_path

            pipe = store.get_session_state_slot("pipeline")
            svc = getattr(pipe, "crop_service", None)
        except Exception:
            return None
        if svc is None:
            return None
        try:
            ov1 = crop_override_for_path(doc, path1)
            ov2 = crop_override_for_path(doc, path2)
        except Exception:
            ov1, ov2 = None, None
        try:
            raw1 = resolve_box_for_path(path1, svc, override=ov1)
        except Exception:
            raw1 = None
        try:
            raw2 = resolve_box_for_path(path2, svc, override=ov2)
        except Exception:
            raw2 = None
    try:
        live1 = get_image_dims(display_image1)
        box1 = scaled_box_for_source(raw1, path=path1, live_size=live1)
    except Exception:
        box1 = None
    if box1 is None:
        return None
    try:
        live2 = get_image_dims(display_image2)
        box2 = scaled_box_for_source(raw2, path=path2, live_size=live2)
    except Exception:
        box2 = None
    try:
        clipped1 = clip_image_for_upload(display_image1, box1)
        clipped2 = (
            clip_image_for_upload(display_image2, box2)
            if box2 is not None
            else display_image2
        )
    except Exception:
        return None
    w, h = box_dims(box1, live1)
    try:
        w, h = int(w), int(h)
    except Exception:
        return None
    if w <= 0 or h <= 0:
        return None
    return (clipped1, clipped2, w, h)

def build_canvas_plan(
    viewport_source,
    image1,
    image2,
    *,
    source_image1=None,
    source_image2=None,
    source_key=None,
    display_cache_key=None,
    target_size: tuple[int, int] | None = None,
    content_size: tuple[int, int] | None = None,
    output_scale: float = 1.0,
    fit_content: bool = False,
    preserve_zoom: bool = False,
    global_bounds=None,
    fill_color=None,
    pad_left: int = 0,
    pad_top: int = 0,
    render_scene=None,
    divider_thickness_px: int | None = None,
    guides_thickness: int | None = None,
    image_is_padded_composite: bool = False,
    geometry_letterbox: bool = False,
    crop_boxes=None,
) -> CanvasRenderPlan:
    if hasattr(viewport_source, "viewport"):
        store = viewport_source
        vp = store.viewport
        display_image1 = image1
        display_image2 = image2
        # The SOURCE role feeds the magnifier: an omitted source must resolve to
        # the side's OWN pixels, not to the display image — after unify the
        # display copy of the smaller side is an upscaled store, and the loupe
        # then magnifies that (measured 2026-10-26: both source grids registered
        # at the canvas size). See presentation/source_tier.py.
        from tabs.image_compare.canvas.presentation.source_tier import (
            is_preview_basis as _is_preview_basis,
        )
        from tabs.image_compare.canvas.presentation.source_tier import (
            source_role_image as _source_role_image,
        )

        source_image1 = (
            source_image1
            if source_image1 is not None
            else _source_role_image(store, 1, image1)
        )
        source_image2 = (
            source_image2
            if source_image2 is not None
            else _source_role_image(store, 2, image2)
        )
        source_key = source_key or ()
    else:
        store, display_image1, display_image2, source_image1, source_image2, source_key, display_cache_key = _build_snapshot_store(
            viewport_source,
            image1,
            image2,
            fit_content=fit_content,
            global_bounds=global_bounds,
            fill_color=fill_color,
        )
        vp = store.viewport
        # Padding is expressed via virtual layout / overlay_clip_rect, not by
        # baking pads into display pixels (see tile-rendering-system.md).
        image_is_padded_composite = bool(image_is_padded_composite)

    is_live_default_plan = (
        hasattr(viewport_source, "viewport")
        and target_size is None
        and render_scene is None
    )

    def _w(img):
        if img is None: return 1
        if hasattr(img, "width") and callable(img.width): return int(img.width())
        if hasattr(img, "size") and isinstance(img.size, (tuple, list)): return int(img.size[0])
        if hasattr(img, "size") and callable(img.size): return int(img.size().width())
        return int(img.width)

    def _h(img):
        if img is None: return 1
        if hasattr(img, "height") and callable(img.height): return int(img.height())
        if hasattr(img, "size") and isinstance(img.size, (tuple, list)): return int(img.size[1])
        if hasattr(img, "size") and callable(img.size): return int(img.size().height())
        return int(img.height)

    w1, h1 = _w(display_image1), _h(display_image1)
    envelope_dims: tuple[int, int, int, int] | None = None
    envelope_hold = False

    if is_live_default_plan and display_image1 is not None:
        # LIVE callers pass the already-resolved per-slot boxes (the same
        # ``resolve_slot_boxes`` seam the upload/letterbox owner uses) — the
        # Store's pipeline slot never carries the crop service in production.
        _boxed = _box_clipped_live_inputs(
            store, display_image1, display_image2, crop_boxes=crop_boxes
        )
        if _boxed is not None:
            display_image1, display_image2, w1, h1 = _boxed

    if is_live_default_plan and display_image1 is not None:
        # Sizing base = the comparison ENVELOPE (max(w1,w2) x max(h1,h2)), the
        # same reference the letterbox owner
        # (``update_common_letterbox_geometry``) fits both sides to. Slot 1
        # alone is not pair-invariant: after a swap inside the pair each side's
        # installed tier can differ (preview/preview while the order-sensitive
        # unify cache misses, then the unified pair), so a slot-1-only canvas
        # made every canvas-px overlay (loupe radius/spacing/offsets, capture
        # ring, clip rect) change with the swap and again when unify landed
        # (owner report 2026-10-27).
        #
        # The envelope is measured in SOURCE space — each side's own decoded
        # pixels (``source_tier.source_role_image``), never the installed
        # display tier: previews are normalised per side to a 1024 long edge, so
        # a preview pair's envelope has a different ASPECT than the source
        # pair's (2000x1400 = 1.43 vs 1024x1024 = 1.0) and the whole
        # composition re-fits the moment the unified store lands. The result is
        # carried on the plan (``envelope_dims``) so no downstream owner
        # re-derives it from whatever tier happens to be installed.
        #
        # Fallbacks keep today's numbers bit-identical: a box-clipped pair
        # (``_boxed`` — display dims are the truth under a crop box) and a
        # non-live plan (export/snapshot) which never enters this branch.
        #
        # A side whose own source tier is not decoded yet falls back to its
        # DISPLAY tier (``source_role_image``). When that display tier is a
        # preview, its dims are a 1024-long-edge guess: mixing them with the
        # other side's real dims yields an envelope that describes neither pair
        # (measured live 2026-10-27: [1024,1400] between [1600,1400] frames).
        # That frame is not presentable — mark it ``envelope_hold`` and leave
        # ``envelope_dims`` unset so no downstream owner re-derives the mix.
        # Both sides guessing is a preview PAIR (self-consistent, today's first
        # paint); only a one-sided guess is a mix.
        w2, h2 = _w(display_image2), _h(display_image2)
        _src1 = source_image1 if source_image1 is not None else display_image1
        _src2 = source_image2 if source_image2 is not None else display_image2
        _sw1, _sh1 = _w(_src1), _h(_src1)
        _sw2, _sh2 = _w(_src2), _h(_src2)
        _guess1 = _is_preview_basis(_src1)
        _guess2 = _is_preview_basis(_src2)
        _has_pair = _src1 is not None and _src2 is not None
        if _boxed is None and _has_pair and _guess1 != _guess2:
            envelope_hold = True
            base_w = max(1, int(w1), int(w2))
            base_h = max(1, int(h1), int(h2))
        elif (
            _boxed is None
            and _sw1 > 1
            and _sh1 > 1
            and _sw2 > 1
            and _sh2 > 1
        ):
            envelope_dims = (_sw1, _sh1, _sw2, _sh2)
            base_w = max(1, _sw1, _sw2)
            base_h = max(1, _sh1, _sh2)
        else:
            base_w = max(1, int(w1), int(w2))
            base_h = max(1, int(h1), int(h2))
        live_virtual_layout = resolve_feature_virtual_layout(
            store,
            drawing_width=base_w,
            drawing_height=base_h,
        )
        live_pad_left, live_pad_right, live_pad_top, live_pad_bottom = (
            live_virtual_layout.resolve_padding_pixels(
                base_width=base_w,
                base_height=base_h,
            )
            if live_virtual_layout is not None
            else (0, 0, 0, 0)
        )
        pad_left = live_pad_left
        pad_top = live_pad_top
        target_size = (
            base_w + live_pad_left + live_pad_right,
            base_h + live_pad_top + live_pad_bottom,
        )
        store.runtime_cache.overlay_clip_rect = resolve_canvas_clip_rect_px(
            live_virtual_layout,
            base_width=base_w,
            base_height=base_h,
        )

    canvas_w = target_size[0] if target_size is not None else w1
    canvas_h = target_size[1] if target_size is not None else h1
    content_w = content_size[0] if content_size is not None else canvas_w
    content_h = content_size[1] if content_size is not None else canvas_h
    view = vp.view_state
    capture_visible = bool(read_canvas_feature_setting_by_key("image_compare", vp, "capture.visible"))
    capture_color = read_canvas_feature_color_by_setting_key("image_compare", vp, "capture.color")
    _get_guides_state = registry().get_feature_command_by_alias("guides.widget_state")
    guides_state = _get_guides_state(view) if _get_guides_state is not None else _FallbackGuidesState()

    divider_px = (
        int(divider_thickness_px)
        if divider_thickness_px is not None
        else int(
            (
                registry().get_feature_command_by_alias("overlay.active_divider_thickness")
                or (lambda _store: 0)
            )(store)
        )
    )
    guides_px = int(guides_thickness) if guides_thickness is not None else int(guides_state.thickness)

    if render_scene is None:
        render_scene = build_render_scene(
            store,
            apply_channel_mode_in_shader=True,
            clip_overlays_to_image_bounds=False,
        )

    overlay_layout = None
    overlay_enabled_query = registry().get_feature_command_by_alias("overlay.enabled")
    build_overlay_layout = registry().get_feature_command_by_alias(
        "overlay.render_build_layout"
    )
    if (
        overlay_enabled_query is not None
        and build_overlay_layout is not None
        and overlay_enabled_query(store)
    ):
        overlay_layout = build_overlay_layout(
            vp,
            width=max(1, int(content_w)),
            height=max(1, int(content_h)),
            canvas_width=canvas_w,
            canvas_height=canvas_h,
            content_offset_x=float(pad_left),
            content_offset_y=float(pad_top),
            divider_thickness_px=divider_px,
        )

    return CanvasRenderPlan(
        image1=display_image1,
        image2=display_image2,
        source_image1=source_image1,
        source_image2=source_image2,
        source_key=source_key,
        display_cache_key=display_cache_key,
        canvas_w=canvas_w,
        canvas_h=canvas_h,
        render_scene=render_scene,
        overlay_layout=overlay_layout,
        capture_visible=capture_visible,
        capture_color=QColor(
            capture_color.r,
            capture_color.g,
            capture_color.b,
            capture_color.a,
        ),
        guides_enabled=bool(guides_state.enabled),
        guides_color=QColor(
            guides_state.color.r,
            guides_state.color.g,
            guides_state.color.b,
            guides_state.color.a,
        ),
        guides_thickness=guides_px,
        fill_rgba=(
            None
            if fill_color is None
            else (
                int(fill_color[0]),
                int(fill_color[1]),
                int(fill_color[2]),
                int(fill_color[3]),
            )
            if len(fill_color) >= 4
            else None
        ),
        output_scale=float(output_scale or 1.0),
        preserve_zoom=preserve_zoom,
        image_is_padded_composite=bool(image_is_padded_composite),
        geometry_letterbox=bool(geometry_letterbox),
        envelope_dims=envelope_dims,
        envelope_hold=envelope_hold,
    )

