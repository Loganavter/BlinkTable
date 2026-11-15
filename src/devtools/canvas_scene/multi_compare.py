"""Multi Compare canvas scene adapter (composition px).

Reuses the tab's resolved composition and its projection; see
``devtools.canvas_scene`` for the R1-R7 contract.
"""

from __future__ import annotations

from typing import Any

from .common import (
    SPACE_COMPOSITION_PX,
    SPACE_FRAMEBUFFER_PX,
    SPACE_RELATIVE,
    _num,
)


# Multi Compare adapter (composition px)
# ---------------------------------------------------------------------------


def _multi_compare_scene(canvas: Any) -> dict[str, Any]:
    state = getattr(canvas, "state", None)
    composition = getattr(canvas, "_active_composition", None)
    unavailable: list[dict[str, str]] = []
    sources: dict[str, str] = {}

    canvas_block = _mc_canvas_block(composition, sources, unavailable)
    panes = _mc_panes(state, composition, canvas_block, sources, unavailable)
    dividers = _mc_dividers(state, composition, sources, unavailable)
    transform = _mc_transform(state, composition, canvas, sources, unavailable)

    sources["magnifiers"] = "not_applicable"
    unavailable.append(
        {
            "field": "magnifiers",
            "reason": "multi_compare has no magnifier feature",
        }
    )
    focus = getattr(state, "focused_slot_id", None)
    return {
        "canvas": canvas_block,
        "panes": panes,
        "dividers": dividers,
        "magnifiers": [],
        "transform": transform,
        "render_layout": None,
        "focus": {
            "focused": bool(getattr(state, "is_focused", False)),
            "slot_id": int(focus) if focus is not None else None,
            "source": "multi_compare_state",
        },
        "sources": sources,
        "unavailable": unavailable,
    }


def _mc_canvas_block(
    composition: Any,
    sources: dict[str, str],
    unavailable: list[dict[str, str]],
) -> dict[str, Any] | None:
    width = int(getattr(composition, "canvas_w", 0) or 0)
    height = int(getattr(composition, "canvas_h", 0) or 0)
    if width > 0 and height > 0:
        sources["canvas"] = "mc_resolved_composition"
        return {
            "w": width,
            "h": height,
            "space": SPACE_COMPOSITION_PX,
            "source": "mc_resolved_composition",
        }
    sources["canvas"] = "unavailable"
    unavailable.append(
        {
            "field": "canvas",
            "reason": "no resolved composition yet (the canvas has not rendered a plan)",
        }
    )
    return None


