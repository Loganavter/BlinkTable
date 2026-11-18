# Audit-Meta: pattern=state-machine reason="single render runtime context builder for RHI"
from __future__ import annotations

import logging
from dataclasses import dataclass
from core.log_names import APP_LOGGER_NAME

logger = logging.getLogger(APP_LOGGER_NAME)
from PySide6.QtCore import QTimer

from tabs.image_compare.canvas.registry import registry
from ui.canvas_infra.viewport.state import (
    get_pan_offset_x,
    get_pan_offset_y,
    get_zoom_level,
)
from ui.canvas_presentation.render_arch import (
    RenderIntent,
    build_render_intent,
)
from tabs.image_compare.canvas.render_arch import (
    BaseImagePrimitive,
    RenderList,
    ResolvedCanvasStyle,
    SceneFrame,
    build_render_list,
    build_scene_frame,
    resolve_canvas_style,
)

from .render_config import update_display_split_position
from ui.canvas_infra.rhi.render_metrics import RenderMetrics
from ui.canvas_infra.rhi.render_mode import EXPORT_CANVAS_OBJECT_NAME
from engine.cache.render_debug import rhi_render_debug
from engine.gpu.rt_y_orientation import composited_clip_correction
from .texture_parts.base_images import (
    update_common_letterbox_geometry,
    upload_pil_images,
    upload_source_pil_image,
)


@dataclass(slots=True)
class ViewportContext:
    width: int
    height: int
    zoom_level: float
    pan_offset_x: float
    pan_offset_y: float
    is_horizontal: bool
    split_position: float
    # Logical canvas size/offset overlay passes should position against,
    # distinct from width/height (the actual render target). Equal to
    # width/height/0/0 outside tiled export; during tiled export the render
    # target is one tile's worth of pixels but overlays (divider, guides,
    # capture-circle, filename) must still compute their position as if
    # drawing onto the full canvas, then get shifted into tile-local space
    # by canvas_offset_x/y. See widget.runtime_state._export_canvas_viewport.
    canvas_width: int
    canvas_height: int
    canvas_offset_x: float
    canvas_offset_y: float
    # The frame's target units, resolved once per frame by
    # ``resolve_target_units`` below (the same two fields multi_compare's
    # scene context carries -- ``multi_compare/scene/context.py``). They are
    # what a *composite* pass cannot guess from ``width/height``:
    #
    # * ``clip_matrix`` -- the composited clip-space correction for the RHI.
    #   ``None`` is a legal state, not a missing value: it means "the widget
    #   has no RHI yet" (before mount), and an identity matrix is only correct
    #   on a y-up NDC backend -- which cannot be known without the RHI. A pass
    #   that needs the correction keeps owning its own identity fallback for
    #   that one case (`drag_drop_overlay/framing.py`), never staging identity
    #   while a backend is live (that was #751: a mirrored drop zone on Vulkan).
    # * ``framebuffer_size`` -- the render target's size in DEVICE pixels.
    #   ``width/height`` are logical; a viewport sized from them draws a
    #   device-px raster into 1/DPR of the framebuffer per axis (a quarter of
    #   it at DPR=2). ``None`` only when even the DPR fallback is unavailable.
    clip_matrix: tuple[float, ...] | None = None
    framebuffer_size: tuple[float, float] | None = None


@dataclass(slots=True)
class TextureContext:
    stored_pil_images: list
    source_texture_ids: list[object]
    texture_ids: list[object]
    source_images_ready: bool
    diff_source_ready: bool
    diff_source_texture_id: object
    shader_letterbox_mode: bool
    content_rect_px: tuple[int, int, int, int] | None
    clip_overlays_to_content_rect: bool
    images_uploaded: list[bool]


@dataclass(slots=True)
class FeatureOverlayContext:
    widget: object
    render_enabled: bool
    clip_to_content: bool
    border_color: object | None
    border_width: float
    quads: tuple[object, ...]
    gpu_active: bool
    gpu_slots: tuple[object, ...]
    gpu_channel_mode: int
    gpu_diff_mode: int
    gpu_diff_threshold: float
    gpu_interp_mode: int
    capture_circles: tuple[object, ...]
    occluded_capture_arcs: tuple[object, ...]
    hidden_capture_circles: tuple[object, ...]
    hidden_overlay_circles: tuple[object, ...]


