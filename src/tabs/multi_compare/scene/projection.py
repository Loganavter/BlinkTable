"""Projection helpers for Multi Compare rendering.

This is the single source of truth for converting composition canvas
coordinates into framebuffer-space rectangles / UV letterbox uniforms.
Slot images are drawn on a shared fullscreen quad; letterbox and slot
cells live in the fragment shader (same idea as image_compare UV letterbox).
"""

from __future__ import annotations

from tabs.multi_compare.scene.context import (
    MultiCompareRenderContext,
    ProjectedLayer,
)


def letterbox_uv(
    *,
    framebuffer_size: tuple[float, float],
    scale: float,
    offset: tuple[float, float],
    canvas_size: tuple[float, float],
) -> tuple[float, float, float, float]:
    """Composition letterbox as framebuffer UV ``(x, y, w, h)``."""

    fb_w, fb_h = framebuffer_size
    if fb_w <= 0.0 or fb_h <= 0.0:
        return (0.0, 0.0, 1.0, 1.0)
    ox, oy = offset
    canvas_w, canvas_h = canvas_size
    return (
        float(ox) / fb_w,
        float(oy) / fb_h,
        max(1e-6, float(canvas_w) * float(scale) / fb_w),
        max(1e-6, float(canvas_h) * float(scale) / fb_h),
    )


def composition_frame_geometry(
    *,
    canvas_size: tuple[float, float],
    frame_size: tuple[float, float],
    tile_origin: tuple[float, float] = (0.0, 0.0),
    scene_zoom: float = 1.0,
    scene_pan: tuple[float, float] = (0.0, 0.0),
) -> tuple[float, float, float]:
    """``(scale, ox, oy)`` of the composition inside one render frame.

    UNITS: every argument and the result are **framebuffer pixels** — the
    space ``MultiCompareRhiRenderer`` feeds as ``framebuffer_size``
    (``target.pixelSize()``), which is ``widget logical size × DPR``. That is
    deliberately the only space this projection knows about:

    * live render — the frame is the widget's framebuffer, canvas-px geometry
      is scaled into it by ``min(fb/canvas)``;
    * export — ``TiledFramebufferExporter(fit_framebuffer_to_target=True)``
      makes one framebuffer px one *output* px, so ``frame_size`` is the whole
      output canvas in framebuffer px and ``tile_origin`` is the tile's
      framebuffer-px offset inside it.

    Mixing a logical (``QWidget.resize``) number into this for one and a
    device number for another is precisely the units defect this helper exists
    to prevent: the composition then covers only ``1/DPR²`` of the frame.

    ``scene_zoom``/``scene_pan`` are the whole-composition camera (the same
    convention as the content camera, one level up: ``screen_uv =
    (canvas_uv - 0.5 + pan) * zoom + 0.5``). Folding them into ``scale`` and
    ``ox/oy`` keeps every consumer — base-image letterbox, dividers, labels,
    hit-testing — reading one geometry, so cursor and pixels cannot desync.
    Identity ``(1.0, (0.0, 0.0))`` reproduces the plain letterbox exactly.
    """
    canvas_w, canvas_h = (max(1.0, float(v)) for v in canvas_size)
    frame_w, frame_h = float(frame_size[0]), float(frame_size[1])
    zoom = float(scene_zoom)
    if not (zoom > 0.0) or zoom != zoom:  # non-finite / non-positive → identity
        zoom = 1.0
    try:
        pan_x, pan_y = float(scene_pan[0]), float(scene_pan[1])
    except (TypeError, ValueError, IndexError):
        pan_x, pan_y = 0.0, 0.0
    scale = min(frame_w / canvas_w, frame_h / canvas_h) * zoom
    ox = (frame_w - canvas_w * scale) * 0.5
    oy = (frame_h - canvas_h * scale) * 0.5
    ox += pan_x * canvas_w * scale - float(tile_origin[0])
    oy += pan_y * canvas_h * scale - float(tile_origin[1])
    return scale, ox, oy


def layer_fit_scale(image, rect_w: float, rect_h: float) -> tuple[float, float]:
    """Aspect-fit scale of a layer image inside a canvas-px rect."""

    if image is None or rect_w <= 0 or rect_h <= 0:
        return 1.0, 1.0
    if hasattr(image, "shape"):
        h, w = image.shape[:2]
    else:
        from engine.store.tiled_pixel_store import pixel_source_size

        w, h = pixel_source_size(image)
    if h <= 0 or w <= 0:
        return 1.0, 1.0
    img_ar = w / h
    cell_ar = rect_w / rect_h
    if img_ar > cell_ar:
        return 1.0, cell_ar / img_ar
    return img_ar / cell_ar, 1.0


def scene_pan_limit(zoom: object) -> float:
    """Largest legal ``|pan|`` per axis at ``zoom`` so no void is shown.

    Inverting the camera (``screen_uv = (canvas_uv - 0.5 + pan) * zoom + 0.5``)
    over ``screen_uv in [0, 1]`` requires the visible window to stay inside
    the canvas, which holds exactly for ``|pan| <= (zoom-1)/(2*zoom)``.
    Identity zoom has no room to pan at all (``0.0``).
    """
    try:
        z = float(zoom)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if not (z > 1.0) or z != z:
        return 0.0
    return (z - 1.0) / (2.0 * z)


