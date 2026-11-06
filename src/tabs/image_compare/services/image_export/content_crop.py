"""Crop an exported raster to the area the images occupy.

The export canvas is the comparison envelope plus the virtual-canvas pads
(magnifier/uncrop) and whatever letterbox the fit leaves. "Crop to images"
trims all of that, so the file is the content rect and nothing else.

The rect is the plan's own content rect
(``resolve_plan_logical_image_rect`` — the same one ``get_canvas_scene``
reports for the live canvas), scaled to whatever raster the renderer produced:
a downscaled preview, an ``output_scale`` frame, or a user-typed resolution.
One rect, one scale — the preview and the file cannot disagree.
"""

from __future__ import annotations

from typing import Any

from ui.canvas_presentation.plan import resolve_plan_logical_image_rect


def content_rect_for_plan(plan: Any) -> tuple[int, int, int, int, int, int] | None:
    """``(x, y, w, h, canvas_w, canvas_h)`` of the plan content, or ``None``.

    ``None`` means "nothing to crop": no plan, a degenerate rect, or a rect
    that already covers the whole canvas (uncropped export is unchanged).
    """
    if plan is None:
        return None
    try:
        x, y, w, h = resolve_plan_logical_image_rect(plan)
        canvas_w = int(getattr(plan, "canvas_w", 0) or 0)
        canvas_h = int(getattr(plan, "canvas_h", 0) or 0)
    except Exception:
        return None
    if w <= 0 or h <= 0 or canvas_w <= 0 or canvas_h <= 0:
        return None
    if x == 0 and y == 0 and w == canvas_w and h == canvas_h:
        return None
    return (int(x), int(y), int(w), int(h), canvas_w, canvas_h)


def scale_rect_to_raster(
    rect: tuple[int, int, int, int, int, int] | None,
    raster_size: tuple[int, int] | None,
) -> tuple[int, int, int, int] | None:
    """Plan-px content rect -> raster-px crop box (rounded, clamped)."""
    if rect is None or raster_size is None:
        return None
    x, y, w, h, canvas_w, canvas_h = rect
    try:
        raster_w, raster_h = int(raster_size[0]), int(raster_size[1])
    except (TypeError, ValueError):
        return None
    if raster_w <= 0 or raster_h <= 0:
        return None
    sx = raster_w / float(canvas_w)
    sy = raster_h / float(canvas_h)
    left = max(0, min(raster_w, int(round(x * sx))))
    top = max(0, min(raster_h, int(round(y * sy))))
    right = max(0, min(raster_w, int(round((x + w) * sx))))
    bottom = max(0, min(raster_h, int(round((y + h) * sy))))
    if right - left < 1 or bottom - top < 1:
        return None
    return (left, top, right, bottom)


def crop_to_plan_content(image, plan: Any):
    """Trim *image* to the plan's image area; unchanged when there is none."""
    if image is None:
        return image
    size = getattr(image, "size", None)
    if not size:
        return image
    box = scale_rect_to_raster(content_rect_for_plan(plan), size)
    if box is None:
        return image
    try:
        return image.crop(box)
    except Exception:
        return image