@dataclass(slots=True)
class MetricsContext:
    render_metrics: RenderMetrics
    render_intent: RenderIntent
    resolved_style: ResolvedCanvasStyle
    scene_frame: SceneFrame
    render_list: RenderList


@dataclass(slots=True)
class RenderRuntimeContext:
    viewport: ViewportContext
    textures: TextureContext
    feature_overlay: FeatureOverlayContext
    metrics: MetricsContext

    def __getattr__(self, name):
        for section in (
            self.viewport,
            self.textures,
            self.feature_overlay,
            self.metrics,
        ):
            if hasattr(section, name):
                return getattr(section, name)
        raise AttributeError(name)


def resolve_target_units(
    widget,
) -> tuple[tuple[float, ...] | None, tuple[float, float] | None]:
    """``(clip_matrix, framebuffer_size)`` for this frame's render target.

    The frame's device units, resolved ONCE per frame so no composite pass has
    to invent a local fallback -- the class behind two 2026-10-27/28 defects:
    a pass staging identity instead of the backend's clip correction (#751, a
    mirrored drop zone on a y-down NDC backend) and a viewport computed from
    logical ``width/height`` while the raster was device px (a quarter of the
    frame at DPR=2).

    ``clip_matrix``
        ``composited_clip_correction(rhi)`` -- the pass writes into the
        widget's own render target, which the ``QRhiWidget`` composites on
        screen, so this is ``rhi.clipSpaceCorrMatrix()`` verbatim (same source
        as ``engine/overlay/shell.py`` and the guides/capture/divider/
        filename passes; see ``engine/gpu/rt_y_orientation.py``). ``None``
        when the widget has no RHI yet (before mount) -- a legal state, never
        an identity matrix: identity is correct only on a y-up NDC backend,
        which cannot be decided without the RHI, so a pass that needs a matrix
        keeps its own identity fallback for the pre-mount case.

    ``framebuffer_size``
        The render target's own ``pixelSize()`` in DEVICE px -- the truth for
        a ``QRhiWidget``, and what a viewport/raster must both use. Falls back
        to logical size x ``devicePixelRatioF()`` when the host exposes no
        target (headless/stub hosts, and the pre-mount frame).

    Never raises: an absent or failing ``rhi()`` / ``renderTarget()`` degrades
    to the fallbacks above and is logged at DEBUG. This runs per frame, so a
    real failure is one debug line, not a crashed canvas.
    """
    clip_matrix: tuple[float, ...] | None = None
    framebuffer_size: tuple[float, float] | None = None

    try:
        rhi = widget.rhi()
    except (AttributeError, RuntimeError) as exc:
        rhi = None
        logger.debug("[ic-ctx] widget has no queryable rhi(): %s", exc)
    if rhi is not None:
        try:
            clip_matrix = composited_clip_correction(rhi)
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            clip_matrix = None
            logger.debug("[ic-ctx] rhi cannot answer clipSpaceCorrMatrix(): %s", exc)

    target = None
    try:
        target = widget.renderTarget()
    except (AttributeError, RuntimeError) as exc:
        logger.debug("[ic-ctx] widget has no renderTarget(): %s", exc)
    if target is not None:
        try:
            size = target.pixelSize()
            width_px, height_px = int(size.width()), int(size.height())
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            width_px = height_px = 0
            logger.debug(
                "[ic-ctx] renderTarget().pixelSize() is unusable (%s): %r",
                exc,
                target,
            )
        if width_px > 0 and height_px > 0:
            framebuffer_size = (float(width_px), float(height_px))

    if framebuffer_size is None:
        try:
            dpr = max(1.0, float(widget.devicePixelRatioF()))
        except (AttributeError, RuntimeError, TypeError, ValueError):
            dpr = 1.0
        try:
            width, height = int(widget.width()), int(widget.height())
        except (AttributeError, RuntimeError, TypeError, ValueError):
            width = height = 0
        framebuffer_size = (
            float(max(1, round(width * dpr))),
            float(max(1, round(height * dpr))),
        )
        logger.debug(
            "[ic-ctx] no render target for the frame; framebuffer_size falls "
            "back to logical x dpr = %r",
            framebuffer_size,
        )

    return clip_matrix, framebuffer_size


