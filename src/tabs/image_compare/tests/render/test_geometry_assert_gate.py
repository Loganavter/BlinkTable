"""The ``IMGSLI_GEOMETRY_ASSERT`` gate: arithmetic between the two rulers.

2026-11-18, Image Compare: the canvas painted the crop-aware basis (312x512)
while the divider/camera/drag ruler read ``runtime_state._content_rect_px``
fitted from the uncropped store (484x484 in the same frame). At split 0.5 both
rects are centred so it looked fine; at 0.25 the line sat ~60 px off the cut.
The check that found it was a subtraction, so it lives in the app now, behind
``IMGSLI_GEOMETRY_ASSERT`` — see
``canvas/presentation/geometry_assert.py``.

These tests pin the three invariants (basis aspect, rect-inside-widget,
divider-inside-rect), the flag's parsing, and the two properties that make the
gate safe to leave in place: it is inert when off, and it cannot raise or
change the rect at its one call site.

Offscreen only, no window: the pure functions need no Qt, and the call-site
widget is a plain ``SimpleNamespace``.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from tabs.image_compare.canvas.presentation import geometry_assert
from tabs.image_compare.canvas.presentation.geometry_assert import (
    TAG,
    check,
    enabled,
    report,
)

FLAG_NAMES = ("IMGSLI_GEOMETRY_ASSERT", "BLINKTABLE_GEOMETRY_ASSERT")


class _RecordingLogger:
    def __init__(self):
        self.calls: list[str] = []

    def warning(self, msg, *args):
        self.calls.append((msg % args) if args else msg)


@pytest.fixture
def clean_gate(monkeypatch):
    """Fresh de-dupe/throttle state per test (module globals by design)."""
    monkeypatch.setattr(geometry_assert, "_seen", set())
    monkeypatch.setattr(geometry_assert, "_last_emit", 0.0)


@pytest.fixture
def flag_on(monkeypatch):
    for name in FLAG_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("IMGSLI_GEOMETRY_ASSERT", "1")


@pytest.fixture
def flag_off(monkeypatch):
    for name in FLAG_NAMES:
        monkeypatch.delenv(name, raising=False)


def _letterbox(widget_w, widget_h, image_w, image_h):
    """The rect the single geometry resolver fits — a consistent frame."""
    from ui.canvas_infra.scene.frame_geometry import resolve_canvas_content_geometry

    geometry = resolve_canvas_content_geometry(
        widget_width=widget_w,
        widget_height=widget_h,
        image_width=image_w,
        image_height=image_h,
        virtual_layout=None,
    )
    assert geometry.inner_rect_px is not None
    return geometry.inner_rect_px


# --------------------------------------------------------------------------- #
# check() — the invariants
# --------------------------------------------------------------------------- #


def test_a_consistent_frame_has_nothing_to_say():
    rect = _letterbox(1024, 484, 312, 512)
    assert check(312, 512, 1024, 484, rect) == []


def test_the_crop_bug_names_both_rulers():
    """The exact measured frame: painted basis vs the uncropped square fit."""
    problems = check(392, 512, 1024, 484, (270, 0, 484, 484))

    assert len(problems) == 1, problems
    message = problems[0]
    assert "484x484" in message, message
    assert "392x512" in message, message
    assert "expected width" in message, message


def test_a_rect_outside_the_widget_is_named():
    # Matching aspect, so only the containment invariant can fire.
    problems = check(500, 300, 1024, 484, (600, 300, 500, 300))

    assert any("overflows the widget on the right" in p for p in problems), problems
    assert any("overflows the widget at the bottom" in p for p in problems), problems


def test_a_rect_before_the_widget_origin_is_named():
    problems = check(200, 200, 1024, 484, (-5, -3, 200, 200))

    assert any("left of the widget" in p for p in problems), problems
    assert any("above the widget" in p for p in problems), problems


def test_a_divider_outside_the_rect_span_is_named():
    problems = check(200, 200, 1024, 484, (100, 40, 200, 200), divider_px=900)

    assert len(problems) == 1, problems
    assert "outside the content rect" in problems[0], problems


def test_a_divider_inside_either_span_is_accepted():
    rect = (100, 40, 200, 200)
    assert check(200, 200, 1024, 484, rect, divider_px=250) == []  # inside x span
    assert check(200, 200, 1024, 484, rect, divider_px=150) == []  # inside y span


def test_one_pixel_rounding_is_not_a_mismatch():
    rect = _letterbox(1024, 484, 312, 512)
    x, y, w, h = rect
    assert check(312, 512, 1024, 484, (x, y, w + 1, h)) == []


def test_unreadable_input_is_a_message_not_an_exception():
    problems = check(312, 512, 1024, 484, (1, 2, 3))
    assert problems and "not four numbers" in problems[0], problems

    # A garbage basis is simply no basis: only readable invariants are checked.
    assert check("n/a", None, 1024, 484, (270, 0, 484, 484)) == []


# --------------------------------------------------------------------------- #
# enabled() — the flag
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", False),
        ("1", True),
        ("true", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("no", False),
        ("off", False),
    ],
)
def test_enabled_parses_the_flag(monkeypatch, value, expected):
    for name in FLAG_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("IMGSLI_GEOMETRY_ASSERT", value)

    assert enabled() is expected


def test_enabled_accepts_the_blinktable_spelling(monkeypatch):
    for name in FLAG_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BLINKTABLE_GEOMETRY_ASSERT", "1")

    assert enabled() is True


# --------------------------------------------------------------------------- #
# report() — inert, throttled, de-duplicated
# --------------------------------------------------------------------------- #


def test_report_touches_no_logger_when_disabled(flag_off, clean_gate):
    logger = _RecordingLogger()

    report(
        canvas_w=312,
        canvas_h=512,
        widget_w=1024,
        widget_h=484,
        content_rect=(270, 0, 484, 484),
        logger=logger,
    )

    assert logger.calls == []


def test_report_stays_quiet_on_a_consistent_frame(flag_on, clean_gate):
    logger = _RecordingLogger()

    report(
        canvas_w=312,
        canvas_h=512,
        widget_w=1024,
        widget_h=484,
        content_rect=_letterbox(1024, 484, 312, 512),
        logger=logger,
    )

    assert logger.calls == []


def test_report_throttles_and_de_duplicates(flag_on, clean_gate, monkeypatch):
    logger = _RecordingLogger()
    buggy = dict(canvas_w=392, canvas_h=512, widget_w=1024, widget_h=484,
                 content_rect=(270, 0, 484, 484))

    report(logger=logger, **buggy)
    report(logger=logger, **buggy)  # same frame again: no second line
    assert len(logger.calls) == 1, logger.calls
    assert logger.calls[0].startswith(TAG)

    # Past the throttle window a repeat is still deduplicated, not re-logged.
    monkeypatch.setattr(geometry_assert, "_last_emit", 0.0)
    report(logger=logger, **buggy)
    assert len(logger.calls) == 1, logger.calls

    # A second, different mismatch inside the throttle window waits its turn.
    outside = dict(canvas_w=200, canvas_h=200, widget_w=300, widget_h=300,
                   content_rect=(100, 100, 500, 500))
    monkeypatch.setattr(geometry_assert, "_last_emit", time.monotonic())
    report(logger=logger, **outside)
    assert len(logger.calls) == 1, logger.calls

    # Once the window passes it is emitted (and only once).
    monkeypatch.setattr(geometry_assert, "_last_emit", 0.0)
    report(logger=logger, **outside)
    report(logger=logger, **outside)
    assert len(logger.calls) == 2, logger.calls


# --------------------------------------------------------------------------- #
# The call site — read-only, and unable to raise
# --------------------------------------------------------------------------- #


def _canvas(rect, plan, inner=None):
    """A live-shaped canvas: the rect the divider reads plus the applied plan."""
    state = SimpleNamespace(
        _content_rect_px=rect,
        _inner_content_rect_px=inner,
        _zoom_viewport_state=None,
    )
    return SimpleNamespace(
        runtime_state=state,
        _active_render_plan=plan,
        width=lambda: 1024,
        height=lambda: 484,
    )


def test_the_call_site_returns_the_same_rect_with_the_flag_on_and_off(
    monkeypatch, flag_off
):
    from tabs.image_compare.canvas.render_config import (
        get_view_transformed_content_rect_widget_px,
    )

    rect = _letterbox(1024, 484, 312, 512)
    plan = SimpleNamespace(canvas_w=312, canvas_h=512, envelope_hold=False)
    widget = _canvas(rect, plan)

    without = get_view_transformed_content_rect_widget_px(widget)

    monkeypatch.setenv("IMGSLI_GEOMETRY_ASSERT", "1")
    logger = _RecordingLogger()
    monkeypatch.setattr(geometry_assert, "_logger", logger)

    with_flag = get_view_transformed_content_rect_widget_px(widget)

    assert with_flag == without
    assert logger.calls == [], "a consistent frame must stay silent"


def test_the_call_site_survives_a_broken_basis(monkeypatch, flag_off):
    from tabs.image_compare.canvas.render_config import (
        get_view_transformed_content_rect_widget_px,
    )

    rect = _letterbox(1024, 484, 312, 512)
    plan = SimpleNamespace(canvas_w="n/a", canvas_h=None, envelope_hold=False)
    widget = _canvas(rect, plan)

    without = get_view_transformed_content_rect_widget_px(widget)

    monkeypatch.setenv("IMGSLI_GEOMETRY_ASSERT", "1")
    logger = _RecordingLogger()
    monkeypatch.setattr(geometry_assert, "_logger", logger)

    assert get_view_transformed_content_rect_widget_px(widget) == without
    assert logger.calls == []


def test_the_call_site_tolerates_a_missing_plan(monkeypatch, flag_on):
    from tabs.image_compare.canvas.render_config import (
        get_view_transformed_content_rect_widget_px,
    )

    rect = _letterbox(1024, 484, 312, 512)
    widget = _canvas(rect, None)
    logger = _RecordingLogger()
    monkeypatch.setattr(geometry_assert, "_logger", logger)

    assert get_view_transformed_content_rect_widget_px(widget) is not None
    assert logger.calls == []


def test_the_call_site_reports_a_diverging_rect(monkeypatch, flag_on, clean_gate):
    from tabs.image_compare.canvas.render_config import (
        get_view_transformed_content_rect_widget_px,
    )

    plan = SimpleNamespace(canvas_w=392, canvas_h=512, envelope_hold=False)
    widget = _canvas((270, 0, 484, 484), plan)
    logger = _RecordingLogger()
    monkeypatch.setattr(geometry_assert, "_logger", logger)

    get_view_transformed_content_rect_widget_px(widget)

    assert len(logger.calls) == 1, logger.calls
    assert "392x512" in logger.calls[0]
    assert "484x484" in logger.calls[0]


def test_a_padded_frame_is_not_a_geometry_mismatch(monkeypatch, flag_on, clean_gate):
    """Feature padding makes the rect the image's; the basis carries the pads.

    ``compute_inner_content_rect`` narrows ``_inner_content_rect_px`` back to the
    image box for plans that own a padded framebuffer, so comparing its aspect
    with ``plan.canvas_*`` would warn on every padded frame. The gate must not
    do that — it only keeps the containment invariants there.
    """
    from tabs.image_compare.canvas.render_config import (
        get_view_transformed_content_rect_widget_px,
    )

    outer = (112, 0, 800, 484)
    inner = (212, 0, 600, 484)
    plan = SimpleNamespace(
        canvas_w=400, canvas_h=300, envelope_hold=False, geometry_letterbox=True
    )

    # The inner rect really does disagree with the basis — that is the point.
    assert check(400, 300, 1024, 484, inner) != []

    widget = _canvas(outer, plan, inner=inner)
    logger = _RecordingLogger()
    monkeypatch.setattr(geometry_assert, "_logger", logger)

    get_view_transformed_content_rect_widget_px(widget)

    assert logger.calls == []
