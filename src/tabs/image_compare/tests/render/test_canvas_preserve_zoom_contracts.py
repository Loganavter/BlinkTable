"""Canvas preserve-zoom keeps image focus stable across letterbox changes.

Dogma source: docs/dev/rendering/zoom-pan.md § Preserving the image sample point
when the letterbox changes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PIL import Image

from ui.canvas_infra.viewport.state import (
    set_pan_offsets,
    set_zoom_level,
)
from ui.canvas_infra.viewport.focus import (
    capture_letterbox_focus,
    restore_letterbox_focus,
)

def _canvas(letterbox):
    return SimpleNamespace(
        runtime_state=SimpleNamespace(_letterbox_params=[letterbox]),
        zoom_level=None,
        pan_offset_x=None,
        pan_offset_y=None,
    )

def test_preserve_zoom_restores_image_focus_after_letterbox_change():
    canvas = _canvas((0.10, 0.00, 0.80, 1.00))
    set_zoom_level(canvas, 8.0)
    set_pan_offsets(canvas, 0.12, -0.04)

    focus = capture_letterbox_focus(canvas)
    canvas.runtime_state._letterbox_params[0] = (0.08, 0.02, 0.84, 0.96)
    assert restore_letterbox_focus(canvas, focus) is True

    assert capture_letterbox_focus(canvas) == focus

def test_preserve_zoom_restores_image_focus_at_fit_zoom():
    canvas = _canvas((0.10, 0.00, 0.80, 1.00))
    set_zoom_level(canvas, 1.0)
    set_pan_offsets(canvas, 0.12, -0.04)

    focus = capture_letterbox_focus(canvas)
    canvas.runtime_state._letterbox_params[0] = (0.08, 0.02, 0.84, 0.96)
    assert restore_letterbox_focus(canvas, focus) is True

    assert capture_letterbox_focus(canvas) == focus


def test_pan_drag_at_fit_zoom_moves_offsets():
    from ui.canvas_infra.viewport.contract import PanDragRequest
    from ui.canvas_infra.viewport.zoom import compute_zoom_pan_drag_transform

    result = compute_zoom_pan_drag_transform(
        PanDragRequest(
            widget_width=200,
            widget_height=100,
            current_zoom=1.0,
            current_pan_x=0.0,
            current_pan_y=0.0,
            last_mouse_x=50.0,
            last_mouse_y=50.0,
            mouse_x=70.0,
            mouse_y=40.0,
        )
    )
    assert result is not None
    new_pan_x, new_pan_y = result
    assert new_pan_x == pytest.approx(0.1)
    assert new_pan_y == pytest.approx(-0.1)


def test_split_stays_image_anchored_when_panning_at_fit_zoom():
    """Store split must not camera-recompute on pan; display still tracks pan."""
    from ui.canvas_infra.viewport.contract import (
        DisplaySplitPositionRequest,
        SplitPositionForViewTransformRequest,
    )
    from ui.canvas_infra.viewport.geometry import QuickContentRect
    from ui.canvas_infra.viewport.zoom import (
        compute_zoom_display_split_position,
        compute_zoom_split_position_for_view_transform,
    )

    content = QuickContentRect(x=0.0, y=0.0, width=200.0, height=100.0)
    store_split = 0.4

    updated = compute_zoom_split_position_for_view_transform(
        SplitPositionForViewTransformRequest(
            widget_width=200,
            widget_height=100,
            image_width=200,
            image_height=100,
            is_horizontal=False,
            split_position_visual=store_split,
            current_zoom=1.0,
            current_pan_x=0.0,
            current_pan_y=0.0,
            new_zoom=1.0,
            new_pan_x=0.15,
            new_pan_y=0.0,
            content_rect=content,
        )
    )
    assert updated is None

    display_before = compute_zoom_display_split_position(
        DisplaySplitPositionRequest(
            widget_width=200,
            widget_height=100,
            image_width=200,
            image_height=100,
            split_visual=store_split,
            is_horizontal=False,
            zoom_level=1.0,
            pan_offset_x=0.0,
            pan_offset_y=0.0,
            content_rect=content,
        )
    )
    display_after = compute_zoom_display_split_position(
        DisplaySplitPositionRequest(
            widget_width=200,
            widget_height=100,
            image_width=200,
            image_height=100,
            split_visual=store_split,
            is_horizontal=False,
            zoom_level=1.0,
            pan_offset_x=0.15,
            pan_offset_y=0.0,
            content_rect=content,
        )
    )
    assert display_after == pytest.approx(display_before + 0.15)


def test_display_split_not_clamped_when_content_leaves_viewport():
    """Clamping display spit to 0..1 glued the line to the screen edge."""
    from ui.canvas_infra.viewport.contract import DisplaySplitPositionRequest
    from ui.canvas_infra.viewport.geometry import QuickContentRect
    from ui.canvas_infra.viewport.zoom import compute_zoom_display_split_position

    content = QuickContentRect(x=0.0, y=100.0, width=800.0, height=400.0)
    # Horizontal spit (Y axis) — large pan_y at zoom-out pushes display past 1.
    display = compute_zoom_display_split_position(
        DisplaySplitPositionRequest(
            widget_width=800,
            widget_height=600,
            image_width=800,
            image_height=400,
            split_visual=0.5,
            is_horizontal=True,
            zoom_level=0.25,
            pan_offset_x=0.0,
            pan_offset_y=3.0,
            content_rect=content,
        )
    )
    assert display is not None
    assert display == pytest.approx(1.25)


def test_split_view_transform_camera_anchors_when_zoomed_in():
    """At zoom > 1, rewrite content spit so screen spit stays fixed on pan."""
    from ui.canvas_infra.viewport.contract import (
        DisplaySplitPositionRequest,
        SplitPositionForViewTransformRequest,
    )
    from ui.canvas_infra.viewport.geometry import QuickContentRect
    from ui.canvas_infra.viewport.zoom import (
        compute_zoom_display_split_position,
        compute_zoom_split_position_for_view_transform,
    )

    content = QuickContentRect(x=0.0, y=0.0, width=200.0, height=100.0)
    store_split = 0.5
    updated = compute_zoom_split_position_for_view_transform(
        SplitPositionForViewTransformRequest(
            widget_width=200,
            widget_height=100,
            image_width=200,
            image_height=100,
            is_horizontal=False,
            split_position_visual=store_split,
            current_zoom=2.0,
            current_pan_x=0.0,
            current_pan_y=0.0,
            new_zoom=2.0,
            new_pan_x=0.1,
            new_pan_y=0.0,
            content_rect=content,
        )
    )
    assert updated is not None
    assert 0.0 <= updated <= 1.0

    display_before = compute_zoom_display_split_position(
        DisplaySplitPositionRequest(
            widget_width=200,
            widget_height=100,
            image_width=200,
            image_height=100,
            split_visual=store_split,
            is_horizontal=False,
            zoom_level=2.0,
            pan_offset_x=0.0,
            pan_offset_y=0.0,
            content_rect=content,
        )
    )
    display_after = compute_zoom_display_split_position(
        DisplaySplitPositionRequest(
            widget_width=200,
            widget_height=100,
            image_width=200,
            image_height=100,
            split_visual=updated,
            is_horizontal=False,
            zoom_level=2.0,
            pan_offset_x=0.1,
            pan_offset_y=0.0,
            content_rect=content,
        )
    )
    assert display_after == pytest.approx(display_before)


def test_split_view_transform_no_rewrite_when_zooming_out_to_fit():
    from ui.canvas_infra.viewport.contract import SplitPositionForViewTransformRequest
    from ui.canvas_infra.viewport.geometry import QuickContentRect
    from ui.canvas_infra.viewport.zoom import (
        compute_zoom_split_position_for_view_transform,
    )

    content = QuickContentRect(x=0.0, y=0.0, width=200.0, height=100.0)
    updated = compute_zoom_split_position_for_view_transform(
        SplitPositionForViewTransformRequest(
            widget_width=200,
            widget_height=100,
            image_width=200,
            image_height=100,
            is_horizontal=False,
            split_position_visual=0.5,
            current_zoom=2.0,
            current_pan_x=0.1,
            current_pan_y=0.0,
            new_zoom=0.5,
            new_pan_x=0.1,
            new_pan_y=0.0,
            content_rect=content,
        )
    )
    assert updated is None


def test_resize_gl_preserves_image_focus_when_letterbox_changes(monkeypatch):
    """Canvas resize must not preserve raw pan across letterbox changes."""
    import tabs.image_compare.canvas.render_context as render_context

    canvas = _canvas((0.10, 0.00, 0.80, 1.00))
    canvas.runtime_state._shader_letterbox_mode = True
    canvas.runtime_state._stored_pil_images = [object(), None]
    canvas.update = lambda: None
    set_zoom_level(canvas, 12.0)
    set_pan_offsets(canvas, 0.12, -0.04)

    focus = capture_letterbox_focus(canvas)
    monkeypatch.setattr(
        render_context,
        "registry",
        lambda: SimpleNamespace(
            has_feature_live_runtime_overlays=lambda: False,
            apply_feature_live_runtime_overlays=lambda store, canvas: False,
        ),
    )
    monkeypatch.setattr(
        render_context,
        "update_common_letterbox_geometry",
        lambda widget, img1, img2, **kwargs: widget.runtime_state._letterbox_params.__setitem__(
            0,
            (0.08, 0.02, 0.84, 0.96),
        ),
    )

    render_context.resize_gl(canvas, 1200, 900)

    assert capture_letterbox_focus(canvas) == focus

def test_resize_gl_recomputes_aspect_ratio_and_shared_interaction_rect(monkeypatch):
    """Window aspect changes must update both rendering and hit-test geometry."""
    import tabs.image_compare.canvas.render_context as render_context

    dimensions = {"width": 500, "height": 1000}
    image1 = Image.new("RGBA", (1000, 500), "red")
    image2 = Image.new("RGBA", (1000, 500), "blue")
    canvas = SimpleNamespace(
        runtime_state=SimpleNamespace(
            _shader_letterbox_mode=True,
            _stored_pil_images=[image1, image2],
            _letterbox_params=[(0.0, 0.0, 1.0, 1.0), (0.0, 0.0, 1.0, 1.0)],
            _content_rect_px=(0, 0, 1000, 500),
            _clip_overlays_to_content_rect=False,
        ),
        width=lambda: dimensions["width"],
        height=lambda: dimensions["height"],
        update=lambda: None,
    )
    monkeypatch.setattr(
        render_context,
        "registry",
        lambda: SimpleNamespace(
            has_feature_live_runtime_overlays=lambda: False,
            apply_feature_live_runtime_overlays=lambda store, canvas: False,
        ),
    )

    render_context.resize_gl(canvas, 500, 1000)

    expected = (0.0, 0.375, 1.0, 0.25)
    assert canvas.runtime_state._letterbox_params == [expected, expected]
    assert canvas.runtime_state._content_rect_px == (0, 375, 500, 250)