def build_render_runtime_context(widget) -> RenderRuntimeContext:
    state = widget.runtime_state
    store = getattr(state, "_store", None)
    viewport = getattr(store, "viewport", None) if store is not None else None
    view_state = getattr(viewport, "view_state", None) if viewport is not None else None
    plan = getattr(widget, "_active_render_plan", None)
    object_name = str(getattr(widget, "objectName", lambda: "")() or "")
    read_only = bool(getattr(state, "_read_only", False))
    mode = (
        "export"
        if object_name == EXPORT_CANVAS_OBJECT_NAME
        else "preview" if read_only or store is None else "interactive"
    )
    inner_rect = getattr(state, "_inner_content_rect_px", None)
    if inner_rect is not None:
        content_rect_px = (
            int(inner_rect[0]),
            int(inner_rect[1]),
            int(inner_rect[2]),
            int(inner_rect[3]),
        )
        split_override = getattr(state, "_inner_split_position", None)
    else:
        content_rect = state._content_rect_px or (0, 0, widget.width(), widget.height())
        content_rect_px = (
            int(content_rect[0]),
            int(content_rect[1]),
            int(content_rect[2]),
            int(content_rect[3]),
        )
        split_override = None
    render_metrics = RenderMetrics(
        canvas_to_view=float(getattr(state, "_content_sr", 1.0) or 1.0),
        view_to_screen=float(get_zoom_level(widget) or 1.0),
        output_scale=float(getattr(plan, "output_scale", 1.0) or 1.0),
        content_width=float(max(0, content_rect_px[2])),
        content_height=float(max(0, content_rect_px[3])),
        mode=mode,
    )
    render_intent = build_render_intent(
        kind=mode,
        output_width=widget.width(),
        output_height=widget.height(),
        output_scale=float(getattr(plan, "output_scale", 1.0) or 1.0),
        zoom_level=float(get_zoom_level(widget) or 1.0),
        clip_overlays_to_content=bool(
            getattr(state, "_clip_overlays_to_content_rect", False)
        ),
        preserve_zoom=bool(getattr(plan, "preserve_zoom", False)),
    )

    export_canvas_viewport = getattr(state, "_export_canvas_viewport", None)
    if export_canvas_viewport is not None:
        canvas_width, canvas_height, canvas_offset_x, canvas_offset_y = (
            export_canvas_viewport
        )
    else:
        canvas_width, canvas_height = widget.width(), widget.height()
        canvas_offset_x, canvas_offset_y = 0.0, 0.0

    render_scene = state._render_scene or {}
    static_fo = dict(getattr(render_scene, "feature_overrides", {}) or {})
    dynamic_fo = dict(getattr(state, "_dynamic_feature_overrides", {}) or {})
    feature_payloads = {**static_fo, **dynamic_fo}

    scene_frame = build_scene_frame(
        render_scene=render_scene,
        content_rect_px=content_rect_px,
        image_rect_px=content_rect_px,
        split_override=split_override,
        feature_payloads=feature_payloads,
    )
    resolved_style = resolve_canvas_style(scene_frame, render_metrics)
    # Side effect: refresh widget display_split for DividerPass (white line).
    update_display_split_position(
        widget,
        scene=scene_frame,
        zoom_level=float(get_zoom_level(widget) or 1.0),
        pan_offset_x=float(get_pan_offset_x(widget) or 0.0),
        pan_offset_y=float(get_pan_offset_y(widget) or 0.0),
        anchor_to_viewport=(mode == "interactive"),
    )
    diff_mode_active = bool(scene_frame.diff_mode_active)
    use_hires = bool(
        state._shader_letterbox_mode
        and not diff_mode_active
        and float(get_zoom_level(widget) or 1.0) > 1.0
        and state._source_images_ready
        and getattr(widget, "_source_texture_ids", [0, 0])[0]
        and getattr(widget, "_source_texture_ids", [0, 0])[1]
    )
    letterbox1 = (
        widget.get_letterbox_params(0)
        if state._shader_letterbox_mode and hasattr(widget, "get_letterbox_params")
        else (0.0, 0.0, 1.0, 1.0)
    )
    letterbox2 = (
        widget.get_letterbox_params(1)
        if state._shader_letterbox_mode and hasattr(widget, "get_letterbox_params")
        else (0.0, 0.0, 1.0, 1.0)
    )
    canvas_letterbox = getattr(state, "_canvas_frame_letterbox", None) or (
        0.0,
        0.0,
        0.0,
        0.0,
    )
    fill = getattr(state, "_letterbox_fill_rgba", None)
    letterbox_fill = (
        (
            float(fill[0]) / 255.0,
            float(fill[1]) / 255.0,
            float(fill[2]) / 255.0,
            float(fill[3]) / 255.0,
        )
        if fill is not None and len(fill) >= 4 and float(fill[3]) > 0
        else (0.0, 0.0, 0.0, 0.0)
    )
    rhi_render_debug(
        "letterbox_uniforms widget=%dx%d shader_mode=%s letterbox1=%r letterbox2=%r "
        "canvas_letterbox=%r content_rect_px=%r",
        widget.width(),
        widget.height(),
        bool(state._shader_letterbox_mode),
        letterbox1,
        letterbox2,
        canvas_letterbox,
        getattr(state, "_content_rect_px", None),
    )
    # Base shader compares spit in letterboxed image UV (magnifier parity).
    # DividerPass still reads display_split_position for the white line.
    content_split = float(scene_frame.split_position_visual)
    if getattr(scene_frame, "split_override", None) is not None:
        content_split = float(scene_frame.split_override)
    content_split = max(0.0, min(1.0, content_split))
    base_image = BaseImagePrimitive(
        split_position=content_split,
        is_horizontal=bool(scene_frame.is_horizontal),
        zoom=float(get_zoom_level(widget) or 1.0),
        pan_offset_x=float(get_pan_offset_x(widget) or 0.0),
        pan_offset_y=float(get_pan_offset_y(widget) or 0.0),
        diff_mode_int=int(scene_frame.diff_mode_int or 0),
        channel_mode_int=int(scene_frame.channel_mode_int or 0),
        use_hires=use_hires,
        letterbox1=tuple(float(v) for v in letterbox1),
        letterbox2=tuple(float(v) for v in letterbox2),
        canvas_letterbox=tuple(float(v) for v in canvas_letterbox),
        letterbox_fill=letterbox_fill,
    )
    overlay_state = state._feature_overlay_gpu
    render_list = build_render_list(
        scene_frame,
        base_image=base_image,
    )
    # The frame's device units, resolved once here so passes read them from the
    # context instead of each re-deriving a fallback (see
    # ``resolve_target_units``).
    clip_matrix, framebuffer_size = resolve_target_units(widget)
    return RenderRuntimeContext(
        viewport=ViewportContext(
            width=widget.width(),
            height=widget.height(),
            zoom_level=get_zoom_level(widget),
            pan_offset_x=get_pan_offset_x(widget),
            pan_offset_y=get_pan_offset_y(widget),
            is_horizontal=bool(
                getattr(
                    view_state, "is_horizontal", getattr(widget, "is_horizontal", False)
                )
            ),
            split_position=float(
                getattr(
                    view_state, "split_position", getattr(widget, "split_position", 0.5)
                )
                or 0.5
            ),
            canvas_width=int(canvas_width),
            canvas_height=int(canvas_height),
            canvas_offset_x=float(canvas_offset_x),
            canvas_offset_y=float(canvas_offset_y),
            clip_matrix=clip_matrix,
            framebuffer_size=framebuffer_size,
        ),
        textures=TextureContext(
            stored_pil_images=list(state._stored_pil_images),
            source_texture_ids=list(getattr(widget, "_source_texture_ids", [0, 0])),
            texture_ids=list(getattr(widget, "texture_ids", [0, 0])),
            source_images_ready=bool(state._source_images_ready),
            diff_source_ready=bool(state._diff_source_ready),
            diff_source_texture_id=getattr(widget, "_diff_source_texture_id", 0) or 0,
            shader_letterbox_mode=bool(state._shader_letterbox_mode),
            content_rect_px=state._content_rect_px,
            clip_overlays_to_content_rect=bool(state._clip_overlays_to_content_rect),
            images_uploaded=list(state._images_uploaded),
        ),
        feature_overlay=FeatureOverlayContext(
            widget=widget,
            render_enabled=bool(getattr(overlay_state, "_quads", ())),
            clip_to_content=bool(
                getattr(state, "_clip_overlays_to_content_rect", False)
            ),
            border_color=getattr(overlay_state, "_border_color", None),
            border_width=float(getattr(overlay_state, "_border_width", 2.0) or 2.0),
            quads=tuple(getattr(overlay_state, "_quads", ()) or ()),
            gpu_active=bool(getattr(overlay_state, "_gpu_active", False)),
            gpu_slots=tuple(getattr(overlay_state, "_gpu_slots", ()) or ()),
            gpu_channel_mode=int(getattr(overlay_state, "_gpu_channel_mode", 0) or 0),
            gpu_diff_mode=int(getattr(overlay_state, "_gpu_diff_mode", 0) or 0),
            gpu_diff_threshold=float(
                getattr(overlay_state, "_gpu_diff_threshold", 20.0 / 255.0) or 0.0
            ),
            gpu_interp_mode=(
                int(getattr(overlay_state, "_gpu_interp_mode"))
                if getattr(overlay_state, "_gpu_interp_mode", None) is not None
                else 1
            ),
            capture_circles=tuple(getattr(state, "_capture_circles", ()) or ()),
            occluded_capture_arcs=tuple(
                getattr(state, "_occluded_capture_arcs", ()) or ()
            ),
            hidden_capture_circles=tuple(
                getattr(state, "_hidden_capture_circles", ()) or ()
            ),
            hidden_overlay_circles=tuple(
                getattr(state, "_hidden_overlay_circles", ()) or ()
            ),
        ),
        metrics=MetricsContext(
            render_metrics=render_metrics,
            render_intent=render_intent,
            resolved_style=resolved_style,
            scene_frame=scene_frame,
            render_list=render_list,
        ),
    )


