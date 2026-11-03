# Audit-Meta: pattern=thin-owner reason="video editor presenter — dialog/timeline signal connect/disconnect mirror plus coordinator ownership"
import logging
import time
import warnings

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QPixmap

from tabs.image_compare.plugins.video_editor.model import VideoProjectModel
from tabs.image_compare.plugins.video_editor.presenter_parts import (
    ExportCoordinator,
    OutputPathCoordinator,
    PlaybackCoordinator,
    PreviewCoordinator,
    StripBandHost,
    ThumbnailCoordinator,
    initialize_editor_from_snapshots,
    resolve_initial_fps,
)
from tabs.image_compare.plugins.video_editor.services.editor import VideoEditorService
from tabs.image_compare.plugins.video_editor.services.playback import PlaybackEngine
from tabs.image_compare.plugins.video_editor.services.thumbnails import ThumbnailService
from core.log_names import APP_LOGGER_NAME


logger = logging.getLogger(APP_LOGGER_NAME)


def pipeline_cache_thumbnail_resolver_for(main_controller):
    """Thumbnail proxy resolver peeking the warm tab-side PipelineCache.

    Returns ``resolver(path, auto_crop, target_size)`` or ``None`` when the
    warm store is unreachable (no sessions, no ``_pipeline_cache``, no
    ``get_pixel``) — the service then keeps its built-in miss path. Every
    lookup is getattr-guarded; never raises.
    """
    try:
        sessions = getattr(main_controller, "sessions", None)
        if sessions is None:
            return None
        pipeline_cache = getattr(sessions, "_pipeline_cache", None)
        if pipeline_cache is None:
            return None
        if not callable(getattr(pipeline_cache, "get_pixel", None)):
            return None
    except Exception:
        return None
    try:
        from tabs.image_compare.plugins.video_editor.services.video_export.service import (
            make_pipeline_cache_thumbnail_resolver,
        )
    except Exception:
        return None
    try:
        return make_pipeline_cache_thumbnail_resolver(pipeline_cache)
    except Exception:
        return None


def session_state_thumbnail_resolver_for(main_controller):
    """Thumbnail proxy resolver reading the live Redux image session.

    Peeks ``main_controller.sessions.store.viewport.session_data.image_state``
    (``image1/image2`` — the SAME unified stores the canvas renders and the
    pyramids were built for) and matches snap paths against the ``document``
    slot lists (``image_list1/2``, the recorder's own path source;
    ``loaded_image*_paths`` are never populated). Returns
    ``resolver(path, auto_crop, target_size)`` or ``None`` when unreachable.
    Every lookup is getattr-guarded; never raises.
    """
    try:
        sessions = getattr(main_controller, "sessions", None)
        redux = getattr(sessions, "store", None)
        viewport = getattr(redux, "viewport", None)
        sess_data = getattr(viewport, "session_data", None)
        istate = getattr(sess_data, "image_state", None)
        if istate is None:
            return None
        has_images = getattr(istate, "image1", None) is not None or getattr(
            istate, "image2", None
        ) is not None
        if not has_images:
            return None
    except Exception:
        return None
    try:
        from tabs.image_compare.plugins.video_editor.services.video_export.service import (
            make_session_state_thumbnail_resolver,
        )
    except Exception:
        return None

    def _peek():
        try:
            sessions = getattr(main_controller, "sessions", None)
            redux = getattr(sessions, "store", None)
            viewport = getattr(redux, "viewport", None)
            sess_data = getattr(viewport, "session_data", None)
            istate = getattr(sess_data, "image_state", None)
            if istate is None:
                return None
            # NOTE: image_state.loaded_image*_paths are never populated
            # (write-paths don't exist) — match snap paths against the
            # document lists instead, the same source the recorder itself
            # uses for snap.image*_path.
            doc = None
            try:
                get_slot = getattr(redux, "get_session_state_slot", None)
                if callable(get_slot):
                    doc = get_slot("document")
            except Exception:
                doc = None
            try:
                list1 = [
                    getattr(item, "path", None)
                    for item in (getattr(doc, "image_list1", None) or [])
                ]
                list2 = [
                    getattr(item, "path", None)
                    for item in (getattr(doc, "image_list2", None) or [])
                ]
            except Exception:
                list1, list2 = None, None
            return (
                getattr(istate, "image1", None),
                getattr(istate, "image2", None),
                list1,
                list2,
            )
        except Exception:
            return None

    try:
        return make_session_state_thumbnail_resolver(_peek)
    except Exception:
        return None


