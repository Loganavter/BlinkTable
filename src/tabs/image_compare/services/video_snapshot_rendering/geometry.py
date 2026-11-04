"""Pure geometry helpers for snapshot frame preparation."""

from __future__ import annotations

from PIL import Image

from engine.store.pixel_ops.resample import write_resampled_to_store
from shared.image_processing.resample_map import get_resample
from engine.store.tiled_pixel_store import TiledPixelStore
from tabs.image_compare.canvas.presentation.plan_builder import CanvasGeometry
from tabs.image_compare.plugins.video_editor.services.video_export_models import (
    GlobalCanvasBounds,
    VideoRenderRequest,
)
from tabs.image_compare.services.video_snapshot_rendering.strip_window import (
    LAYER_PX,
)


#: The strip thumbnail's target/aspect reference. ``is_thumbnail_target_size``
#: reads its ASPECT from here; the size itself is the shape a strip tile has
#: when the block cell is one thumbnail wide.
THUMBNAIL_TARGET_SIZE = (160, 90)


def is_thumbnail_target_size(width, height) -> bool:
    """True for a STRIP-TILE target: the thumbnail's aspect, one layer at most.

    A strip tile is drawn stretched over one timeline block cell, so whatever
    the plan bakes into its pixels is what the cell shows: a fit-inside plan
    (aspect pads) puts bars inside the cell, a cover plan fills it. The gate
    that selects cover is therefore about the target being a TILE, not about
    one exact size: the tile follows the block cell at the target's DPR
    (128x72 at 1x, 160x90 at 1.25x, 256x144 at 2x), all of them the
    thumbnail's 16:9 aspect.

    Two conditions, both load-bearing:

    * **the thumbnail's aspect** (±1 px of the height that width implies, so a
      rounded device size still counts). A target of another aspect is not a
      block cell and keeps the fit-inside path with its pads.
    * **no larger than one texture-array layer** (``LAYER_PX``): a strip tile
      is uploaded 1:1 into one layer, so nothing bigger can be a tile. That is
      what keeps the EXPORT path untouched — every export target above one
      layer (1920x1080, 800x600, 3840x2160, ...) is not a tile and keeps the
      fit + letterbox semantics it has always had.

    Deliberately not "any target": widening to aspect alone would send every
    16:9 export frame down the cover path and crop the picture instead of
    letterboxing it.
    """
    try:
        target_w = int(width)
        target_h = int(height)
    except (TypeError, ValueError):
        return False
    if target_w <= 0 or target_h <= 0:
        return False
    if max(target_w, target_h) > LAYER_PX:
        return False
    thumb_w, thumb_h = THUMBNAIL_TARGET_SIZE
    expected_h = int(round(target_w * thumb_h / float(thumb_w)))
    return abs(target_h - expected_h) <= 1


def thumbnail_proxy_dest_scale(
    source_width, source_height, target_width, target_height
) -> float:
    """Tab-side spec for the pyramid pick: output px per source px.

    The engine decides the level (service calls
    ``PyramidPixelStore.best_level_for_scale`` with this scale); the tab
    only resolves the spec — same split as the export LOD path.
    """
    sw = max(1, int(source_width))
    sh = max(1, int(source_height))
    tw = max(1, int(target_width))
    th = max(1, int(target_height))
    return min(tw / float(sw), th / float(sh))


def _resample_for_method(method_name: str) -> Image.Resampling:
    return get_resample(method_name)


def resolve_prescale_target(request: VideoRenderRequest) -> tuple[int, int]:
    target_w = max(1, int(request.target_surface.width))
    target_h = max(1, int(request.target_surface.height))
    bounds = request.global_bounds
    if bounds is None:
        return target_w, target_h

    span_x = max(
        1.0,
        float(getattr(bounds, "canvas_x_max", 1.0))
        - float(getattr(bounds, "canvas_x_min", 0.0)),
    )
    span_y = max(
        1.0,
        float(getattr(bounds, "canvas_y_max", 1.0))
        - float(getattr(bounds, "canvas_y_min", 0.0)),
    )
    required_base_w = max(1, int(round(float(target_w) / span_x)))
    required_base_h = max(1, int(round(float(target_h) / span_y)))
    return (
        max(target_w, required_base_w),
        max(target_h, required_base_h),
    )