def resize_canvas(widget, w: int, h: int):
    state = widget.runtime_state
    store = getattr(state, "_store", None) or getattr(widget, "_store", None)
    interaction = getattr(getattr(store, "viewport", None), "interaction_state", None)
    resizing = bool(
        interaction is not None and getattr(interaction, "resize_in_progress", False)
    )

    letterbox_focus = None
    if state._shader_letterbox_mode and state._stored_pil_images[0] is not None:
        from ui.canvas_infra.viewport.focus import capture_letterbox_focus

        letterbox_focus = capture_letterbox_focus(widget)
    img1, img2 = state._stored_pil_images
    # The active plan's SOURCE-space envelope (crop-aware: the plan carries the
    # box-clipped dims under a crop) — fitting the stored previews instead made
    # a resize re-derive the UNCROPPED aspect, and the divider then drew against
    # a different ruler than the image (owner report 2026-11-18).
    _resize_envelope = getattr(
        getattr(widget, "_active_render_plan", None), "envelope_dims", None
    )
    if state._shader_letterbox_mode and img1 is not None:
        update_common_letterbox_geometry(
            widget, img1, img2, envelope_dims=_resize_envelope
        )
        from ui.canvas_infra.viewport.focus import restore_letterbox_focus

        restore_letterbox_focus(widget, letterbox_focus)
    elif img1 is not None:
        if resizing:
            # Live redraw with current textures; defer PIL letterbox rebuild.
            sync_runtime_overlays_after_resize(widget)
            widget.update()
            return
        _schedule_pil_letterbox_refresh(widget)
        sync_runtime_overlays_after_resize(widget)
        widget.update()
        return
    if state._shader_letterbox_mode and img2 is not None:
        update_common_letterbox_geometry(
            widget, img1, img2, envelope_dims=_resize_envelope
        )
    elif img2 is not None:
        state._letterbox_params[1] = (0.0, 0.0, 1.0, 1.0)
    sync_runtime_overlays_after_resize(widget)
    widget.update()


