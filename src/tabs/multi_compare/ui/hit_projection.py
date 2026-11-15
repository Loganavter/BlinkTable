"""Canvas↔widget projection helpers for multi-compare hit testing."""

from __future__ import annotations

from PySide6.QtCore import QRect

from tabs.multi_compare.models import LeafNode, SplitNode
from tabs.multi_compare.scene.projection import (
    composition_frame_geometry,
    scene_camera,
)
from tabs.multi_compare.ui import layout_geometry


def letterbox_transform(
    canvas_w: int,
    canvas_h: int,
    fb_w: float,
    fb_h: float,
    *,
    scene_zoom: float = 1.0,
    scene_pan: tuple[float, float] = (0.0, 0.0),
) -> tuple[float, float, float] | None:
    """Pure letterbox core: ``(sr, ox, oy)`` projecting canvas-px → widget-px.

    Delegates to :func:`tabs.multi_compare.scene.projection.
    composition_frame_geometry` — the same call ``build_render_context`` makes
    — so hit-testing and rendering cannot drift apart, including under the
    scene camera (``sr`` then carries ``scene_zoom`` and ``ox/oy`` the pan).
    ``None`` for degenerate inputs — the thin ``canvas_layout`` shell maps
    that (plus missing state) to its own ``None``.
    """
    if canvas_w <= 0 or canvas_h <= 0 or fb_w <= 0 or fb_h <= 0:
        return None
    return composition_frame_geometry(
        canvas_size=(float(canvas_w), float(canvas_h)),
        frame_size=(float(fb_w), float(fb_h)),
        scene_zoom=scene_zoom,
        scene_pan=scene_pan,
    )


def composition_canvas_size(active_comp, state, sources=None) -> tuple[int, int] | None:
    """Pure canvas-size resolution: active composition wins, else plan it.

    Data in, sizes out — no widget reads (``state`` is the tree data, not
    the live canvas). ``None`` when there is nothing to project yet.
    """
    if active_comp is not None and active_comp.canvas_w > 0 and active_comp.canvas_h > 0:
        return int(active_comp.canvas_w), int(active_comp.canvas_h)
    from tabs.multi_compare.services.composition_builder import (
        build_composition_plan,
    )

    plan = build_composition_plan(state, include_labels=False, sources=sources)
    if plan is None:
        return None
    if plan.canvas_w <= 0 or plan.canvas_h <= 0:
        return None
    return int(plan.canvas_w), int(plan.canvas_h)


def divider_gap_thickness(divider_settings, default: int) -> int:
    """Pure gap policy: visible thickness, else 0, else the builder default."""
    if divider_settings is None:
        return int(default)
    return max(0, int(divider_settings.thickness)) if divider_settings.visible else 0


def canvas_layout(widget) -> tuple[int, int, float, float, float] | None:
    """Return ``(canvas_w, canvas_h, sr, ox, oy)`` for projecting composition
    canvas-px to widget-px using the same letterbox formula as ``render()``.

    ``sr``/``ox``/``oy`` come from
    :func:`tabs.multi_compare.scene.projection.composition_frame_geometry`
    with the widget's scene camera — the exact call
    :func:`tabs.multi_compare.scene.projection.build_render_context` makes —
    so hit-testing and rendering are guaranteed to agree, zoomed or not.

    Thin impure shell: widget/state reads on top, geometry math delegated
    to :func:`composition_canvas_size` + :func:`letterbox_transform`.
    """
    if widget.state.root is None or widget.width() <= 0 or widget.height() <= 0:
        return None
    slot_sources = getattr(widget, "_slot_sources", None)
    if callable(slot_sources):
        sources = slot_sources()
    else:
        canvas = getattr(widget, "canvas", None)
        sources = canvas._slot_sources() if canvas is not None else None
    size = composition_canvas_size(
        getattr(widget, "_active_composition", None), widget.state, sources
    )
    if size is None:
        return None
    canvas_w, canvas_h = size
    zoom, pan_x, pan_y = scene_camera(widget.state)
    transform = letterbox_transform(
        canvas_w,
        canvas_h,
        float(widget.width()),
        float(widget.height()),
        scene_zoom=zoom,
        scene_pan=(pan_x, pan_y),
    )
    if transform is None:
        return None
    sr, ox, oy = transform
    return canvas_w, canvas_h, sr, ox, oy