def fit_source_to_content(
    source,
    content_size: tuple[int, int],
    fill_rgba: tuple[int, int, int, int] | None = None,
    resize_method: str = "LANCZOS",
):
    """Scale ``source`` to fit *inside* ``content_size`` without letterbox bake.

    Returns an unpadded ``TiledPixelStore`` (or the original when already small
    enough). Aspect letterbox margins are expressed by folding insets into
    ``CanvasGeometry`` pads — see ``geometry_with_aspect_insets``.
    ``fill_rgba`` is accepted for call-site compatibility and ignored.
    """
    del fill_rgba
    cw, ch = content_size
    sw, sh = int(source.width), int(source.height)
    if sw <= 0 or sh <= 0 or cw <= 0 or ch <= 0:
        return source

    fit_r = min(cw / max(1, sw), ch / max(1, sh), 1.0)
    tw = max(1, int(sw * fit_r))
    th = max(1, int(sh * fit_r))
    if (tw, th) == (sw, sh) and isinstance(source, TiledPixelStore):
        return source
    if (tw, th) == (sw, sh) and isinstance(source, Image.Image):
        return TiledPixelStore.from_pil(
            source if source.mode == "RGBA" else source.convert("RGBA")
        )

    resample = _resample_for_method(resize_method)
    out = TiledPixelStore.allocate(tw, th)
    write_resampled_to_store(out, source, tw, th, resample)
    return out