def sync_runtime_overlays_after_resize(widget) -> None:
    state = widget.runtime_state
    if getattr(state, "_resize_overlay_sync_active", False):
        return
    state._resize_overlay_sync_active = True
    try:
        store = getattr(state, "_store", None)
        if store is None:
            store = getattr(widget, "_store", None)
        plan = getattr(widget, "_active_render_plan", None)
        if not state._shader_letterbox_mode or (store is None and plan is None):
            return

        if plan is not None:
            from ui.canvas_presentation.plan_applicator import (
                apply_plan_runtime_overlays,
            )

            apply_plan_runtime_overlays(widget, plan)

        if store is not None:
            from ui.canvas_presentation.plan_applicator import _sync_geometry_state

            _sync_geometry_state(widget, store)
            _live_plan = getattr(widget, "_active_render_plan", None)
            if _live_plan is None or _live_plan.overlay_layout is None:
                registry().apply_feature_live_runtime_overlays(store, widget)
    finally:
        state._resize_overlay_sync_active = False


def _schedule_pil_letterbox_refresh(widget):
    state = widget.runtime_state
    if getattr(state, "_pil_letterbox_refresh_scheduled", False):
        return
    state._pil_letterbox_refresh_scheduled = True

    def _do_refresh():
        state._pil_letterbox_refresh_scheduled = False
        img1, img2 = state._stored_pil_images
        if img1 is None:
            return
        upload_pil_images(
            widget,
            img1,
            img2,
            source_image1=None,
            source_image2=None,
            source_key=None,
            shader_letterbox=False,
        )

    # P4: one-shot deferred letterbox refresh (120 ms) — attributed.
    try:
        from tabs.image_compare.canvas.loupe_trace import (
            frame_context_from_widget as _fcw,
        )
        from tabs.image_compare.canvas.loupe_trace import (
            trace_invalidation as _trace_inv,
        )

        _trace_inv(
            _fcw(widget),
            "pil_letterbox",
            "PIL letterbox refresh deferred 120ms",
        )
    except Exception:
        pass
    QTimer.singleShot(120, _do_refresh)


