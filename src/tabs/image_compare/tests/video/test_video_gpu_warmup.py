"""The one-shot GPU warm-up must cover the offscreen service the exporter owns.

The video exporter owns one offscreen QRhiWidget (preview + export + the
notification preview all render through it). Until strip atlas phase 3 it
owned a second one for 160x90 filmstrip tiles; that per-frame thumbnail
pipeline is deleted, so the warm-up has exactly one service to reach. These
tests pin that the call reaches it, that an unready or raising service keeps
the caller's retry alive, and that an absent service is not invented. The
cold cost itself is a ~4 s D3D shader/pipeline compile on the Windows build
(measured 2026-10-24), paid on the startup timer instead of inside the user's
first preview request.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

_APP = None


def _app() -> QApplication:
    global _APP
    _APP = QApplication.instance() or _APP or QApplication([])
    return _APP


class _SpyService:
    """Stand-in for ``GpuExportService``; counts warm-up calls, reports ready."""

    def __init__(self, ready=True, error=None):
        self.calls = 0
        self.ready = ready
        self.error = error

    def warm_up(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.ready


def _exporter(gpu_export_service=None):
    _app()
    from tabs.image_compare.plugins.video_editor.services.video_export.service import (
        VideoExporterService,
    )

    store = SimpleNamespace(settings=SimpleNamespace(export_default_dir="/tmp"))
    return VideoExporterService(
        recorder=SimpleNamespace(snapshots=[]),
        store=store,
        main_controller=None,
        gpu_export_service=gpu_export_service,
    )


def test_warm_up_reaches_the_offscreen_service():
    main = _SpyService()
    exporter = _exporter(gpu_export_service=main)

    assert exporter.warm_up_gpu_widgets() is True
    assert main.calls == 1, (
        "the offscreen service was left cold: the first preview/export render "
        "then pays the ~4 s shader/pipeline compile"
    )


def test_warm_up_fails_when_the_service_is_not_ready():
    main = _SpyService(ready=False)
    exporter = _exporter(gpu_export_service=main)

    assert exporter.warm_up_gpu_widgets() is False, (
        "a cold widget must keep the caller's one-shot retry alive"
    )
    assert main.calls == 1


def test_warm_up_survives_a_service_raising():
    main = _SpyService(error=RuntimeError("no canvas provider"))
    exporter = _exporter(gpu_export_service=main)

    assert exporter.warm_up_gpu_widgets() is False
    assert main.calls == 1


def test_absent_service_is_not_warmed_and_reports_failure():
    exporter = _exporter(gpu_export_service=None)

    assert exporter.warm_up_gpu_widgets() is False, (
        "with no offscreen service there is nothing ready, so the caller must "
        "not record the warm-up as done"
    )


def test_a_service_without_warm_up_is_not_a_crash():
    exporter = _exporter(gpu_export_service=object())

    assert exporter.warm_up_gpu_widgets() is False
