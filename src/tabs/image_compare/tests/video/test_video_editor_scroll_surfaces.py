"""Video editor scroll surfaces use the toolkit SurfaceScrollArea."""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QWidget

from sli_ui_toolkit.widgets import SurfaceScrollArea

from tabs.image_compare.plugins.video_editor.dialog.sections import (
    _wrap_tab_scroll,
    create_timeline_scroll_area,
)

#: Frames enough to overflow the narrow timeline viewport below.
_FRAMES = 121
_FPS = 10


def test_tab_scroll_uses_surface_scroll_area_token_mode(qapp):
    """Tab panes paint nothing — the scroll area provides the surface fill."""
    content = QWidget()
    tab = _wrap_tab_scroll(content)
    scroll = tab.findChild(SurfaceScrollArea)
    assert scroll is not None
    assert scroll.widget() is content
    assert scroll._surface_token == "surface.background"
    tab.deleteLater()


def test_timeline_scroll_uses_surface_scroll_area_token_mode(qapp):
    dialog = SimpleNamespace(_on_head_moved=lambda *a: None, _on_trim_clicked=lambda *a: None, _on_play_pause_requested=lambda: None)
    scroll = create_timeline_scroll_area(dialog)
    try:
        assert isinstance(scroll, SurfaceScrollArea)
        assert scroll._surface_token == "surface.background"
        assert scroll.minimumHeight() > 0
        assert dialog.timeline is not None
        assert scroll.widget() is dialog.timeline
    finally:
        scroll.deleteLater()


def _settle(qapp, ms=300):
    """Let the timeline's layout settle: its width comes from a settle tick."""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    qapp.processEvents()


def _overflowing_timeline(qapp, *, width=400, height=200):
    """The dialog's own timeline scroll area, shown, with a horizontal overflow.

    Built through ``create_timeline_scroll_area`` (the shape the dialog uses),
    then zoomed well past the viewport so the native horizontal axis really
    overflows — the assertion that its range is non-zero is what keeps the
    overlay-visibility checks from passing vacuously.
    """
    dialog = SimpleNamespace(
        _on_head_moved=lambda *a: None,
        _on_trim_clicked=lambda *a: None,
        _on_play_pause_requested=lambda: None,
    )
    scroll = create_timeline_scroll_area(dialog)
    scroll.resize(width, height)
    scroll.show()
    qapp.processEvents()
    snapshots = [
        SimpleNamespace(timestamp=index / _FPS, frame_index=index)
        for index in range(_FRAMES)
    ]
    timeline = dialog.timeline
    timeline.set_data(snapshots, fps=_FPS, duration=(_FRAMES - 1) / _FPS)
    timeline.fit_view()
    timeline.set_zoom(8.0)
    timeline.update_layout_width()
    _settle(qapp)
    return scroll, timeline


def test_timeline_scroll_owns_the_horizontal_axis(qapp):
    """One horizontal control: the timeline's footer bar, not the host thumb.

    plan-timeline-scrollbar-authority-2026-11-04 (owner decision 2026-11-06):
    the timeline draws its own footer bar for this axis, so the host's overlay
    thumb must never appear and must reserve no bottom margin — while the
    native bar keeps driving the timeline.
    """
    scroll, timeline = _overflowing_timeline(qapp)
    try:
        bar = scroll.horizontalScrollBar()
        assert bar.maximum() > bar.minimum(), "the horizontal axis must overflow"
        assert timeline.minimumWidth() > scroll.viewport().width()

        assert scroll.horizontal_scrollbar_enabled() is False
        assert not scroll.custom_h_scrollbar.isVisible(), (
            "the host overlay thumb duplicates the timeline's footer bar"
        )
        assert scroll.viewportMargins().bottom() == 0, (
            "a hidden overlay bar must not reserve a bottom margin"
        )
        assert scroll.overlay_horizontal_scrollbar_inset() == 0

        # The native scrollbar is still the scrolling mechanism: same range,
        # and moving it moves the timeline.
        bar.setValue(bar.maximum())
        qapp.processEvents()
        assert bar.value() == bar.maximum()
        assert timeline.x() == -bar.value()
    finally:
        scroll.close()
        scroll.deleteLater()


def test_timeline_scroll_keeps_the_vertical_overlay_thumb(qapp):
    """Only the horizontal axis is host-owned; the vertical thumb stays."""
    scroll, timeline = _overflowing_timeline(qapp)
    try:
        assert not scroll.custom_v_scrollbar.isVisible(), "fits vertically at first"

        # Force the other axis to overflow: the vertical thumb must still
        # appear (and reserve its own clearance) with the h switch off.
        timeline.setMinimumHeight(900)
        _settle(qapp)

        vbar = scroll.verticalScrollBar()
        assert vbar.maximum() > vbar.minimum(), "the vertical axis must overflow"
        assert scroll.custom_v_scrollbar.isVisible()
        assert scroll.viewportMargins().right() > 0
        assert scroll.viewportMargins().bottom() == 0
        assert not scroll.custom_h_scrollbar.isVisible()
    finally:
        scroll.close()
        scroll.deleteLater()