def _mc_panes(
    state: Any,
    composition: Any,
    canvas_block: dict[str, Any] | None,
    sources: dict[str, str],
    unavailable: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Both pane truths (R3): painted composition layers + raw hit-test leaves."""
    slots_by_id = {int(slot.id): slot for slot in (getattr(state, "slots", None) or ())}
    layers = list(getattr(composition, "layers", None) or ())
    painted_ids = {int(layer.layer_id) for layer in layers}
    sources["panes"] = "mc_resolved_composition+layout_tree"
    rows: list[dict[str, Any]] = [
        {
            "slot": int(layer.layer_id),
            "path": _slot_path(slots_by_id.get(int(layer.layer_id))),
            "rect": [_num(value) for value in layer.rect],
            "space": SPACE_COMPOSITION_PX,
            "z": index,
            "visible": True,
            "painted": True,
            "in_composition": True,
            "reason": None,
            "rect_source": "mc_resolved_composition",
            "layer_zoom": _num(getattr(layer, "zoom", 1.0), 1.0),
            "layer_pan": [
                _num(getattr(layer, "pan_x", 0.0)),
                _num(getattr(layer, "pan_y", 0.0)),
            ],
        }
        for index, layer in enumerate(layers)
    ]

    root = getattr(state, "root", None)
    if root is None:
        unavailable.append(
            {"field": "panes", "reason": "the tab has no layout tree yet"}
        )
        return sorted(rows, key=lambda row: row["slot"])
    if canvas_block is None:
        unavailable.append(
            {
                "field": "panes",
                "reason": (
                    "no resolved composition — the layout tree exists but the "
                    "renderer has not planned a frame, so pane rects are unknown"
                ),
            }
        )
        return sorted(rows, key=lambda row: row["slot"])

    from PySide6.QtCore import QRect

    from tabs.multi_compare.ui import layout_geometry

    leaves, _splits = layout_geometry.walk_paths(
        root,
        QRect(0, 0, int(canvas_block["w"]), int(canvas_block["h"])),
        gap=_mc_gap_px(state),
    )
    focused = getattr(state, "focused_slot_id", None)
    is_focused = bool(getattr(state, "is_focused", False))
    excluded = 0
    for leaf, rect, _path in leaves:
        slot_id = int(leaf.slot_id)
        if slot_id in painted_ids:
            continue
        excluded += 1
        rows.append(
            {
                "slot": slot_id,
                "path": _slot_path(slots_by_id.get(slot_id)),
                "rect": [
                    _num(rect.x()),
                    _num(rect.y()),
                    _num(rect.width()),
                    _num(rect.height()),
                ],
                "space": SPACE_COMPOSITION_PX,
                "z": None,
                "visible": False,
                "painted": False,
                "in_composition": False,
                "reason": _mc_exclusion_reason(
                    slots_by_id.get(slot_id), slot_id, focused, is_focused
                ),
                "rect_source": "layout_tree (hit-testing walk)",
                "layer_zoom": None,
                "layer_pan": None,
            }
        )
    if excluded:
        unavailable.append(
            {
                "field": "panes[painted]",
                "reason": (
                    f"{excluded} pane(s) exist in the layout tree but are not in the "
                    "render composition — reasons are per pane"
                ),
            }
        )
    return sorted(rows, key=lambda row: row["slot"])


def _mc_exclusion_reason(
    slot: Any, slot_id: int, focused: Any, is_focused: bool
) -> str:
    """Why the composition builder dropped a leaf that hit-testing still walks.

    ``build_composition_plan`` skips a leaf in exactly two ways
    (``services/composition_builder.py:162-172``): another slot is focused, or
    the slot is missing / has no decoded image. There is no third branch, so
    "not painted, not focused, not missing" cannot silently stay unnamed.
    """
    if is_focused and focused is not None and int(focused) != slot_id:
        return "focus_collapsed (another slot is focused)"
    if slot is None:
        return "slot_missing (leaf references a slot the state does not hold)"
    return "imageless (no decoded pixels for this slot)"


def _slot_path(slot: Any) -> str | None:
    path = getattr(slot, "path", None) if slot is not None else None
    return str(path) if path else None


def _mc_gap_px(state: Any) -> int:
    """The gap ``build_composition_plan`` reserves — same policy, no mutation."""
    from tabs.multi_compare.services.composition_builder import DEFAULT_SPLIT_GAP_PX
    from tabs.multi_compare.ui.hit_projection import divider_gap_thickness

    return int(
        divider_gap_thickness(
            getattr(state, "divider_settings", None), int(DEFAULT_SPLIT_GAP_PX)
        )
    )


def _mc_dividers(
    state: Any,
    composition: Any,
    sources: dict[str, str],
    unavailable: list[dict[str, str]],
) -> list[dict[str, Any]]:
    gaps = list(getattr(composition, "gaps", None) or ())
    settings = getattr(state, "divider_settings", None)
    sources["dividers"] = "mc_resolved_composition.gaps"
    rows = []
    for index, gap in enumerate(gaps):
        x, y, width, height = gap.rect
        direction = str(getattr(gap, "direction", ""))
        if direction == "h":
            # A row of children reserves a vertical strip -> a vertical line.
            axis, pos, thickness = "v", _num(x), _num(width)
        else:
            axis, pos, thickness = "h", _num(y), _num(height)
        rows.append(
            {
                "axis": axis,
                "pos": pos,
                "thickness": thickness,
                "visible": bool(getattr(settings, "visible", True)),
                "space": SPACE_COMPOSITION_PX,
                "thickness_space": SPACE_COMPOSITION_PX,
                "source": "mc_resolved_composition.gaps",
                "order": index,
                "rect": [_num(x), _num(y), _num(width), _num(height)],
            }
        )
    if not rows:
        unavailable.append(
            {
                "field": "dividers",
                "reason": (
                    "no split gap in the composition (single pane, or dividers hidden)"
                ),
            }
        )
    return sorted(rows, key=lambda row: (row["axis"], row["pos"], row["order"]))


def _mc_transform(
    state: Any,
    composition: Any,
    canvas: Any,
    sources: dict[str, str],
    unavailable: list[dict[str, str]],
) -> dict[str, Any]:
    """The projection's own output — never a second implementation (R3)."""
    framebuffer, framebuffer_source = _framebuffer_size(canvas)
    if composition is None:
        sources["transform"] = "unavailable"
        unavailable.append(
            {
                "field": "transform.projection",
                "reason": "no resolved composition to project",
            }
        )
        projection = None
    else:
        from tabs.multi_compare.scene.projection import build_render_context

        available = {int(layer.layer_id) for layer in composition.layers}
        context = build_render_context(
            composition=composition,
            framebuffer_size=framebuffer,
            clip_matrix=(),
            available_slot_ids=available,
            widget=canvas,
        )
        projection = {
            "scale": _num(context.scale, 1.0),
            "offset": [_num(context.offset[0]), _num(context.offset[1])],
            "from_space": SPACE_COMPOSITION_PX,
            "space": SPACE_FRAMEBUFFER_PX,
            "source": "tabs.multi_compare.scene.projection.build_render_context",
        }
        sources["transform"] = projection["source"]
    return {
        "source": sources["transform"],
        "input_space": SPACE_COMPOSITION_PX,
        "output_space": SPACE_FRAMEBUFFER_PX,
        # The transform-level camera is the SCENE camera (whole composition):
        # it is what scales the projection below, so reporting the content
        # camera here made the read claim "identity" while the projection was
        # visibly zoomed (measured live 2026-11-15). The per-slot content
        # camera stays visible on each pane as ``layer_zoom``/``layer_pan``.
        "camera": {
            "zoom": _num(_scene_camera(state)[0], 1.0),
            "pan": [_num(_scene_camera(state)[1]), _num(_scene_camera(state)[2])],
            "space": SPACE_RELATIVE,
            "state_materialized": True,
        },
        "content_camera": {
            "zoom": _num(getattr(state, "zoom", 1.0), 1.0),
            "pan": [
                _num(getattr(state, "pan_x", 0.0)),
                _num(getattr(state, "pan_y", 0.0)),
            ],
            "space": SPACE_RELATIVE,
            "state_materialized": True,
        },
        "projection": projection,
        "framebuffer": [_num(framebuffer[0]), _num(framebuffer[1])],
        "framebuffer_source": framebuffer_source,
    }


def _scene_camera(state: Any) -> tuple[float, float, float]:
    """``(zoom, pan_x, pan_y)`` of the whole-composition camera.

    Delegates to the tab's single reader (``scene/projection.py``) so the
    scene dump cannot drift from the renderer; a state without the fields
    reads as identity.
    """
    try:
        from tabs.multi_compare.scene.projection import scene_camera

        return scene_camera(state)
    except Exception:
        return (1.0, 0.0, 0.0)


def _framebuffer_size(canvas: Any) -> tuple[tuple[float, float], str]:
    """Device pixels of the canvas widget — the renderer's target size proxy.

    The real target size comes from ``renderTarget().pixelSize()`` inside the
    paint pass; a read outside it uses the widget's device size and says so.
    """
    try:
        ratio = float(canvas.devicePixelRatioF() or 1.0)
    except Exception:
        ratio = 1.0
    try:
        width = float(max(0, int(canvas.width())))
        height = float(max(0, int(canvas.height())))
    except Exception:
        width = height = 0.0
    return (width * ratio, height * ratio), "widget_device_size"


# ---------------------------------------------------------------------------
