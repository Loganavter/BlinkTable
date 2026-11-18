from PySide6.QtCore import QRect

from ui.canvas_infra.viewport.contract import DisplaySplitPositionRequest
from ui.canvas_infra.viewport.geometry import QuickContentRect
from ui.canvas_infra.viewport.pipeline import compute_display_split_position
from ui.canvas_infra.viewport.state import set_display_split_position

from ui.canvas_infra.rhi.render_common import widget_px_to_screen_px


def update_display_split_position(
    widget,
    *,
    scene,
    zoom_level: float,
    pan_offset_x: float,
    pan_offset_y: float,
    anchor_to_viewport: bool = True,
) -> float:
    if scene is None:
        return set_display_split_position(
            widget, getattr(widget, "split_position", 0.5)
        )

    w, h = widget.width(), widget.height()
    img1 = widget.runtime_state._stored_pil_images[0]
    if img1 and w > 0 and h > 0:
        content_rect = None
        content_rect_px = (
            getattr(widget.runtime_state, "_inner_content_rect_px", None)
            or widget.runtime_state._content_rect_px
        )
        if content_rect_px:
            cx, cy, cw, ch = content_rect_px
            if cw > 0 and ch > 0:
                content_rect = QuickContentRect(x=cx, y=cy, width=cw, height=ch)
        from shared.image_processing.image_dims import get_image_dims

        iw, ih = get_image_dims(img1)
        result = compute_display_split_position(
            DisplaySplitPositionRequest(
                widget_width=w,
                widget_height=h,
                image_width=iw,
                image_height=ih,
                split_visual=scene.split_position_visual,
                is_horizontal=scene.is_horizontal,
                zoom_level=zoom_level if anchor_to_viewport else 1.0,
                pan_offset_x=pan_offset_x if anchor_to_viewport else 0.0,
                pan_offset_y=pan_offset_y if anchor_to_viewport else 0.0,
                content_rect=content_rect,
            )
        )
        return set_display_split_position(widget, result)
    return set_display_split_position(widget, scene.split_position_visual)


def get_content_rect_screen_px(widget) -> tuple[int, int, int, int] | None:
    content_rect = (
        getattr(widget.runtime_state, "_inner_content_rect_px", None)
        or widget.runtime_state._content_rect_px
    )
    if not content_rect:
        return None

    x, y, w, h = content_rect
    if w <= 0 or h <= 0:
        return None

    x0, y0 = widget_px_to_screen_px(widget, x, y)
    x1, y1 = widget_px_to_screen_px(widget, x + w, y + h)
    left = int(round(min(x0, x1)))
    top = int(round(min(y0, y1)))
    width = max(0, int(round(abs(x1 - x0))))
    height = max(0, int(round(abs(y1 - y0))))
    if width <= 0 or height <= 0:
        return None
    return (left, top, width, height)


def get_view_transformed_content_rect_widget_px(
    widget,
) -> tuple[float, float, float, float] | None:
    """Letterbox content rect after the live zoom/pan camera (widget-px).

    Used by ``DividerPass`` so the line's clip/position track the base image
    on both axes after zoom/pan.
    """
    from ui.canvas_infra.viewport.geometry import map_content_rect_through_view
    from ui.canvas_infra.viewport.state import (
        get_pan_offset_x,
        get_pan_offset_y,
        get_zoom_level,
    )

    state = getattr(widget, "runtime_state", None)
    if state is None:
        return None
    content_rect = (
        getattr(state, "_inner_content_rect_px", None) or state._content_rect_px
    )
    if not content_rect:
        return None
    x, y, w, h = content_rect
    if w <= 0 or h <= 0:
        return None
    widget_w = float(widget.width())
    widget_h = float(widget.height())
    if widget_w <= 0 or widget_h <= 0:
        return None
    return map_content_rect_through_view(
        (float(x), float(y), float(w), float(h)),
        widget_width=widget_w,
        widget_height=widget_h,
        zoom_level=get_zoom_level(widget),
        pan_offset_x=get_pan_offset_x(widget),
        pan_offset_y=get_pan_offset_y(widget),
    )