def chain_thumbnail_resolvers(*resolvers):
    """First-hit-wins chain over nullable resolvers (session-state first)."""
    live = [r for r in resolvers if callable(r)]

    def _chained(path, auto_crop=False, target_size=None):
        for resolver in live:
            try:
                hit = resolver(path, auto_crop, target_size)
            except Exception:
                continue
            if hit is not None:
                return hit
        return None

    return _chained if live else None

class VideoEditorPresenter(QObject):
    previewUpdated = Signal(QPixmap)
    previewReady = Signal()
    timelinePositionChanged = Signal(int)
    playbackStateChanged = Signal(bool)
    buttonsStateChanged = Signal(bool, bool)
    fitContentAvailableChanged = Signal(bool)
    # NOTE: Signal(object), not Signal(dict) (HANDOFF-1 from A-feed): a
    # ``Signal(dict)`` emission converts the payload through QVariantMap,
    # which drops QPixmap values — receivers got ``{}`` (strip wipe).
    # ``object`` delivers the pixmap dict intact. The coordinator applies
    # bulk payloads directly to ``timeline.set_thumbnails`` and re-emits
    # here only as a no-widget fallback — both paths stay correct.
    thumbnailsUpdated = Signal(object)
    thumbnailReady = Signal(int, QPixmap)
    exportStarted = Signal()
    exportLog = Signal(str)
    errorOccurred = Signal(str)

    def __init__(self, view, snapshots, export_controller, main_controller):
        super().__init__()
        self.view = view
        self.export_controller = export_controller
        self.main_controller = main_controller
        initial_fps = resolve_initial_fps(view, snapshots, main_controller)
        self.model = VideoProjectModel(fps=initial_fps)
        self.editor_service = VideoEditorService(snapshots, fps=initial_fps)
        self.playback_engine = PlaybackEngine()
        self.playback_engine.set_playback_speed(1.0)
        self.thumbnail_service = ThumbnailService()
        video_exporter = (
            getattr(self.export_controller, "video_exporter", None)
            if self.export_controller is not None
            else None
        )
        if video_exporter is not None:
            self.thumbnail_service.set_snapshot_renderer(
                video_exporter.render_snapshot_thumbnail_to_pil
            )
            async_renderer = getattr(
                video_exporter, "render_snapshot_thumbnail_to_pil_async", None
            )
            if callable(async_renderer):
                # Preferred path: keeps the thumbnail worker thread from
                # blocking on the GPU round-trip (see ThumbnailService docs).
                self.thumbnail_service.set_async_snapshot_renderer(async_renderer)
            cpu_composer = getattr(video_exporter, "compose_thumbnail_cpu", None)
            if callable(cpu_composer) and hasattr(
                self.thumbnail_service, "set_cpu_thumbnail_composer"
            ):
                # Cheapest path: a ~2 ms PIL composite on the pool thread for
                # snapshots that need no overlay/diff/channel work. The
                # service still routes overlay-bearing frames to the GPU
                # renderer above (thumbnail_needs_gpu), and a composer miss
                # falls back to the sync GPU render, so this can only speed
                # the strip up — never change a tile that needs the GPU.
                self.thumbnail_service.set_cpu_thumbnail_composer(cpu_composer)
            # P1-followup: the exporter-private image repository is cold at
            # dialog open. Session-state image1/image2 are the SAME unified
            # stores the canvas renders and the pyramids were built for —
            # peek there first; PipelineCache second; built-in miss last.
            try:
                sessions = getattr(main_controller, "sessions", None)
                pipeline_cache = getattr(sessions, "_pipeline_cache", None)
                redux = getattr(sessions, "store", None)
                viewport = getattr(redux, "viewport", None)
                sess_data = getattr(viewport, "session_data", None)
                istate = getattr(sess_data, "image_state", None)
                logger.debug(
                    "thumbnail proxy resolver wiring: main_controller=%s sessions=%s pipeline_cache=%s image_state=%s",
                    main_controller is not None,
                    sessions is not None,
                    pipeline_cache is not None,
                    istate is not None,
                )
                proxy_resolver = chain_thumbnail_resolvers(
                    session_state_thumbnail_resolver_for(main_controller),
                    pipeline_cache_thumbnail_resolver_for(main_controller),
                )
                if proxy_resolver is not None and hasattr(
                    video_exporter, "set_thumbnail_proxy_resolver"
                ):
                    video_exporter.set_thumbnail_proxy_resolver(proxy_resolver)
                    logger.debug("thumbnail proxy resolver injected (chained)")
                else:
                    logger.debug("thumbnail proxy resolver skipped (factory None)")
            except Exception:
                logger.debug("PipelineCache thumbnail resolver skipped", exc_info=True)

        self.preview_coordinator = PreviewCoordinator(
            view=view,
            export_controller=export_controller,
            playback_engine=self.playback_engine,
            model=self.model,
            editor_service=self.editor_service,
            timer_parent=self,
            emit_preview_ready=self.previewReady.emit,
            emit_fit_content_available=self.fitContentAvailableChanged.emit,
        )
        self.output_coordinator = OutputPathCoordinator(
            view=view,
            main_controller=main_controller,
            editor_service=self.editor_service,
            model=self.model,
        )
        self.thumbnail_coordinator = ThumbnailCoordinator(
            view=view,
            editor_service=self.editor_service,
            playback_engine=self.playback_engine,
            thumbnail_service=self.thumbnail_service,
            emit_thumbnails_updated=self.thumbnailsUpdated.emit,
            timer_parent=self,
        )
        # Strip atlas phase 2: the GPU band draws the visible blocks in ONE
        # pass on every view change. The QPainter thumbnail pipeline above
        # keeps running untouched (phase 3 removes it); the band only covers
        # the strip row, so a band failure degrades to a placeholder tile, not
        # a dead timeline.
        self.strip_band_host = StripBandHost(
            view=view,
            editor_service=self.editor_service,
            export_controller=export_controller,
        )
        self.strip_band_host.attach()
        self.playback_coordinator = PlaybackCoordinator(
            view=view,
            editor_service=self.editor_service,
            playback_engine=self.playback_engine,
            model=self.model,
            export_controller=export_controller,
            preview_coordinator=self.preview_coordinator,
            thumbnail_coordinator=self.thumbnail_coordinator,
            emit_timeline_position=self.timelinePositionChanged.emit,
            emit_playback_state=self.playbackStateChanged.emit,
            emit_buttons_state=self.buttonsStateChanged.emit,
        )
        self.export_coordinator = ExportCoordinator(
            view=view,
            export_controller=export_controller,
            model=self.model,
            editor_service=self.editor_service,
            preview_coordinator=self.preview_coordinator,
        )

        self._connect_service_signals()
        self._connect_view_signals()
        QTimer.singleShot(0, self._initialize_from_snapshots)

    def _connect_service_signals(self):
        self.playback_engine.frameChanged.connect(self.playback_coordinator.on_frame_changed)
        self.playback_engine.playbackStateChanged.connect(
            self.playback_coordinator.on_playback_state_changed
        )
        self.thumbnail_service.thumbnailReady.connect(
            self.thumbnail_coordinator.on_single_thumbnail_ready
        )
        self.thumbnail_service.thumbnailsGenerated.connect(
            self.thumbnail_coordinator.on_thumbnails_generated
        )
        self.thumbnail_service.generationFinished.connect(
            self.thumbnail_coordinator.on_thumbnails_generation_finished
        )
        if self.main_controller and hasattr(self.main_controller, "video_export_progress"):
            self.main_controller.video_export_progress.connect(self._on_export_progress)
            self.main_controller.video_export_finished.connect(self._on_export_finished)
        if self.main_controller and hasattr(self.main_controller, "error_occurred"):
            self.main_controller.error_occurred.connect(self.errorOccurred)
        if self.main_controller and hasattr(self.main_controller, "video_export_log"):
            self.main_controller.video_export_log.connect(self.exportLog)

    def _disconnect_service_signals(self):
        try:
            self._safe_disconnect(self.playback_engine.frameChanged, self.playback_coordinator.on_frame_changed)
            self._safe_disconnect(self.playback_engine.playbackStateChanged, self.playback_coordinator.on_playback_state_changed)
            self._safe_disconnect(self.thumbnail_service.thumbnailReady, self.thumbnail_coordinator.on_single_thumbnail_ready)
            self._safe_disconnect(self.thumbnail_service.thumbnailsGenerated, self.thumbnail_coordinator.on_thumbnails_generated)
            self._safe_disconnect(self.thumbnail_service.generationFinished, self.thumbnail_coordinator.on_thumbnails_generation_finished)
        except Exception:
            pass
        if self.main_controller:
            try:
                if hasattr(self.main_controller, "video_export_progress"):
                    self._safe_disconnect(self.main_controller.video_export_progress, self._on_export_progress)
                    self._safe_disconnect(self.main_controller.video_export_finished, self._on_export_finished)
                if hasattr(self.main_controller, "error_occurred"):
                    self._safe_disconnect(self.main_controller.error_occurred, self.errorOccurred)
                if hasattr(self.main_controller, "video_export_log"):
                    self._safe_disconnect(self.main_controller.video_export_log, self.exportLog)
            except Exception:
                pass

    @staticmethod
    def _safe_disconnect(signal, slot):
        if signal is None:
            return
        try:
            # PySide6 reports an already-disconnected pair as a
            # RuntimeWarning ("Failed to disconnect ... from signal ..."),
            # NOT as an exception — the close path disconnects these pairs
            # twice by design (dialog side in closeEvent, presenter side in
            # cleanup), so silence exactly that expected failure mode here;
            # the disconnect itself still runs and drops a live pair.
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=RuntimeWarning)
                signal.disconnect(slot)
        except (TypeError, RuntimeError):
            pass

    def _connect_view_signals(self):
        self.view.destroyed.connect(self._on_view_destroyed)
        self.view.playClicked.connect(self.playback_coordinator.toggle_playback)
        self.view.playPauseRequested.connect(self.playback_coordinator.toggle_playback)
        self.view.timelineScrubbed.connect(self.playback_coordinator.seek_to_frame)
        self.view.undoClicked.connect(self.playback_coordinator.undo)
        self.view.redoClicked.connect(self.playback_coordinator.redo)
        self.view.trimClicked.connect(self.playback_coordinator.trim_selection)
        self.view.exportClicked.connect(self.export_video)
        self.view.stopExportClicked.connect(self.export_coordinator.stop_export)
        self.view.widthChanged.connect(self.preview_coordinator.on_width_changed)
        self.view.heightChanged.connect(self.preview_coordinator.on_height_changed)
        self.view.fpsChanged.connect(self.playback_coordinator.on_fps_changed)
        if getattr(self, "strip_band_host", None) is not None:
            # fps is part of the strip layout (block -> time), so a new fps
            # rebuilds the window on the next band frame.
            self.view.fpsChanged.connect(self.strip_band_host.on_recording_changed)
        self.view.previewScaleChanged.connect(
            self.preview_coordinator.on_preview_scale_changed
        )
        self.view.aspectRatioLockChanged.connect(
            self.playback_coordinator.on_aspect_ratio_lock_changed
        )
        self.view.fitContentChanged.connect(self.preview_coordinator.on_fit_content_changed)
        self.view.fitContentFillColorChanged.connect(
            self.preview_coordinator.on_fit_content_fill_color_changed
        )
        self.view.containerChanged.connect(self.output_coordinator.on_container_changed)
        # Stored (not inline): disconnect needs the identical callable.
        self._on_output_dir_text_changed = (
            lambda *_args: self.output_coordinator.refresh_unique_output_filename()
        )
        if hasattr(self.view, "edit_output_dir"):
            self.view.edit_output_dir.textChanged.connect(
                self._on_output_dir_text_changed
            )
        if hasattr(self.view, "edit_filename"):
            self.view.edit_filename.editingFinished.connect(
                self.output_coordinator.refresh_unique_output_filename
            )
        if hasattr(self.view, "timeline"):
            # Frozen timeline surface: zoom + viewport signals only (the
            # deprecated toolkit signals are gone toolkit-side).
            self.view.timeline.viewportChanged.connect(
                self.thumbnail_coordinator.on_timeline_viewport_changed
            )
            self.view.timeline.zoomChanged.connect(
                self.thumbnail_coordinator.on_timeline_zoom_changed
            )
            self.view.timeline.viewportChangedState.connect(
                self.thumbnail_coordinator.on_timeline_viewport_state_changed
            )
        self.view.windowResized.connect(self.preview_coordinator.on_window_resized)

    def _disconnect_view_signals(self):
        """Mirror of _connect_view_signals: drop every view/timeline slot.

        Every ``connect`` above has exactly one disconnect here, so no stale
        bound-method slot keeps the dialog alive past close. Safe to call on
        an already-destroyed view (every lookup/disconnect is guarded).
        """
        try:
            view = self.view
        except Exception:
            return
        if view is None:
            return

        def _sig(obj, name):
            try:
                return getattr(obj, name, None)
            except Exception:
                return None

        pairs = [
            (_sig(view, "destroyed"), self._on_view_destroyed),
            (_sig(view, "playClicked"), self.playback_coordinator.toggle_playback),
            (_sig(view, "playPauseRequested"), self.playback_coordinator.toggle_playback),
            (_sig(view, "timelineScrubbed"), self.playback_coordinator.seek_to_frame),
            (_sig(view, "undoClicked"), self.playback_coordinator.undo),
            (_sig(view, "redoClicked"), self.playback_coordinator.redo),
            (_sig(view, "trimClicked"), self.playback_coordinator.trim_selection),
            (_sig(view, "exportClicked"), self.export_video),
            (_sig(view, "stopExportClicked"), self.export_coordinator.stop_export),
            (_sig(view, "widthChanged"), self.preview_coordinator.on_width_changed),
            (_sig(view, "heightChanged"), self.preview_coordinator.on_height_changed),
            (_sig(view, "fpsChanged"), self.playback_coordinator.on_fps_changed),
            (_sig(view, "previewScaleChanged"), self.preview_coordinator.on_preview_scale_changed),
            (_sig(view, "aspectRatioLockChanged"), self.playback_coordinator.on_aspect_ratio_lock_changed),
            (_sig(view, "fitContentChanged"), self.preview_coordinator.on_fit_content_changed),
            (_sig(view, "fitContentFillColorChanged"), self.preview_coordinator.on_fit_content_fill_color_changed),
            (_sig(view, "containerChanged"), self.output_coordinator.on_container_changed),
            (_sig(view, "windowResized"), self.preview_coordinator.on_window_resized),
        ]
        strip_band_host = getattr(self, "strip_band_host", None)
        if strip_band_host is not None:
            # Appended only when the host exists: ``disconnect(None)`` would
            # drop every slot on ``fpsChanged``, not just ours.
            pairs.append((_sig(view, "fpsChanged"), strip_band_host.on_recording_changed))
        for signal, slot in pairs:
            try:
                self._safe_disconnect(signal, slot)
            except Exception:
                pass
        try:
            edit_output_dir = _sig(view, "edit_output_dir")
            if edit_output_dir is not None:
                self._safe_disconnect(
                    _sig(edit_output_dir, "textChanged"),
                    getattr(self, "_on_output_dir_text_changed", None),
                )
            edit_filename = _sig(view, "edit_filename")
            if edit_filename is not None:
                self._safe_disconnect(
                    _sig(edit_filename, "editingFinished"),
                    self.output_coordinator.refresh_unique_output_filename,
                )
        except Exception:
            pass
        try:
            timeline = _sig(view, "timeline")
            if timeline is not None:
                self._safe_disconnect(
                    _sig(timeline, "viewportChanged"),
                    self.thumbnail_coordinator.on_timeline_viewport_changed,
                )
                self._safe_disconnect(
                    _sig(timeline, "zoomChanged"),
                    self.thumbnail_coordinator.on_timeline_zoom_changed,
                )
                self._safe_disconnect(
                    _sig(timeline, "viewportChangedState"),
                    self.thumbnail_coordinator.on_timeline_viewport_state_changed,
                )
        except Exception:
            pass

    def _on_view_destroyed(self, *_args):
        self._disconnect_service_signals()
        self._disconnect_view_signals()
        self._disconnect_presenter_signals()
        strip_band_host = getattr(self, "strip_band_host", None)
        if strip_band_host is not None:
            # Partial presenters (stub views in the wiring suites) never built
            # the host; the destroy path must stay a no-op for them.
            strip_band_host.cleanup()
        self.preview_coordinator.on_view_destroyed()
        self.output_coordinator.detach_view()
        self.thumbnail_coordinator.detach_view()
        self.playback_coordinator.detach_view()
        self.export_coordinator.detach_view()
        self.view = None
        if self.parent() is None:
            try:
                self.deleteLater()
            except Exception:
                pass

    def _disconnect_presenter_signals(self):
        """Presenter side of the dialog wiring mirror (A-shell S1).

        Disconnects every own signal listed in
        ``dialog/runtime.py::PRESENTER_TO_DIALOG_SLOTS`` from the matching
        view slot. Called from ``cleanup`` and ``_on_view_destroyed``; the
        dialog side (``runtime.disconnect_presenter_signals``, invoked from
        ``shell.closeEvent``) covers the close path — both are idempotent,
        so close→cleanup double-disconnect is a no-op. Safe on a
        half-built or already-destroyed view (every lookup is guarded).
        """
        try:
            view = self.view
        except Exception:
            return
        if view is None:
            return
        try:
            from tabs.image_compare.plugins.video_editor.dialog.runtime import (
                PRESENTER_TO_DIALOG_SLOTS,
            )
        except Exception:
            return
        for signal_name, slot_name in PRESENTER_TO_DIALOG_SLOTS:
            try:
                signal = getattr(self, signal_name, None)
                slot = getattr(view, slot_name, None)
            except Exception:
                continue
            if signal is None or slot is None:
                continue
            try:
                self._safe_disconnect(signal, slot)
            except Exception:
                pass

    def _initialize_from_snapshots(self):
        _dbg_t0 = time.perf_counter()
        if not initialize_editor_from_snapshots(
            self.view, self.editor_service, self.playback_engine, self.model
        ):
            return
        _dbg_t1 = time.perf_counter()
        self.preview_coordinator.reset_render_state()
        self.playback_coordinator.update_buttons_state()
        self.thumbnail_coordinator.generate_thumbnails()
        self.preview_coordinator.schedule_update()
        # Eager, independent of fit_content_mode: lets the UI disable the
        # fit-content toggle up front when the canvas never leaves 0..1.
        self.preview_coordinator.recalculate_global_bounds()

    def _initialize_output_fields(self):
        _dbg_t0 = time.perf_counter()
        self.output_coordinator.initialize_output_fields()
        logger.warning(
            "DBG-BUG4 _initialize_output_fields took %.1fms",
            (time.perf_counter() - _dbg_t0) * 1000,
        )

    def set_favorite_path(self, path):
        self.output_coordinator.set_favorite_path(path)

    def get_favorite_path(self):
        return self.output_coordinator.get_favorite_path()

    def toggle_playback(self):
        self.playback_coordinator.toggle_playback()

    def start_playback(self):
        self.playback_coordinator.start_playback()

    def pause_playback(self):
        self.playback_coordinator.pause_playback()

    def seek_to_frame(self, frame_index: int):
        self.playback_coordinator.seek_to_frame(frame_index)

    def undo(self):
        self.playback_coordinator.undo()
        self._strip_band_content_changed()

    def redo(self):
        self.playback_coordinator.redo()
        self._strip_band_content_changed()

    def trim_selection(self):
        self.playback_coordinator.trim_selection()
        self._strip_band_content_changed()

    def _strip_band_content_changed(self):
        """A cut/undo/redo changed the recording the strip draws from."""
        host = getattr(self, "strip_band_host", None)
        if host is not None:
            host.on_recording_changed()

    def export_video(self):
        self.export_coordinator.export_video(self.exportStarted.emit)

    def stop_export(self):
        self.export_coordinator.stop_export()

    def cleanup(self):
        self._disconnect_service_signals()
        self._disconnect_view_signals()
        self._disconnect_presenter_signals()
        self.playback_engine.stop()
        self.thumbnail_coordinator.cleanup()
        strip_band_host = getattr(self, "strip_band_host", None)
        if strip_band_host is not None:
            strip_band_host.cleanup()
        # Remaining toolkit-private fallback (A-shell S1): no public
        # stop/teardown API is visible on the timeline widget from this
        # single-repo cell, so the guarded private-timer stop stays. It
        # cannot crash (hasattr-guarded) and only fires when the widget
        # outlives its dialog; logged + listed in DONE when it triggers.
        if self.view is not None and hasattr(self.view, "timeline") and hasattr(
            self.view.timeline, "_lerp_timer"
        ):
            logger.debug("video editor cleanup stopping timeline lerp timer")
            self.view.timeline._lerp_timer.stop()
        self.preview_coordinator.cleanup()
        self.output_coordinator.detach_view()
        self.thumbnail_coordinator.detach_view()
        self.playback_coordinator.detach_view()
        self.export_coordinator.detach_view()
        self.view = None
        if self.parent() is None:
            try:
                self.deleteLater()
            except Exception:
                pass

    def _on_export_progress(self, value):
        if self.view is not None:
            self.view.set_export_progress(value)

    def _on_export_finished(self, success):
        if self.view is not None:
            self.view.on_export_finished(success)
