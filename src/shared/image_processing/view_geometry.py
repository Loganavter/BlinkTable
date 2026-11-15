"""Visible-view geometry for zoomed exports ("save view") — one source.

Both compare canvases map a *canvas-space* point ``c`` (0..1) to screen uv
``s`` with the same affine form::

    s = (c - 0.5 + pan) * zoom + 0.5

That is the single-view canvas's base vertex shader (``screenUV = (targetUV -
center + offset) * zoom + center``), the per-slot form of the same chain in the
multi-view canvas's fragment shader (inverted there as ``uv = (uv - 0.5)/zoom +
0.5 - pan``), and the composition projection of its scene camera.

A "save view" export writes exactly the region visible on screen, at native
density: one canvas pixel per output pixel. Inverting the form over
``s in [0, 1]`` gives that region::

    c_min = 0.5 - pan - 0.5/zoom        c_size = 1/zoom

``zoom <= 1`` cannot reveal anything the full canvas does not already show, so
it reads as the whole canvas (the caller's option then becomes a no-op rather
than an upscale of letterbox padding).

Units: ``canvas_w``/``canvas_h`` are the composition canvas in canvas pixels
(the same space ``CompositionPlan.canvas_w`` / an export plan's ``canvas_w``
use); the returned rect is in those pixels. Callers that render an export frame
at ``(canvas_w, canvas_h)`` and then crop by this rect get one output px per
canvas px, which is the whole point — a caller that instead resized the frame
into the rect's size would resample the composition and desync from the live
view.
"""

from __future__ import annotations


def _coerce_positive(value: object, fallback: float = 1.0) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    if not (out > 0.0):
        return fallback
    return out


def visible_view_uv(
    zoom: object,
    pan_x: object = 0.0,
    pan_y: object = 0.0,
) -> tuple[float, float, float, float]:
    """Visible region in canvas-normalized ``(left, top, width, height)``.

    ``zoom <= 1`` reads as ``(0, 0, 1, 1)``. Pan is clamped so the rect stays
    inside the canvas: a viewport cannot show void the canvas does not have.
    Fail-open: bad inputs read as the whole canvas.
    """
    z = _coerce_positive(zoom, 1.0)
    if z <= 1.0:
        return (0.0, 0.0, 1.0, 1.0)
    try:
        px = float(pan_x)  # type: ignore[arg-type]
        py = float(pan_y)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        px = py = 0.0
    size = 1.0 / z
    limit = 0.5 - size * 0.5
    left = min(max(0.5 - px - size * 0.5, 0.0), max(0.0, 2.0 * limit))
    top = min(max(0.5 - py - size * 0.5, 0.0), max(0.0, 2.0 * limit))
    return (left, top, size, size)


def visible_view_rect(
    canvas_w: object,
    canvas_h: object,
    zoom: object = 1.0,
    pan_x: object = 0.0,
    pan_y: object = 0.0,
) -> tuple[int, int, int, int]:
    """Visible region in canvas pixels: ``(left, top, width, height)``.

    The rect is clamped to the canvas and never smaller than 1x1. Integer
    rounding keeps ``left + width <= canvas_w`` and ``top + height <=
    canvas_h`` so a crop cannot run off the frame.
    """
    try:
        cw = int(round(float(canvas_w)))  # type: ignore[arg-type]
        ch = int(round(float(canvas_h)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return (0, 0, 1, 1)
    cw = max(1, cw)
    ch = max(1, ch)
    left_uv, top_uv, w_uv, h_uv = visible_view_uv(zoom, pan_x, pan_y)
    width = max(1, min(cw, int(round(w_uv * cw))))
    height = max(1, min(ch, int(round(h_uv * ch))))
    left = min(max(0, int(round(left_uv * cw))), cw - width)
    top = min(max(0, int(round(top_uv * ch))), ch - height)
    return (left, top, width, height)


def view_native_size(
    canvas_w: object,
    canvas_h: object,
    zoom: object = 1.0,
    pan_x: object = 0.0,
    pan_y: object = 0.0,
) -> tuple[int, int]:
    """Output size of a "save view" export for this view (native density)."""
    _left, _top, width, height = visible_view_rect(canvas_w, canvas_h, zoom, pan_x, pan_y)
    return (width, height)
