from __future__ import annotations

import logging
import os
import tempfile

from core.events import CoreErrorOccurredEvent
from core.log_names import APP_LOGGER_NAME
from sli_ui_toolkit.i18n import get_current_language, tr
from sli_ui_toolkit.workers import GenericWorker

logger = logging.getLogger(APP_LOGGER_NAME)


def _schedule_temp_delete(path: str | None, delay_ms: int = 60000) -> None:
    """Best-effort delayed unlink of a temp preview file (fail-open).

    The notify path used to reference this helper without defining it
    (NameError on success-with-preview). Deletion failure never blocks
    the notification — the OS temp cleaner reaps leftovers.
    """
    if not path:
        return

    def _unlink() -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

    try:
        from PySide6.QtCore import QTimer

        QTimer.singleShot(max(0, int(delay_ms)), _unlink)
    except Exception:
        try:
            import threading

            timer = threading.Timer(max(0.0, delay_ms / 1000.0), _unlink)
            timer.daemon = True
            timer.start()
        except Exception:
            pass


def build_video_notification_preview(
    video_exporter,
    frames,
    resolution,
    options=None,
) -> str | None:
    try:
        if video_exporter is None:
            return None

        recording = (
            frames
            if hasattr(frames, "evaluate_at")
            else video_exporter._coerce_recording(frames)
        )
        if recording is None:
            return None

        snapshot = recording.evaluate_at(0.0)
        export_options = dict(options or {})
        fit_content = bool(export_options.get("fit_content", False))
        fill_rgba = (0, 0, 0, 0)
        fill_color = export_options.get("fit_content_fill_color")
        if isinstance(fill_color, str) and fill_color:
            from PySide6.QtGui import QColor

            qcolor = QColor(fill_color)
            fill_rgba = (
                qcolor.red(),
                qcolor.green(),
                qcolor.blue(),
                qcolor.alpha(),
            )

        global_bounds = None
        if fit_content:
            # GUI-thread-safe read only: get_cached_global_bounds()
            # recomputes synchronously on a hash miss (decode/measure
            # work) and must never run on the notify path's thread.
            # Peek here; a None peek renders without bounds (fail-open)
            # instead of blocking — the background recalculate worker
            # republishes via store_cached_global_bounds.
            peek = getattr(video_exporter, "peek_cached_global_bounds", None)
            if callable(peek):
                try:
                    global_bounds = peek()
                except Exception:
                    global_bounds = None
            else:
                try:
                    global_bounds = video_exporter.get_cached_global_bounds(
                        recording, False
                    )
                except Exception:
                    global_bounds = None

        frame = video_exporter.render_snapshot_to_pil(
            snapshot,
            resolution[0],
            resolution[1],
            auto_crop=False,
            fit_content=fit_content,
            global_bounds=global_bounds,
            fill_color=fill_rgba,
        )
        if frame is None:
            return None

        max_dim = 256
        frame = frame.convert("RGBA")
        width, height = frame.size
        if max(width, height) > max_dim:
            scale = max_dim / max(width, height)
            frame = frame.resize((int(width * scale), int(height * scale)))

        thumb_file = tempfile.NamedTemporaryFile(
            prefix="imgsli_video_thumb_",
            suffix=".png",
            delete=False,
        )
        thumb_path = thumb_file.name
        thumb_file.close()
        frame.save(thumb_path, format="PNG")
        _schedule_temp_delete(thumb_path, delay_ms=60000)
        return thumb_path
    except Exception:
        logger.warning(
            "video export preview frame generation failed; continuing without thumbnail",
            exc_info=True,
        )
        return None