def cover_crop_box(
    source_size: tuple[int, int], content_size: tuple[int, int]
) -> tuple[int, int, int, int] | None:
    """Pure geometry of the cover fit: ``(grown_w, grown_h, left, top)``.

    Scale by the max ratio, then center-crop to ``content_size`` — the box
    :func:`cover_source_to_content` materializes. Exposed separately so a
    caller can read the exact same box without allocating a ``TiledPixelStore``
    for a 160x90 tile (``allocate`` + resample costs ~11 ms per side, measured
    2026-10-29). ``None`` for a degenerate size.
    """
    sw, sh = int(source_size[0]), int(source_size[1])
    cw, ch = int(content_size[0]), int(content_size[1])
    if sw <= 0 or sh <= 0 or cw <= 0 or ch <= 0:
        return None
    cover_r = max(cw / float(sw), ch / float(sh))
    tw = max(1, int(round(sw * cover_r)))
    th = max(1, int(round(sh * cover_r)))
    return tw, th, max(0, (tw - cw) // 2), max(0, (th - ch) // 2)


def cover_source_to_content(
    source,
    content_size: tuple[int, int],
    resize_method: str = "LANCZOS",
):
    """Scale ``source`` to COVER ``content_size`` (thumbnail strip only).

    Unlike :func:`fit_source_to_content` (fit-inside + aspect insets folded
    into pads), cover scales by the max ratio and center-crops to exactly
    ``content_size`` — the strip shows full-bleed 160x90 blocks with no
    transparent letterbox pads to trim.
    Export/letterbox paths must NOT use this (they keep fit + pads).
    """
    cw, ch = int(content_size[0]), int(content_size[1])
    sw, sh = int(source.width), int(source.height)
    box = cover_crop_box((sw, sh), (cw, ch))
    if box is None:
        return source
    tw, th, left, top = box

    resample = _resample_for_method(resize_method)
    if (tw, th) == (sw, sh):
        if isinstance(source, TiledPixelStore):
            grown = source
        elif isinstance(source, Image.Image):
            grown = TiledPixelStore.from_pil(
                source if source.mode == "RGBA" else source.convert("RGBA")
            )
        else:
            return source
    else:
        grown = TiledPixelStore.allocate(tw, th)
        write_resampled_to_store(grown, source, tw, th, resample)
    if (tw, th) == (cw, ch):
        return grown
    try:
        cropped = grown.crop((left, top, left + cw, top + ch))
    except Exception:
        return grown
    if isinstance(cropped, TiledPixelStore):
        return cropped
    try:
        return TiledPixelStore.from_pil(
            cropped if cropped.mode == "RGBA" else cropped.convert("RGBA")
        )
    except Exception:
        return grown


def geometry_with_aspect_insets(
    canvas_geometry: CanvasGeometry,
    fitted_size: tuple[int, int],
) -> CanvasGeometry:
    """Fold aspect letterbox into pads so the image fills the content box."""
    content_w = max(1, int(canvas_geometry.image_width))
    content_h = max(1, int(canvas_geometry.image_height))
    fw, fh = int(fitted_size[0]), int(fitted_size[1])
    if fw <= 0 or fh <= 0:
        return canvas_geometry
    if (fw, fh) == (content_w, content_h):
        return canvas_geometry

    inset_x = max(0, (content_w - fw) // 2)
    inset_y = max(0, (content_h - fh) // 2)
    return CanvasGeometry(
        image_width=fw,
        image_height=fh,
        canvas_width=int(canvas_geometry.canvas_width),
        canvas_height=int(canvas_geometry.canvas_height),
        padding_left=int(canvas_geometry.padding_left) + inset_x,
        padding_top=int(canvas_geometry.padding_top) + inset_y,
        padding_right=int(canvas_geometry.padding_right)
        + max(0, content_w - fw - inset_x),
        padding_bottom=int(canvas_geometry.padding_bottom)
        + max(0, content_h - fh - inset_y),
        virtual_layout=canvas_geometry.virtual_layout,
    )


def resolve_scaled_content_geometry(
    frame,
) -> tuple[tuple[int, int], tuple[int, int], int, int]:
    """Map a prepared frame to ``(canvas_size, content_size, pad_left, pad_top)``.

    ``frame.render_*`` is the letterboxed *image* box from
    ``build_render_frame_presentation``, not the export framebuffer. The
    padded canvas must stay ``frame.target`` (export resolution). Using render
    size as the canvas collapses pillar/letterbox pads; ``render_loop`` then
    stretches the short frame to the job size (4:3 sources → fat 16:9 DAR).

    With ``virtual_layout``, pads come from normalized canvas bounds (same
    target-vs-render rule — otherwise span_y shrink invents fake side chrome).
    """
    target = getattr(frame, "target", None)
    if target is not None and int(getattr(target, "width", 0) or 0) > 0:
        canvas_w = max(1, int(target.width))
        canvas_h = max(1, int(target.height))
    else:
        canvas_w = max(1, int(frame.render_width))
        canvas_h = max(1, int(frame.render_height))

    if frame.virtual_layout is None:
        content_w = max(1, int(frame.render_width))
        content_h = max(1, int(frame.render_height))
        pad_left = max(0, int(getattr(frame, "image_dest_x", 0) or 0))
        pad_top = max(0, int(getattr(frame, "image_dest_y", 0) or 0))
        # Prefer layout pads when present (same numbers as image_dest_*).
        layout = getattr(frame, "layout", None)
        if layout is not None:
            pad_left = max(0, int(getattr(layout, "content_x", pad_left) or 0))
            pad_top = max(0, int(getattr(layout, "content_y", pad_top) or 0))
            content_w = max(1, int(getattr(layout, "content_width", content_w) or content_w))
            content_h = max(1, int(getattr(layout, "content_height", content_h) or content_h))
        return (
            (canvas_w, canvas_h),
            (content_w, content_h),
            pad_left,
            pad_top,
        )

    bounds = frame.virtual_layout.canvas_bounds
    span_x = max(1e-6, float(bounds.x_max - bounds.x_min))
    span_y = max(1e-6, float(bounds.y_max - bounds.y_min))
    base_w = max(1, int(round(canvas_w / span_x)))
    base_h = max(1, int(round(canvas_h / span_y)))
    pad_left = max(0, int(round(-float(bounds.x_min) * base_w)))
    pad_top = max(0, int(round(-float(bounds.y_min) * base_h)))
    return (
        (canvas_w, canvas_h),
        (base_w, base_h),
        pad_left,
        pad_top,
    )


def scale_global_bounds(
    bounds: GlobalCanvasBounds,
    prescale_target: tuple[int, int],
    *,
    output_size: tuple[int, int] | None = None,
) -> GlobalCanvasBounds:
    source_w = max(1, int(bounds.base_width))
    source_h = max(1, int(bounds.base_height))
    target_w, target_h = prescale_target
    ratio = min(float(target_w) / float(source_w), float(target_h) / float(source_h))
    if output_size is not None:
        out_w, out_h = output_size
        span_x = max(1.0, float(bounds.canvas_x_max) - float(bounds.canvas_x_min))
        span_y = max(1.0, float(bounds.canvas_y_max) - float(bounds.canvas_y_min))
        min_ratio = min(
            1.0,
            float(max(1, out_w)) / (float(source_w) * span_x),
            float(max(1, out_h)) / (float(source_h) * span_y),
        )
        ratio = max(ratio, min_ratio)
    if ratio >= 0.999:
        return bounds

    return GlobalCanvasBounds(
        pad_left=max(0, int(round(bounds.pad_left * ratio))),
        pad_right=max(0, int(round(bounds.pad_right * ratio))),
        pad_top=max(0, int(round(bounds.pad_top * ratio))),
        pad_bottom=max(0, int(round(bounds.pad_bottom * ratio))),
        base_width=max(1, int(round(source_w * ratio))),
        base_height=max(1, int(round(source_h * ratio))),
        canvas_x_min=float(bounds.canvas_x_min),
        canvas_x_max=float(bounds.canvas_x_max),
        canvas_y_min=float(bounds.canvas_y_min),
        canvas_y_max=float(bounds.canvas_y_max),
    )