def clamp_scene_pan(
    pan_x: object,
    pan_y: object,
    zoom: object,
) -> tuple[float, float]:
    """Clamp a scene-camera pan to the range where the viewport shows no void."""
    limit = scene_pan_limit(zoom)
    out = []
    for value in (pan_x, pan_y):
        try:
            out.append(max(-limit, min(limit, float(value))))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            out.append(0.0)
    return out[0], out[1]


def scene_camera(state) -> tuple[float, float, float]:
    """``(zoom, pan_x, pan_y)`` of the whole-composition camera on ``state``.

    The one reader every consumer uses (projection, hit-testing, indicator,
    save-view export), so a state object that predates the camera fields —
    or an export widget with no state at all — reads as identity instead of
    raising. Out-of-range values are repaired here rather than at each call
    site: ``zoom`` below 1 would reveal void and a pan past the limit would
    show the frame edge, so both are clamped to the legal window.
    """
    try:
        zoom = float(getattr(state, "scene_zoom", 1.0))
    except (TypeError, ValueError):
        zoom = 1.0
    if not (zoom >= 1.0) or zoom != zoom:
        zoom = 1.0
    pan_x = getattr(state, "scene_pan_x", 0.0)
    pan_y = getattr(state, "scene_pan_y", 0.0)
    pan_x, pan_y = clamp_scene_pan(pan_x, pan_y, zoom)
    return zoom, pan_x, pan_y


def scene_camera_is_identity(camera: tuple[float, float, float]) -> bool:
    """True when the camera is at its identity (nothing to save as a view)."""
    zoom, pan_x, pan_y = camera
    return abs(zoom - 1.0) <= 1e-9 and abs(pan_x) <= 1e-9 and abs(pan_y) <= 1e-9


def build_render_context(
    *,
    composition,
    framebuffer_size: tuple[float, float],
    clip_matrix: tuple[float, ...],
    available_slot_ids: set[int] | frozenset[int],
    widget: object | None = None,
) -> MultiCompareRenderContext:
    """Project a resolved composition into a render context for one frame."""

    fb_w, fb_h = framebuffer_size
    scale, ox, oy = 1.0, 0.0, 0.0
    export_viewport = None
    if widget is not None:
        export_viewport = getattr(widget, "_export_canvas_viewport", None)
    # Scene camera: live renders follow the widget's state; the offscreen
    # export canvas is a *separate* widget whose state is identity, and a
    # tiled export viewport deliberately re-bases the projection itself — so
    # the camera is read only when no export viewport is in play. That keeps
    # export at identity by construction (a save-view crop renders the native
    # frame and crops it, see ``use_cases/export.py``) while an explicit
    # cropped export view could still pass a camera through a widget state.
    scene_zoom, scene_pan_x, scene_pan_y = 1.0, 0.0, 0.0
    if widget is not None and export_viewport is None:
        scene_zoom, scene_pan_x, scene_pan_y = scene_camera(
            getattr(widget, "state", None)
        )
    layers = []
    canvas_w = 1
    canvas_h = 1
    if composition is not None and composition.layers:
        canvas_w = max(1, int(composition.canvas_w))
        canvas_h = max(1, int(composition.canvas_h))
        if export_viewport is not None:
            # (full frame, tile origin) — framebuffer px, i.e. the same grid as
            # ``framebuffer_size`` (see engine.gpu.export_tiling units header).
            full_fb_w, full_fb_h, tile_left, tile_top = export_viewport
            scale, ox, oy = composition_frame_geometry(
                canvas_size=(float(canvas_w), float(canvas_h)),
                frame_size=(float(full_fb_w), float(full_fb_h)),
                tile_origin=(float(tile_left), float(tile_top)),
            )
        else:
            scale, ox, oy = composition_frame_geometry(
                canvas_size=(float(canvas_w), float(canvas_h)),
                frame_size=(fb_w, fb_h),
                scene_zoom=scene_zoom,
                scene_pan=(scene_pan_x, scene_pan_y),
            )
        layers = list(composition.layers)

    projected: list[ProjectedLayer] = []
    canvas_w_f = float(canvas_w)
    canvas_h_f = float(canvas_h)
    for layer in layers:
        slot_id = int(layer.layer_id)
        if slot_id not in available_slot_ids:
            continue
        lx, ly, lw, lh = layer.rect
        fit_x, fit_y = layer_fit_scale(layer.image, lw, lh)
        projected.append(
            ProjectedLayer(
                layer=layer,
                slot_id=slot_id,
                rect_fb=(
                    ox + lx * scale,
                    oy + ly * scale,
                    max(1.0, lw * scale),
                    max(1.0, lh * scale),
                ),
                slot_rect_uv=(
                    lx / canvas_w_f,
                    ly / canvas_h_f,
                    max(1e-6, lw / canvas_w_f),
                    max(1e-6, lh / canvas_h_f),
                ),
                fit_x=fit_x,
                fit_y=fit_y,
                zoom=float(layer.zoom),
                pan_x=float(layer.pan_x),
                pan_y=float(layer.pan_y),
            )
        )

    return MultiCompareRenderContext(
        composition=composition,
        framebuffer_size=framebuffer_size,
        scale=scale,
        offset=(ox, oy),
        clip_matrix=clip_matrix,
        projected_layers=tuple(projected),
        widget=widget,
    )
