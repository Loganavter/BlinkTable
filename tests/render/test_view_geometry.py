"""``shared.image_processing.view_geometry`` — the "save view" crop contract.

Pins the inverse of the canvases' forward zoom/pan form
(``s = (c - 0.5 + pan) * zoom + 0.5``): the region a save-view export writes at
native density, and the invariants a caller may rely on (inside the canvas,
full canvas when nothing is zoomed, no dependence on pan when zoom <= 1).
"""

from __future__ import annotations

import pytest

from shared.image_processing.view_geometry import (
    view_native_size,
    visible_view_rect,
    visible_view_uv,
)


def _forward(c: float, zoom: float, pan: float) -> float:
    """The canvases' forward mapping, verbatim (shader form)."""
    return (c - 0.5 + pan) * zoom + 0.5


def test_zoom_one_is_the_whole_canvas_whatever_the_pan() -> None:
    for pan in (-0.4, 0.0, 0.25):
        assert visible_view_uv(1.0, pan, pan) == (0.0, 0.0, 1.0, 1.0)
        assert visible_view_rect(1000, 600, 1.0, pan, pan) == (0, 0, 1000, 600)


def test_zoom_below_one_is_the_whole_canvas() -> None:
    # Zooming out shows letterbox padding, never more content: an export must
    # not invent pixels around the canvas.
    assert visible_view_rect(800, 400, 0.5, 0.0, 0.0) == (0, 0, 800, 400)


def test_zoom_two_centered_is_the_middle_quarter() -> None:
    assert visible_view_rect(1000, 600, 2.0, 0.0, 0.0) == (250, 150, 500, 300)


def test_rect_is_the_inverse_of_the_forward_shader_form() -> None:
    zoom, pan = 3.5, 0.07
    left, top, width, height = visible_view_uv(zoom, pan, -pan)
    assert _forward(left, zoom, pan) == pytest.approx(0.0, abs=1e-12)
    assert _forward(left + width, zoom, pan) == pytest.approx(1.0, abs=1e-12)
    assert _forward(top, zoom, -pan) == pytest.approx(0.0, abs=1e-12)
    assert _forward(top + height, zoom, -pan) == pytest.approx(1.0, abs=1e-12)


def test_pan_moves_the_window_and_stops_at_the_canvas_edge() -> None:
    # Pan to the limit: the window hugs the left edge, never leaving the frame.
    assert visible_view_rect(1000, 600, 2.0, 0.25, 0.0) == (0, 150, 500, 300)
    assert visible_view_rect(1000, 600, 2.0, -0.25, 0.0) == (500, 150, 500, 300)
    # Beyond the limit is the same window (clamped), not a void strip:
    # +pan_x hugs the left edge, -pan_y hugs the bottom edge.
    assert visible_view_rect(1000, 600, 2.0, 9.0, -9.0) == (0, 300, 500, 300)


@pytest.mark.parametrize("canvas", [(999, 601), (1001, 599), (17, 5), (4000, 3001)])
@pytest.mark.parametrize("zoom", [1.0, 1.3, 2.0, 7.0, 50.0])
def test_rect_always_stays_inside_the_canvas(canvas, zoom) -> None:
    cw, ch = canvas
    for pan in (-1.0, -0.31, 0.0, 0.13, 1.0):
        left, top, width, height = visible_view_rect(cw, ch, zoom, pan, -pan)
        assert width >= 1 and height >= 1
        assert 0 <= left and left + width <= cw
        assert 0 <= top and top + height <= ch


def test_native_size_follows_the_rect() -> None:
    _left, _top, width, height = visible_view_rect(1200, 800, 4.0, 0.1, 0.0)
    assert view_native_size(1200, 800, 4.0, 0.1, 0.0) == (width, height)
    # 4x zoom: a quarter of each side, shifted left by the positive pan.
    assert (width, height) == (300, 200)
    assert visible_view_rect(1200, 800, 4.0, 0.1, 0.0)[0] == 330


def test_bad_inputs_fail_open_to_the_whole_canvas() -> None:
    assert visible_view_rect(640, 480, "nonsense", object(), None) == (0, 0, 640, 480)
    assert visible_view_rect(0, 0, 2.0, 0.0, 0.0) == (0, 0, 1, 1)
