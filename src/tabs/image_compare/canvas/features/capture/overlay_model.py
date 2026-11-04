"""Immediate-overlay pilot: stateless capture-ring resolver (Engine Phase 3).

Pure decision + geometry layer for ``CaptureRingPass``. Every public
function takes the frame context first and returns frozen plain data —
no QRhi objects, no uniform buffers, no pipelines, no module-level
mutable state, no ``store`` import, no export-mode branch.
``CaptureRingPass`` (``passes.py``) keeps the retained GPU shell its
``CanvasRenderPass`` contract requires (``initialize``-owned
``FullscreenUniformPassResources``, ``prepare`` → ``resource_updates``
staging, ``record`` draw) and delegates all ring decisions here, so
live and snapshot rendering share one source of truth.

Source priority is unchanged: ``feature_overlay.capture_circles`` first,
then ``scene_frame.feature_payloads["capture_circles"]``, then
``widget.runtime_state._capture_circles``.

Retained on purpose (see ``test_overlay_stateless.py``): pipeline /
buffer ownership stays in the pass — creating them per frame would
recompile shaders every frame, and
``tests/contracts/test_pass_reads_only_ctx.py`` pins ``record()`` as
"ctx plus its own retained GPU resources".
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor

from engine.cache.screen_projection import project_px_to_screen
from engine.cache.stroke_geometry import shrink_screen_radius_for_stroke

__all__ = ["CaptureRing", "resolve_capture_circles", "resolve_capture_rings"]


@dataclass(frozen=True, slots=True)
class CaptureRing:
    """One capture ring, fully resolved to canvas-px (plain data)."""

    center_x: float
    center_y: float
    screen_radius: float
    line_width: float
    red: float
    green: float
    blue: float
    alpha: float


def _magnifier_capture_thickness(widget) -> int | None:
    """The user's detection-ring thickness, or ``None`` for the style default.

    Reached the same way the rest of the canvas reaches state from a widget:
    ``widget.runtime_state._store`` (set by ``CanvasWidget.set_store``). Any
    failure returns ``None`` — a missing setting must never change a frame.
    """
    try:
        from tabs.image_compare.canvas.features.magnifier.state.feature_state import (
            get_magnifier_widget_state,
        )

        state = getattr(widget, "runtime_state", None)
        store = getattr(state, "_store", None)
        view_state = getattr(getattr(store, "viewport", None), "view_state", None)
        if view_state is None:
            return None
        value = getattr(
            get_magnifier_widget_state(view_state), "default_capture_thickness", None
        )
        return None if value is None else int(value)
    except Exception:
        return None


def resolve_capture_circles(ctx, widget) -> tuple:
    """Resolve raw capture circles for this frame from the context (pure).

    Priority: ``feature_overlay.capture_circles`` → scene-frame payload
    ``capture_circles`` → ``widget.runtime_state._capture_circles``.
    Returns the circles untouched (no geometry applied); ``()`` when none.
    """
    overlay = getattr(ctx, "feature_overlay", None)
    circles = tuple(getattr(overlay, "capture_circles", ()) or ())
    if circles:
        return circles
    payloads = (
        ctx.scene_frame.feature_payloads
        if isinstance(getattr(ctx.scene_frame, "feature_payloads", None), dict)
        else {}
    )
    payload_circles = payloads.get("capture_circles")
    if payload_circles:
        return tuple(payload_circles)
    return tuple(getattr(widget.runtime_state, "_capture_circles", ()))


def resolve_capture_rings(ctx, widget) -> tuple[CaptureRing, ...]:
    """Resolve canvas-px capture rings for this frame (pure).

    Applies zoom, pan projection, stroke-shrink, and full-opacity ring
    color. Projection inputs come from ``ctx`` (canvas dims, zoom, pan,
    tile offsets) via the shared engine projector -- the widget handle
    only sources the fallback circle list (see ``resolve_capture_circles``).
    Calling twice with an unchanged frame yields an equal tuple;
    nothing is cached, stored, or mutated.
    """
    line_width = float(ctx.resolved_style.annotation_ring_stroke_px)
    # The owner's detection-area thickness (settings window, 2026-11-04). It
    # lives on the magnifier feature state because the ring is the loupe's
    # sampling area; ``None`` keeps the canvas style's own width, so an install
    # that never touched the row draws exactly what it drew before.
    override = _magnifier_capture_thickness(widget)
    if override is not None:
        line_width = float(max(1.0, int(override)))
    zoom = float(ctx.zoom_level)
    pan_x = float(getattr(ctx, "pan_offset_x", 0.0))
    pan_y = float(getattr(ctx, "pan_offset_y", 0.0))
    rings: list[CaptureRing] = []
    for center, radius, color in resolve_capture_circles(ctx, widget):
        if center is None or float(radius or 0.0) <= 0.0:
            continue
        center_x, center_y = project_px_to_screen(
            center.x(),
            center.y(),
            float(ctx.canvas_width),
            float(ctx.canvas_height),
            zoom,
            pan_x,
            pan_y,
            canvas_offset_x=float(ctx.canvas_offset_x),
            canvas_offset_y=float(ctx.canvas_offset_y),
        )
        draw_color = QColor(color)
        draw_color.setAlpha(255)
        rings.append(
            CaptureRing(
                center_x=float(center_x),
                center_y=float(center_y),
                screen_radius=shrink_screen_radius_for_stroke(
                    float(radius) * zoom, line_width
                ),
                line_width=line_width,
                red=draw_color.redF(),
                green=draw_color.greenF(),
                blue=draw_color.blueF(),
                alpha=draw_color.alphaF(),
            )
        )
    return tuple(rings)
