"""Two runtime defects the suite could not see (2026-11-04).

Both were invisible for the same reason: the tests around them use doubles that
accept anything. These pin the REAL signatures/state instead.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def test_the_preview_calls_the_real_exporter_signature():
    """``prepare_snapshot_canvas_frame`` has no ``thumbnail`` parameter.

    #64 retired the per-frame thumbnail pipeline and the exporter's thumbnail
    mode with it; the preview kept passing ``thumbnail=False``, so every
    preview frame raised TypeError inside the worker. A ``spec=`` mock rejects
    unknown kwargs exactly like the real method, which is what every other test
    double failed to do.
    """
    from tabs.image_compare.plugins.video_editor.presenter_parts import preview
    from tabs.image_compare.plugins.video_editor.services.video_export.service import (
        VideoExporterService,
    )

    exporter = MagicMock(spec=VideoExporterService)
    owner = SimpleNamespace(fit_content_mode=False)
    preview.PreviewCoordinator._prepare_preview_frame(
        owner,
        exporter,
        SimpleNamespace(frame_index=0),
        320,
        180,
        None,
        (0, 0, 0, 0),
    )

    kwargs = exporter.prepare_snapshot_canvas_frame.call_args.kwargs
    assert "thumbnail" not in kwargs
    assert kwargs["fill_color"] == (0, 0, 0, 0)


def test_the_canvas_state_declares_the_letterbox_refresh_flag():
    """The resize path writes the flag on a slots dataclass.

    Writing an undeclared attribute raised AttributeError on every
    QRhiWidget::resizeEvent (swallowed by Qt's override boundary), so the PIL
    letterbox refresh never ran from the resize path.
    """
    from tabs.image_compare.canvas.state import CanvasRuntimeState

    state = CanvasRuntimeState()
    assert hasattr(state, "_pil_letterbox_refresh_scheduled")
    assert state._pil_letterbox_refresh_scheduled is False
    state._pil_letterbox_refresh_scheduled = True
    assert state._pil_letterbox_refresh_scheduled is True


def test_the_resize_scheduler_sets_and_clears_the_flag():
    """The scheduler's own guard works once the field exists."""
    from tabs.image_compare.canvas import render_context

    scheduled: list = []
    widget = SimpleNamespace(
        runtime_state=SimpleNamespace(
            _pil_letterbox_refresh_scheduled=False,
            _store=None,
        )
    )

    class _Timer:
        @staticmethod
        def singleShot(_ms, callback):
            scheduled.append(callback)

    # Patch the module's own name: it did ``from PySide6.QtCore import QTimer``.
    original = render_context.QTimer
    render_context.QTimer = _Timer
    try:
        render_context._schedule_pil_letterbox_refresh(widget)
    finally:
        render_context.QTimer = original

    assert widget.runtime_state._pil_letterbox_refresh_scheduled is True
    assert len(scheduled) == 1