class VideoExportFlow:
    def __init__(self, controller):
        self.controller = controller

    @staticmethod
    def normalize_even_resolution(resolution: tuple[int, int]) -> tuple[int, int]:
        width, height = resolution
        safe_width = width if width % 2 == 0 else max(2, width - 1)
        safe_height = height if height % 2 == 0 else max(2, height - 1)
        return safe_width, safe_height

    def export_video_from_editor(
        self,
        frames,
        fps,
        resolution=(1920, 1080),
        options=None,
    ):
        controller = self.controller
        safe_resolution = self.normalize_even_resolution(resolution)
        log_emit = None
        if hasattr(controller, "main_controller") and hasattr(
            controller.main_controller,
            "video_export_log",
        ):
            log_emit = controller.main_controller.video_export_log.emit

        def export_task(progress_callback):
            return controller.video_exporter.export_recorded_video(
                safe_resolution,
                fps,
                snapshots_override=frames,
                export_options=options,
                progress_callback=progress_callback,
                log_callback=log_emit,
            )

        worker = GenericWorker(export_task)
        export_completed = {"signaled": False}

        if hasattr(controller, "main_controller") and hasattr(
            controller.main_controller,
            "video_export_progress",
        ):
            worker.signals.progress.connect(
                controller.main_controller.video_export_progress.emit
            )
            worker.kwargs["progress_callback"] = worker.signals.progress

        def on_success(path):
            export_completed["signaled"] = True
            if path:
                logger.info("Video export finished: %s", path)
                if hasattr(controller, "main_controller") and hasattr(
                    controller.main_controller,
                    "video_export_finished",
                ):
                    controller.main_controller.video_export_finished.emit(True)

                if controller.presenter and hasattr(
                    controller.presenter.main_window_app,
                    "actions",
                ):
                    # The notification preview is a full-res render — it
                    # must not run on the GUI thread (this slot fires via
                    # the worker result signal). Build it on the shared
                    # thread pool; the result signal marshals the notify
                    # back to the GUI thread. No pool → notify imageless
                    # immediately (fail-open) rather than blocking.
                    pool = getattr(controller, "thread_pool", None)
                    if pool is None or not hasattr(pool, "start"):
                        self._notify_export_success(path, None)
                    else:
                        try:
                            preview_worker = GenericWorker(
                                build_video_notification_preview,
                                controller.video_exporter,
                                frames,
                                safe_resolution,
                                options,
                            )
                            preview_worker.signals.result.connect(
                                lambda preview, p=path: self._notify_export_success(
                                    p, preview
                                )
                            )
                            preview_worker.signals.error.connect(
                                lambda _err, p=path: self._notify_export_success(
                                    p, None
                                )
                            )
                            pool.start(preview_worker)
                        except Exception:
                            logger.warning(
                                "video notify preview dispatch failed; "
                                "notifying without thumbnail",
                                exc_info=True,
                            )
                            self._notify_export_success(path, None)
            else:
                self._emit_export_finished(False)

        def on_error(err):
            export_completed["signaled"] = True
            logger.error("Video export failed: %s", err)
            self._emit_export_failed(f"Video export failed: {err}")
            self._emit_export_finished(False)

        def on_finished():
            if not export_completed["signaled"]:
                self._emit_export_finished(False)

        worker.signals.result.connect(on_success)
        worker.signals.error.connect(on_error)
        worker.signals.finished.connect(on_finished)
        controller.thread_pool.start(worker)

    def cancel_video_export(self) -> None:
        """Single export cancel path (ve-p3, X5): forward to the exporter's
        lock-guarded request_cancel() (which also runs cleanup); the render
        loop polls is_cancel_requested(). Thread-safe in the service."""
        controller = self.controller
        if controller.video_exporter is not None:
            controller.video_exporter.request_cancel()

    def is_cancel_requested(self) -> bool:
        """Single export cancel query (ve-p3, X5): read-through to the
        exporter's lock-guarded flag; False when no exporter is attached."""
        controller = self.controller
        exporter = getattr(controller, "video_exporter", None)
        if exporter is None:
            return False
        return bool(exporter.is_cancel_requested())

    def _notify_export_success(self, path: str, preview: str | None) -> None:
        """GUI-thread notify for a finished export (preview may be None).

        Runs on the GUI thread — either directly (no thread pool) or via
        the preview worker's result signal. Never renders; the preview
        file (if any) was built off-thread by
        :func:`build_video_notification_preview`.
        """
        controller = self.controller
        try:
            lang = get_current_language()
            try:
                store_lang = getattr(
                    getattr(controller.main_controller, "store", None),
                    "settings",
                    None,
                )
                if store_lang is not None:
                    lang = getattr(store_lang, "current_language", lang) or lang
            except Exception:
                pass
            title = tr("video.video_exported", lang)
            if title == "video.video_exported":
                title = "Video Exported"
            saved_tpl = tr("video.saved_to", lang)
            if saved_tpl == "video.saved_to":
                saved_tpl = "Saved to: {path}"
            try:
                msg = (
                    saved_tpl.format(path=path)
                    if "{path}" in saved_tpl
                    else f"{saved_tpl} {path}"
                )
            except Exception:
                msg = f"Saved to: {path}"
            # ``MainWindow.action_registry`` is the action owner (window.py:88);
            # ``window.actions`` is ``QWidget.actions()`` (a builtin method), so
            # that spelling raises AttributeError and the exception below hides it.
            controller.presenter.main_window_app.action_registry.notify_system(
                title,
                msg,
                image_path=preview,
            )
        except Exception:
            logger.warning(
                "video export success notification failed",
                exc_info=True,
            )
        if preview:
            _schedule_temp_delete(preview, delay_ms=10000)

    def _emit_export_finished(self, success: bool) -> None:
        controller = self.controller
        if hasattr(controller, "main_controller") and hasattr(
            controller.main_controller,
            "video_export_finished",
        ):
            controller.main_controller.video_export_finished.emit(success)

    def _emit_export_failed(self, message: str) -> None:
        controller = self.controller
        if controller.event_bus:
            controller.event_bus.emit(CoreErrorOccurredEvent(message))
        else:
            controller.error_occurred.emit(message)