def project_canvas_rect(
    rect_canvas: QRect, sr: float, ox: float, oy: float
) -> QRect:
    x = int(round(ox + rect_canvas.x() * sr))
    y = int(round(oy + rect_canvas.y() * sr))
    w = max(1, int(round(rect_canvas.width() * sr)))
    h = max(1, int(round(rect_canvas.height() * sr)))
    return QRect(x, y, w, h)


def project_canvas_rects(
    rects_canvas: list[QRect], sr: float, ox: float, oy: float
) -> list[QRect]:
    """Pure batch projection: canvas-px rects → widget-px rects."""
    return [project_canvas_rect(r, sr, ox, oy) for r in rects_canvas]


def composition_gap_canvas_px(widget) -> int:
    """Same gap ``build_composition_plan`` actually reserves between
    siblings for this widget's current state -- must track
    ``state.divider_settings.thickness`` (its own default, absent an
    explicit override) or hit-testing disagrees with what got rendered the
    moment the user changes the divider-width toolbar control.

    Thin impure shell over :func:`divider_gap_thickness`."""
    from tabs.multi_compare.services.composition_builder import (
        DEFAULT_SPLIT_GAP_PX,
    )

    return divider_gap_thickness(
        getattr(widget.state, "divider_settings", None), int(DEFAULT_SPLIT_GAP_PX)
    )


def drop_gaps(widget) -> list[tuple[SplitNode, tuple[int, ...], int, QRect]]:
    """Return split gaps with their tree paths for drop-zone hit-testing.

    Walks the layout in canvas-px (same gap as ``composition_builder``) and
    projects rects through the composition letterbox transform so divider
    hit-zones line up with what the renderer draws.
    """
    layout = canvas_layout(widget)
    if layout is None:
        return []
    canvas_w, canvas_h, sr, ox, oy = layout
    canvas_rect = QRect(0, 0, canvas_w, canvas_h)
    gaps_canvas = layout_geometry.drop_gaps(
        widget.state.root,
        canvas_rect,
        gap=composition_gap_canvas_px(widget),
    )
    return [
        (split, path, idx, project_canvas_rect(rect, sr, ox, oy))
        for (split, path, idx, rect) in gaps_canvas
    ]


def leaf_paths_and_rects(
    widget,
) -> list[tuple[LeafNode, QRect, tuple[int, ...]]]:
    layout = canvas_layout(widget)
    if layout is None:
        return []
    canvas_w, canvas_h, sr, ox, oy = layout
    canvas_rect = QRect(0, 0, canvas_w, canvas_h)
    leaves, _splits = layout_geometry.walk_paths(
        widget.state.root,
        canvas_rect,
        gap=composition_gap_canvas_px(widget),
    )
    return [
        (leaf, project_canvas_rect(rect, sr, ox, oy), path)
        for (leaf, rect, path) in leaves
    ]


def node_rect_at_path(widget, path: tuple[int, ...]) -> QRect | None:
    if widget.state.root is None:
        return None
    if not path:
        return widget.rect()
    layout = canvas_layout(widget)
    if layout is None:
        return None
    canvas_w, canvas_h, sr, ox, oy = layout
    canvas_rect = QRect(0, 0, canvas_w, canvas_h)
    leaves, splits = layout_geometry.walk_paths(
        widget.state.root,
        canvas_rect,
        only_path=path,
        gap=composition_gap_canvas_px(widget),
    )
    for _, rect, p in splits:
        if p == path:
            return project_canvas_rect(rect, sr, ox, oy)
    for _, rect, p in leaves:
        if p == path:
            return project_canvas_rect(rect, sr, ox, oy)
    return None


def leaf_rects(widget) -> list[tuple[LeafNode, QRect]]:
    """Leaf rects in widget-px, projected from the composition layout."""
    return [(leaf, rect) for (leaf, rect, _path) in leaf_paths_and_rects(widget)]
