"""Regression: tab-contributed settings extras are reachable by keyboard.

Before the NavRowBuilder migration (Phase 4 of plan_navigation_descriptor_unification),
``build_image_perf_extras`` added widgets straight to the layout via
``add_layout()`` — they never entered the page's nav-row list and were
permanently unreachable by Up/Down.  This test asserts the fix: extras now
return nav-row widgets that integrate with ``NavRowBuilder.extend()``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from PySide6.QtWidgets import QWidget

from sli_ui_toolkit.managers import NavRowBuilder
from tabs.image_compare.ui.settings_performance import build_image_perf_extras


def _make_dialog():
    """Minimal mock dialog满足 build_image_perf_extras 的需求."""
    dialog = MagicMock()
    dialog._perf_layout = MagicMock()
    dialog.current_language = "en"
    dialog._tr = lambda text, *a, **kw: text
    return dialog


def _make_prefs(**overrides):
    """Minimal mock preferences context."""
    defaults = dict(
        optimize_magnifier_movement=False,
        optimize_laser_smoothing=False,
        magnifier_intersection_highlight_enabled=False,
        magnifier_auto_color_new_instances=False,
        zoom_interpolation_method="NEAREST",
        movement_interpolation_method="BILINEAR",
        current_video_fps=60,
        store=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_extras_return_nav_rows(qtbot):
    """build_image_perf_extras must return a non-empty list of QWidgets."""
    dialog = _make_dialog()
    p = _make_prefs()
    rows = build_image_perf_extras(dialog, p)
    assert isinstance(rows, list)
    assert len(rows) > 0, (
        "extras should return at least one nav row — the old add_layout() "
        "path returned None and silently dropped rows from navigation"
    )
    for row in rows:
        assert isinstance(row, QWidget), f"expected QWidget, got {type(row)}"


def test_extras_rows_integrate_with_nav_row_builder(qtbot):
    """Rows from extras, fed into NavRowBuilder.extend(), appear in the section."""
    dialog = _make_dialog()
    p = _make_prefs()
    extra_rows = build_image_perf_extras(dialog, p)

    builder = NavRowBuilder(tag="test-extras")
    before = builder.row(QWidget())  # one row before extras
    builder.extend(extra_rows)
    after = builder.row(QWidget())   # one row after extras
    section = builder.build()

    section_rows = section._rows_provider()
    # The extras rows must be sandwiched between `before` and `after`.
    assert section_rows[0] is before
    assert section_rows[-1] is after
    # All extras rows appear in the section, in their original order.
    extras_in_section = section_rows[1:-1]
    assert extras_in_section == extra_rows
