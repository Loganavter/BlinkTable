"""Snapshot of the live "save view" region for the export paths of this tab.

The export dialog is **non-modal**, so the region the user saw when the dialog
opened is a snapshot: the rect is published once and the save path crops to
exactly that rect. Re-reading the live zoom at save time would write a region
the preview never showed (the user may have zoomed while the dialog was up).

Geometry is ``shared.image_processing.view_geometry`` — the one form both
canvases share (``screen_uv = (canvas_uv - 0.5 + pan) * zoom + 0.5``). This
module only adapts the tab's live accessor (``CanvasGeometryProvider.
get_zoom_pan``) and the dialog/save option names; it is not a second
implementation of the math.
"""

from __future__ import annotations

from dataclasses import dataclass

from shared.image_processing.view_geometry import visible_view_rect, view_native_size


@dataclass(frozen=True, slots=True)
class ViewSnapshot:
    """The visible region in canvas px plus the size it exports at."""

    rect: tuple[int, int, int, int]
    size: tuple[int, int]
    # False when the rect covers the whole canvas: ``zoom < 1`` reveals
    # nothing the full frame does not already show (view_geometry reads it as
    # the whole canvas), so the option would be a labelled no-op.
    cropped: bool


def parse_view_rect(raw) -> tuple[int, int, int, int] | None:
    """Validate an option-supplied ``view_rect`` (canvas px) or ``None``.

    The rect travels through the dialog's option dict, so anything can be in
    there by the time the worker runs; a value that is not four positive
    numbers means "no view crop" and the full frame is written as before.
    """
    if not raw:
        return None
    try:
        left, top, width, height = (int(round(float(v))) for v in tuple(raw)[:4])
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return (max(0, left), max(0, top), width, height)


def resolve_view_snapshot(canvas_w, canvas_h, zoom_pan) -> ViewSnapshot | None:
    """Snapshot of the visible region, or ``None`` when there is no view.

    ``canvas_w``/``canvas_h`` are the export plan's canvas in canvas px (the
    same space the rect is returned in); ``zoom_pan`` is the
    ``(zoom, pan_x, pan_y)`` triple from the tab's canvas geometry provider.
    Fail-open: a missing canvas or accessor reads as "no view" — never an
    exception, because the dialog must still open without a canvas.
    """
    try:
        cw = int(round(float(canvas_w)))
        ch = int(round(float(canvas_h)))
    except (TypeError, ValueError):
        return None
    if cw <= 0 or ch <= 0 or not zoom_pan:
        return None
    try:
        zoom, pan_x, pan_y = (float(v) for v in tuple(zoom_pan)[:3])
    except (TypeError, ValueError):
        return None
    rect = visible_view_rect(cw, ch, zoom, pan_x, pan_y)
    return ViewSnapshot(
        rect=rect,
        size=view_native_size(cw, ch, zoom, pan_x, pan_y),
        cropped=rect != (0, 0, cw, ch),
    )