def request_update(widget):
    state = widget.runtime_state
    if state._update_batch_depth > 0:
        state._update_pending = True
        return
    # Perf spans (Wave C, plan-engine-perf-debugger-2026-09-20): the
    # enqueue hop of the wheel-to-photon chain (span/record only — the
    # update() call below is unchanged).
    try:
        from contextlib import nullcontext as _nullcontext

        from tabs.image_compare.canvas.loupe_trace import (
            frame_context_from_widget as _fcw,
        )

        _fc = _fcw(widget)
        _enqueue_cm = (
            _fc.span("ic.enqueue", detail=lambda: {"source": "request_update"})
            if _fc is not None
            else _nullcontext()
        )
    except Exception:
        from contextlib import nullcontext as _nullcontext

        _enqueue_cm = _nullcontext()
    with _enqueue_cm:
        widget.update()
    _schedule_glass_settle_frame(widget)


def _schedule_glass_settle_frame(widget):
    """One extra repaint, one event-loop turn after this real content
    update -- lets the CPU-readback display path
    (ui/widgets/glass_hud/panel_display.py's GlassPanelDisplayWidget, see
    docs/dev/KNOWN_BUGS.md's "QRhiWidget/QOpenGLWidget can't alpha-blend
    against sibling widgets" entry) catch up to the composite this frame's
    own render_backdrops() call just produced.

    shared.rendering.glass_panel's render_backdrops() now runs *after*
    this frame's own main pass (moved there -- see that module's own
    "Timing" docstring section for why the earlier "before the main pass,
    reads the *previous* frame's colorTexture()" version wasn't actually
    load-bearing), so its composite already reflects this exact frame's
    content. render_backdrops() also now collects every panel's
    composite_tex readback *synchronously* (a single `rhi.finish()` right
    after issuing them, then reading `.data` immediately -- see that
    module's render_backdrops() docstring) instead of polling it on the
    next call, so `ready_images` is fully up to date by the time
    render_backdrops() itself returns -- no more colorTexture-staleness or
    readback-collection gap to catch up to.

    What this settle frame still covers: GlassPanelDisplayWidget is a
    *sibling* widget, not the RHI canvas itself, so it only repaints when
    something schedules its own `update()` -- render_backdrops() finishing
    with fresh data doesn't by itself trigger that. This one extra
    QTimer.singleShot(0, ...) repaint is what makes that catch-up paint
    happen shortly after a real content update, without stacking one per
    update() during a fast burst (see the single pending-flag note below).

    Without any settle frame at all, the backdrop is stuck stale as soon
    as the canvas goes idle: render_backdrops() only runs as part of the
    canvas's own repaint, and with no continuous render loop (this canvas
    is purely on-demand -- see
    docs/dev/rendering/investigations/glass-panel-backdrop-self-reference.md),
    nothing repaints again after the last real content change on its own.

    A single pending flag (not re-armed on every request_update()) so a
    burst of updates during active interaction doesn't stack up N settle
    frames -- just one, shortly after the burst ends. No-op if no glass
    panel is currently registered (nothing to catch up for).
    """
    glass_panels = getattr(widget, "glass_panels", None)
    if not glass_panels or not glass_panels.items():
        return
    state = widget.runtime_state
    if getattr(state, "_glass_settle_pending", False):
        return
    state._glass_settle_pending = True

    def _settle():
        state._glass_settle_pending = False
        widget.update()

    # P4: one-shot glass catch-up repaint — attributed (single pending
    # flag: bursts coalesce to one settle frame, never N).
    try:
        from tabs.image_compare.canvas.loupe_trace import (
            frame_context_from_widget as _fcw,
        )
        from tabs.image_compare.canvas.loupe_trace import (
            trace_invalidation as _trace_inv,
        )

        _trace_inv(
            _fcw(widget),
            "glass_settle",
            "glass catch-up repaint deferred one event-loop turn",
        )
    except Exception:
        pass
    QTimer.singleShot(0, _settle)


