"""Settings minimum size must bracket the right column's content.

Owner report (2026-10-28), two defects:

1. "The content on the right is practically not taken into account in the
   window's minimum size — it is not authored for a 50-60 px strip." The
   floor was the bare design constant (300 px) while ``QScrollArea``'s own
   ``minimumSizeHint`` is a content-independent 68x68, so the shell could be
   shrunk until the right column was 147 px wide and every page needed a
   horizontal scrollbar.
2. "On dpr 1.25 the line separating the left and right sections overlaps the
   leftmost stroke of the control groups on the right." The app half of that
   is the column's own gap: ``SidebarDialogShell`` resolves ``content_margins``
   once, at construction, so a live interface-scale change left the padding at
   design px while everything inside the column scaled. (The divider line
   itself — splitter handle width and DPR snapping — lives in the toolkit and
   is fixed there, not here.)

Falsification story: every assertion below is RED against the pre-fix modules.
``dialog.minimumWidth()`` was 300 while the widest page needs ~788 px of shell,
the General page showed a horizontal scrollbar at the minimum size, and the
column padding stayed at 8 px after ``UiScale.set_factor(1.25)`` instead of
following the factor to 10.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QScrollArea

from plugins.settings.dialog import SettingsDialog
from plugins.settings.layout_geometry import (
    _content_minimum_window_width,
    settings_minimum_floor,
)
from sli_ui_toolkit.managers import UiScale, scaled_px
from tests.helpers.drain_until_stable import drain_until_stable
from ui.layout_spacing import control_edge_padding

_SCALE = 1.25
#: A 720p screen is 1280 px wide; the window minimum must stay under it.
_WIDE_SCREEN_PX = 1280


@pytest.fixture(autouse=True)
def _restore_ui_scale():
    """``UiScale`` is process-wide — never leak a factor to other modules."""
    yield
    UiScale.get_instance().set_factor(1.0)


@pytest.fixture(autouse=True)
def _no_remembered_sizes(monkeypatch):
    """A remembered window size would mask the floor under test."""
    monkeypatch.setattr(
        "shared_toolkit.ui.layout_sizing._load_remembered_size",
        lambda _key: None,
    )


def _build_dialog() -> SettingsDialog:
    # settings.* keys live in the plugin pack; register it so the pages lay
    # out real labels instead of raw key strings (raw keys are much wider and
    # turn this measurement into a translation artifact).
    from resources.translations import add_i18n_root

    add_i18n_root(Path("src/plugins/settings/resources/i18n"))
    return SettingsDialog(
        current_language="en",
        current_theme="dark",
        current_max_length=30,
        min_limit=1,
        max_limit=200,
        debug_mode_enabled=False,
        system_notifications_enabled=True,
        active_tab="image_compare",
    )


def _page_metrics(dialog: SettingsDialog, index: int):
    wrapper = dialog.pages_stack.widget(index)
    scroll_area = wrapper.findChild(QScrollArea)
    content = scroll_area.widget()
    return scroll_area, content


def _show_page_at_minimum(dialog: SettingsDialog, app: QApplication, index: int):
    # A hidden scroll area never lays its content out, so its scroll ranges
    # stay 0 (the scaled minimum is measured on show, not on ``resize``).
    if not dialog.isVisible():
        dialog.show()
        app.processEvents()
    dialog.pages_stack.setCurrentIndex(index)
    minimum = dialog.minimumSize()
    dialog.resize(minimum.width(), minimum.height())
    app.processEvents()
    return _page_metrics(dialog, index)


def _scrolls(scroll_area: QScrollArea, *, vertical: bool) -> bool:
    """True when that axis actually overflows the viewport.

    Range, not ``isVisible()``: a hidden dialog's scrollbar widgets report
    ``isVisible() == False`` regardless of their range (and the toolkit's
    minimalist bar draws itself), but ``maximum() > 0`` is the same fact the
    scrollbar wrapper acts on.
    """
    bar = (
        scroll_area.verticalScrollBar() if vertical else scroll_area.horizontalScrollBar()
    )
    return bar.maximum() > 0


def test_minimum_width_brackets_the_widest_page_content(qapp):
    dialog = _build_dialog()
    try:
        content_floor = _content_minimum_window_width(dialog)
        available_width = dialog.screen().availableGeometry().width()

        # The content really is the deciding term (a design floor is 300 px).
        assert content_floor > scaled_px(300)
        # ... and it reaches the window, capped only by the monitor.
        assert dialog.minimumWidth() == min(content_floor, available_width)
        # Height stays a resizability floor: pages are taller than any
        # sensible window and the vertical scrollbar is the normal answer.
        assert dialog.minimumHeight() == scaled_px(200)
        assert settings_minimum_floor(dialog)[1] == scaled_px(200)
    finally:
        dialog.deleteLater()


def test_narrow_window_needs_only_a_vertical_scrollbar(qapp):
    dialog = _build_dialog()
    try:
        # Page 0 ("General") is an ordinary page (its content is narrower than
        # the widest page), so at the window minimum it must not clip.
        scroll_area, content = _show_page_at_minimum(dialog, qapp, 0)
        assert _scrolls(scroll_area, vertical=True), (
            "a short window is expected to scroll the page vertically"
        )
        assert not _scrolls(scroll_area, vertical=False), (
            "the minimum window width still clips the right column: "
            f"viewport {scroll_area.viewport().width()} < content "
            f"{content.sizeHint().width()}"
        )
    finally:
        dialog.deleteLater()


def test_every_page_fits_the_minimum_width(qapp):
    dialog = _build_dialog()
    try:
        content_floor = _content_minimum_window_width(dialog)
        available_width = dialog.screen().availableGeometry().width()
        if content_floor > available_width:
            pytest.skip(
                "the monitor is narrower than the content floor, so a "
                "horizontal scrollbar is the documented fallback"
            )
        for index in range(dialog.pages_stack.count()):
            scroll_area, content = _show_page_at_minimum(dialog, qapp, index)
            assert not _scrolls(scroll_area, vertical=False), (
                f"page {index} needs a horizontal scrollbar at the window "
                f"minimum (viewport {scroll_area.viewport().width()} < "
                f"content {content.sizeHint().width()})"
            )
    finally:
        dialog.deleteLater()


def test_content_floor_fits_a_720p_screen(qapp):
    """No page's hard-coded control width may blow the minimum past 1280 px."""
    dialog = _build_dialog()
    try:
        content_floor = _content_minimum_window_width(dialog)
        assert content_floor <= scaled_px(_WIDE_SCREEN_PX), (
            "a page's fixed control width makes the window minimum wider than "
            f"a 720p screen — content floor {content_floor}px"
        )
    finally:
        dialog.deleteLater()


def test_content_padding_follows_a_live_scale_change(qapp):
    """The PAGES keep the design padding; the footer rule row does not.

    Since the footer rule spans the content column edge to edge (owner request
    2026-11-04), the column itself is flush and the padding moved to the pages'
    host — the assertion follows the padding rather than the old layout.
    """
    dialog = _build_dialog()
    try:
        pages = dialog.shell.pages_host.layout()
        assert pages.contentsMargins().left() == control_edge_padding()
        # the rule's own row stays flush, which is what makes it reach both edges
        assert dialog.content_layout.contentsMargins().left() == 0
        assert dialog.content_layout.contentsMargins().right() == 0

        UiScale.get_instance().set_factor(_SCALE)
        drain_until_stable(
            qapp,
            lambda: pages.contentsMargins().left(),
            timeout_ms=1000,
            poll_ms=10,
            stable_frames=2,
        )

        assert pages.contentsMargins().left() == control_edge_padding(), (
            "the right column's padding stayed at design px after a live "
            "scale change — the divider then sits against the control groups"
        )
        assert pages.contentsMargins().left() == scaled_px(8)
    finally:
        dialog.deleteLater()