def emit_viewport_state_change(widget):
    state = widget.runtime_state
    store = state._store
    if store is None:
        store = getattr(widget, "_store", None)
    if store is not None and hasattr(store, "emit_state_change"):
        store.emit_viewport_change("geometry")

    plan = getattr(widget, "_active_render_plan", None)
    if not state._shader_letterbox_mode or (store is None and plan is None):
        return

    if plan is not None:

        from ui.canvas_presentation.plan_applicator import apply_plan_runtime_overlays

        apply_plan_runtime_overlays(widget, plan)

    if store is not None:

        from ui.canvas_presentation.plan_applicator import _sync_geometry_state

        _sync_geometry_state(widget, store)
        _live_plan = getattr(widget, "_active_render_plan", None)
        if _live_plan is None or _live_plan.overlay_layout is None:
            registry().apply_feature_live_runtime_overlays(store, widget)


def schedule_source_preload(widget):
    state = widget.runtime_state
    if state._source_preload_scheduled:
        return
    state._source_preload_scheduled = True
    # P4: one-shot source preload — attributed (dedup flag: at most one
    # deferral outstanding, never a polling loop).
    try:
        from tabs.image_compare.canvas.loupe_trace import (
            frame_context_from_widget as _fcw,
        )
        from tabs.image_compare.canvas.loupe_trace import (
            trace_invalidation as _trace_inv,
        )

        _trace_inv(
            _fcw(widget),
            "source_preload",
            "source texture preload deferred one event-loop turn",
        )
    except Exception:
        pass
    QTimer.singleShot(0, lambda: preload_source_textures(widget))


def preload_source_textures(widget):
    state = widget.runtime_state
    state._source_preload_scheduled = False
    img1, img2 = state._source_pil_images
    if not img1 and not img2:
        state._source_images_ready = False
        return

    try:
        if img1 is not None:
            upload_source_pil_image(widget, img1, 0)
        if img2 is not None:
            upload_source_pil_image(widget, img2, 1)

        state._source_images_ready = True
        if (
            state._store is not None
            and hasattr(state._store, "emit_state_change")
            and registry().has_feature_live_runtime_overlays()
        ):
            state._store.emit_viewport_change("geometry")
    except Exception:
        state._source_images_ready = False
    request_update(widget)


GLViewportContext = ViewportContext
GLTextureContext = TextureContext
GLFeatureOverlayContext = FeatureOverlayContext
GLMetricsContext = MetricsContext
GLRenderRuntimeContext = RenderRuntimeContext
resize_gl = resize_canvas


def begin_update_batch(widget):
    widget.runtime_state._update_batch_depth += 1


def end_update_batch(widget):
    state = widget.runtime_state
    if state._update_batch_depth <= 0:
        state._update_batch_depth = 0
        return
    state._update_batch_depth -= 1
    if state._update_batch_depth == 0 and state._update_pending:
        state._update_pending = False
        widget.update()
