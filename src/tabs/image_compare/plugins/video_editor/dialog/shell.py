# Audit-Meta: pattern=qdialog-wiring reason="video editor dialog shell — one QDialog chrome"
import logging
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
)

from tabs.host_helpers import (
    ThemedDialog,
    decorate_dialog,
    handle_application_font_change,
    install_dialog_help_menu,
    register_dialog_title_bar_navigation,
    unregister_dialog_title_bar_navigation,
)

from tabs.image_compare.plugins.video_editor.dialog.sections import (
    build_settings_panel,
    create_timeline_scroll_area,
    create_toolbar,
)
from tabs.image_compare.plugins.video_editor.layout_geometry import (
    MIN_VIDEO_EDITOR_BOTTOM_CONTAINER_HEIGHT_PX,
    MIN_VIDEO_EDITOR_DIALOG_HEIGHT_PX,
    MIN_VIDEO_EDITOR_PREVIEW_HEIGHT_PX,
    MIN_VIDEO_EDITOR_PREVIEW_WIDTH_PX,
    MIN_VIDEO_EDITOR_SETTINGS_WIDTH_PX,
    VIDEO_EDITOR_INITIAL_HEIGHT_PX,
    VIDEO_EDITOR_INITIAL_WIDTH_PX,
    VIDEO_EDITOR_TOP_HORIZONTAL_SPACING_PX,
)
from tabs.image_compare.plugins.video_editor.dialog.export import VideoEditorDialogExport
from tabs.image_compare.plugins.video_editor.dialog.nav import (
    register_navigation,
    unregister_navigation,
)
from tabs.image_compare.plugins.video_editor.dialog.persistence import VideoEditorDialogPersistence
from tabs.image_compare.plugins.video_editor.dialog.runtime import VideoEditorDialogRuntime
from tabs.image_compare.plugins.video_editor.dialog.surface_widgets import VideoPreviewSurface
from tabs.image_compare.plugins.video_editor.presenter import VideoEditorPresenter
from sli_ui_toolkit.i18n import tr
from sli_ui_toolkit.managers import scaled_px
from engine.cache.tab_canvas_services import create_canvas_widget
from sli_ui_toolkit.theme import ThemeManager
from sli_ui_toolkit.widgets import CustomLineEdit
from ui.widgets.themed_surface import ThemedBackgroundContainer
from core.log_names import APP_LOGGER_NAME

logger = logging.getLogger(APP_LOGGER_NAME)

class VideoEditorDialog(ThemedDialog):
    readyToShow = Signal()

    playClicked = Signal(bool)
    timelineScrubbed = Signal(int)
    playPauseRequested = Signal()
    undoClicked = Signal()
    redoClicked = Signal()
    trimClicked = Signal()
    exportClicked = Signal()
    stopExportClicked = Signal()

    widthChanged = Signal(int)
    heightChanged = Signal(int)

    fpsChanged = Signal(int)
    previewScaleChanged = Signal(float)
    aspectRatioLockChanged = Signal(bool)
    fitContentChanged = Signal(bool)
    fitContentFillColorChanged = Signal(object)
    containerChanged = Signal(str)
    windowResized = Signal()
    timelineHeightChanged = Signal(int)

    def __init__(self, snapshots, export_controller, main_window_app, parent=None):
        _dbg_t0 = time.perf_counter()
        super().__init__(parent)

        self.current_language = "en"
        if hasattr(export_controller, "store") and export_controller.store:
            self.current_language = export_controller.store.settings.current_language

        self.setWindowTitle(tr("video.video_editor_exporter", self.current_language))
        # Opening size is re-budgeted against the screen by
        # ``apply_top_row_geometry`` (see layout_geometry's fit-to-screen
        # section) — a 720p screen cannot show the design size.
        self.resize(
            scaled_px(VIDEO_EDITOR_INITIAL_WIDTH_PX),
            scaled_px(VIDEO_EDITOR_INITIAL_HEIGHT_PX),
        )
        self.setMinimumHeight(scaled_px(MIN_VIDEO_EDITOR_DIALOG_HEIGHT_PX))

        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint
        )
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        self.export_controller = export_controller
        self.main_window_app = main_window_app
        self.theme_manager = ThemeManager.get_instance()
        self.snapshots = snapshots
        self.fit_content_fill_color = QColor(0, 0, 0, 255)
        self._ready_to_show_emitted = False
        self.runtime = VideoEditorDialogRuntime(self)
        self.persistence = VideoEditorDialogPersistence(self)
        self.export_ui = VideoEditorDialogExport(self)

        self._init_ui()
        self.update_language(self.current_language)
        self.install_dialog_geometry(self._update_settings_panel_width)
        self.mark_theme_ui_ready()
        decorate_dialog(self, title=tr("video.video_editor_exporter", self.current_language))
        install_dialog_help_menu(self, page="video")

        main_controller = (
            export_controller.presenter.main_controller
            if hasattr(export_controller, "presenter")
            else None
        )
        self.presenter = VideoEditorPresenter(
            self,
            snapshots,
            export_controller,
            main_controller,
        )
        self._connect_presenter_signals()

        if hasattr(self, "edit_fps") and hasattr(self.presenter, "editor_service"):
            # Fps single source (A-shell S1): editor_service owns frame math;
            # the spinbox only clamps to the recording cap. model.fps stays a
            # synced mirror (no frame math reads it). The live fps-change
            # path (PlaybackCoordinator.on_fps_changed, out-of-scope file)
            # still writes model-only — see DONE HANDOFF.
            max_fps = 240
            first_snapshot = self._get_first_snapshot()
            if first_snapshot and hasattr(first_snapshot, "settings_state"):
                recording_fps = getattr(
                    first_snapshot.settings_state, "video_recording_fps", None
                )
                if recording_fps:
                    max_fps = recording_fps

            self.edit_fps.blockSignals(True)
            self.edit_fps.setRange(1, max_fps)
            service_fps = self.presenter.editor_service.get_fps()
            initial_fps = min(service_fps, max_fps)
            self.edit_fps.setValue(initial_fps)
            if initial_fps != service_fps:
                self.presenter.editor_service.set_fps(initial_fps)
                self.presenter.model.fps = self.presenter.editor_service.get_fps()
                logger.debug(
                    "video editor fps clamped to recording cap: %s -> %s",
                    service_fps,
                    initial_fps,
                )
            self.edit_fps.blockSignals(False)

        self._update_preview_palette()

        self.persistence.load_export_settings()
        self.persistence.connect_export_settings_persistence()

        self._update_settings_panel_width()
        # Register before first show — showEvent may be skipped on reuse/raise.
        self._contribute_find_actions()
        QTimer.singleShot(0, self._initialize_output_fields_guarded)
        QTimer.singleShot(1200, self._emit_ready_to_show_guarded)

        logger.warning(
            "DBG-BUG4 VideoEditorDialog.__init__ took %.1f ms (constructor done, about to return/show)",
            (time.perf_counter() - _dbg_t0) * 1000,
        )

    def _get_first_snapshot(self):
        snapshots = getattr(self, "snapshots", None)
        if snapshots is None:
            return None
        if isinstance(snapshots, (list, tuple)):
            return snapshots[0] if snapshots else None
        if hasattr(snapshots, "evaluate_at"):
            try:
                return snapshots.evaluate_at(0.0)
            except Exception:
                return None
        return None

    def _connect_presenter_signals(self):
        self.runtime.connect_presenter_signals()

    def _init_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        self.vertical_splitter = QSplitter(Qt.Orientation.Vertical)
        self.vertical_splitter.setChildrenCollapsible(False)
        self.vertical_splitter.setHandleWidth(scaled_px(8))
        self.main_layout.addWidget(self.vertical_splitter, stretch=1)

        self.top_container = ThemedBackgroundContainer()
        top_layout = QHBoxLayout(self.top_container)
        top_layout.setContentsMargins(scaled_px(10), scaled_px(10), scaled_px(10), scaled_px(10))
        top_layout.setSpacing(scaled_px(VIDEO_EDITOR_TOP_HORIZONTAL_SPACING_PX))

        canvas = create_canvas_widget()
        if canvas is None:
            logger.debug("VideoEditor preview canvas not available for active tab — using placeholder")
            canvas = VideoPreviewSurface()
            canvas.setObjectName("VideoEditorPreviewPlaceholder")
            # Degrade gracefully: plain QWidget placeholder, no QRhi rendering.
            canvas.set_read_only = lambda *a, **k: None  # type: ignore[attr-defined]
        self.preview_label = canvas
        self.preview_label.setObjectName("VideoEditorPreviewLabel" if canvas.objectName() != "VideoEditorPreviewPlaceholder" else "VideoEditorPreviewLabel")
        if hasattr(self.preview_label, "set_read_only"):
            try:
                self.preview_label.set_read_only(True)
            except Exception:
                logger.debug("preview placeholder set_read_only no-op", exc_info=True)
        self.preview_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.preview_label.setMinimumSize(
            scaled_px(MIN_VIDEO_EDITOR_PREVIEW_WIDTH_PX),
            scaled_px(MIN_VIDEO_EDITOR_PREVIEW_HEIGHT_PX),
        )
        top_layout.addWidget(self.preview_label, stretch=1)

        self.settings_panel = build_settings_panel(self)
        self.settings_panel.setMinimumWidth(
            scaled_px(MIN_VIDEO_EDITOR_SETTINGS_WIDTH_PX)
        )
        self.settings_panel.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred
        )
        top_layout.addWidget(self.settings_panel)
        self.vertical_splitter.addWidget(self.top_container)

        self.bottom_container = ThemedBackgroundContainer()
        bottom_layout = QVBoxLayout(self.bottom_container)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(0)

        self.toolbar_frame = create_toolbar(self)
        bottom_layout.addWidget(self.toolbar_frame)

        self.scroll_area = create_timeline_scroll_area(self)
        try:
            # Timeline-zone gate (debug_timeline.timeline_debug_enabled):
            # IMGSLI_TIMELINE_DEBUG-only (host-dbgflags W5 narrow; the
            # former VIDEO_EDITOR umbrella now belongs to the editor zone).
            from tabs.image_compare.plugins.video_editor.debug_timeline import (
                timeline_debug_enabled,
            )

            if timeline_debug_enabled():
                logging.getLogger(APP_LOGGER_NAME).warning("[timeline-debug] create_timeline_scroll_area timeline_id=%s scroll_id=%s viewport_id=%s", id(self.timeline) if hasattr(self, 'timeline') else -1, id(self.scroll_area), id(self.scroll_area.viewport()))
        except Exception:
            pass
        bottom_layout.addWidget(self.scroll_area, stretch=1)
        self.bottom_container.setMinimumHeight(
            scaled_px(MIN_VIDEO_EDITOR_BOTTOM_CONTAINER_HEIGHT_PX)
        )
        self.vertical_splitter.addWidget(self.bottom_container)
        self.vertical_splitter.setStretchFactor(0, 1)
        self.vertical_splitter.setStretchFactor(1, 0)
        self.vertical_splitter.setSizes([scaled_px(560), scaled_px(260)])
        self.vertical_splitter.splitterMoved.connect(self._on_splitter_moved)

        self._set_focus_policies()
        self._setup_shortcuts()

    def _on_splitter_moved(self, _pos: int, _index: int):
        # Dialog itself may not resize — only the preview/timeline panes do.
        # Emit the same signal as resizeEvent so preview re-letterboxes.
        self.timelineHeightChanged.emit(self.scroll_area.viewport().height())
        self.windowResized.emit()

    def _update_settings_panel_width(self):
        self.runtime.update_settings_panel_width()

    def changeEvent(self, event):
        handle_application_font_change(self, event)
        super().changeEvent(event)

    def _set_focus_policies(self):
        self.runtime.set_focus_policies()

    def _position_stop_export_button(self):
        self.runtime.position_stop_export_button()

    def _setup_shortcuts(self):
        self.runtime.setup_shortcuts()

    def set_preview(self, pixmap: QPixmap):
        self.runtime.set_preview(pixmap)

    def set_timeline_position(self, frame_idx: int):
        self.timeline.blockSignals(True)
        self.timeline.set_current_frame(frame_idx)
        self.timeline.blockSignals(False)

    def set_snapshots(
        self,
        snapshots,
        fps: int | None = None,
        timeline_model=None,
        duration: float | None = None,
    ):
        self.timeline.set_data(
            snapshots, fps=fps, timeline_model=timeline_model, duration=duration
        )

    def set_resolution(self, width: int, height: int):
        self.edit_width.blockSignals(True)
        self.edit_height.blockSignals(True)
        self.edit_width.setText(str(width))
        self.edit_height.setText(str(height))
        self.edit_width.blockSignals(False)
        self.edit_height.blockSignals(False)

    def update_available_codecs(self, codecs: list, default_codec: str):
        from tabs.image_compare.plugins.video_editor.services.export_config import ExportConfigBuilder

        current_codec = self.combo_codec.currentData()
        self.combo_codec.blockSignals(True)
        self.combo_codec.clear()
        for codec in codecs:
            self.combo_codec.addItem(
                self._tr(ExportConfigBuilder.get_codec_display_key(codec)), codec
            )

        target_codec = current_codec if current_codec in codecs else default_codec
        idx = self.combo_codec.findData(target_codec)
        if idx >= 0:
            self.combo_codec.setCurrentIndex(idx)
        elif self.combo_codec.count() > 0:
            self.combo_codec.setCurrentIndex(0)

        self.combo_codec.blockSignals(False)

        self._on_codec_changed(self.combo_codec.currentText())

    def get_selection_range(self) -> tuple:
        return self.timeline.get_selection_range()

    def has_selection(self) -> bool:
        return self.timeline.has_selection()

    def get_export_options(self) -> dict:
        return self.export_ui.get_export_options()

    def get_preview_size(self) -> tuple:
        try:
            width = self.preview_label.width()
            height = self.preview_label.height()
            if width >= 10 and height >= 10:
                return width, height
            hint = self.preview_label.sizeHint()
            return max(width, hint.width()), max(height, hint.height())
        except RuntimeError:
            return (0, 0)

    def _emit_ready_to_show(self) -> None:
        self.runtime.emit_ready_to_show()

    def _dialog_alive(self) -> bool:
        """True while the C++ dialog object still exists.

        Fail-open when the binding check itself is unavailable — callers
        still guard the callback body with try/except RuntimeError.
        """
        try:
            import shiboken6

            return bool(shiboken6.isValid(self))
        except Exception:
            return True

    def _initialize_output_fields_guarded(self) -> None:
        # showEvent/__init__ singleShot target: no work scheduled past close.
        if not self._dialog_alive():
            return
        try:
            self.presenter._initialize_output_fields()
        except RuntimeError:
            pass

    def _emit_ready_to_show_guarded(self) -> None:
        # __init__ singleShot target: no work scheduled past close.
        if not self._dialog_alive():
            return
        try:
            self._emit_ready_to_show()
        except RuntimeError:
            pass

    def _fit_timeline_if_alive(self) -> None:
        # showEvent singleShot target: no work scheduled past close.
        if not self._dialog_alive():
            return
        try:
            timeline = getattr(self, "timeline", None)
            if timeline is None:
                return
            import shiboken6

            if not shiboken6.isValid(timeline):
                return
            timeline.fit_view()
        except RuntimeError:
            pass

    def _emit_window_signal_if_alive(self) -> None:
        # showEvent singleShot target: no work scheduled past close.
        if not self._dialog_alive():
            return
        try:
            self.windowResized.emit()
        except RuntimeError:
            pass

    def _on_play_toggled(self, checked: bool):
        self.playClicked.emit(checked)

    def _on_undo_clicked(self):
        self.undoClicked.emit()

    def _on_redo_clicked(self):
        self.redoClicked.emit()

    def _on_trim_clicked(self):
        self.trimClicked.emit()

    def _on_export_clicked(self):
        if hasattr(self, "export_progress") and self.export_progress.isVisible():
            return
        self.exportClicked.emit()

    def _on_stop_export_clicked(self):
        self.stopExportClicked.emit()

    def _on_head_moved(self, index: int):
        self.timelineScrubbed.emit(index)

    def _on_play_pause_requested(self):
        self.playPauseRequested.emit()

    def _on_width_edited(self):
        try:
            val = int(self.edit_width.text())
            self.widthChanged.emit(val)
        except ValueError:
            pass

    def _on_height_edited(self):
        try:
            val = int(self.edit_height.text())
            self.heightChanged.emit(val)
        except ValueError:
            pass

    def _on_fps_changed(self, fps: int):
        self.fpsChanged.emit(fps)

    def _on_preview_scale_changed(self):
        if not hasattr(self, "combo_preview_scale"):
            return
        current = self.combo_preview_scale.currentData()
        try:
            self.previewScaleChanged.emit(float(current))
        except (TypeError, ValueError):
            self.previewScaleChanged.emit(1.0)

    def _on_ratio_lock_toggled(self, checked: bool):
        self.aspectRatioLockChanged.emit(checked)

    def _on_fit_content_toggled(self, checked: bool):
        if hasattr(self, "btn_fit_fill_color"):
            self.btn_fit_fill_color.setVisible(bool(checked))
        self.fitContentChanged.emit(checked)

    def _on_fit_fill_color_clicked(self):
        self.export_ui.on_fit_fill_color_clicked()

    def _update_fit_fill_color_button(self):
        self.export_ui.update_fit_fill_color_button()

    def _on_container_changed(self, text: str):
        container = self.combo_container.currentData()
        if container:
            self.containerChanged.emit(container)

    def _on_codec_changed(self, codec_text: str):
        self.export_ui.on_codec_changed(codec_text)

    def _on_preview_updated(self, pixmap: QPixmap):
        self.set_preview(pixmap)

    def _on_timeline_position_changed(self, frame_idx: int):
        self.set_timeline_position(frame_idx)

    def _on_playback_state_changed(self, is_playing: bool):
        if self.btn_play.isChecked() != is_playing:
            self.btn_play.setChecked(is_playing, emit_signal=False)

    def _on_buttons_state_changed(self, can_undo: bool, can_redo: bool):
        self.btn_undo.setEnabled(can_undo)
        self.btn_redo.setEnabled(can_redo)

        undo_opacity = "1.0" if can_undo else "0.4"
        redo_opacity = "1.0" if can_redo else "0.4"

        self.btn_undo.setProperty("opacity", undo_opacity)
        self.btn_redo.setProperty("opacity", redo_opacity)

    def _on_fit_content_available_changed(self, available: bool):
        # Nothing to uncrop when the canvas never leaves normalized 0..1
        # (no snapshot ever padded/overflowed the frame) — toggling would be
        # a visual no-op. Same dim-when-disabled treatment as undo/redo.
        if hasattr(self, "btn_fit_content"):
            self.btn_fit_content.setEnabled(available)
            self.btn_fit_content.setProperty("opacity", "1.0" if available else "0.4")

    def _on_export_started(self):
        self._set_export_progress_state("active")
        self.set_export_progress(0)
        if hasattr(self, "export_log_edit"):
            from datetime import datetime
            ts = datetime.now().strftime("%H:%M:%S")
            tpl = self._tr("video.export_started")
            msg = tpl.format(ts=ts) if "{ts}" in tpl else f"{tpl} {ts}" if tpl != "video.export_started" else f"Export started {ts}"
            self.export_log_edit.append_status(msg)

    def set_export_progress(self, value: int):
        self.runtime.set_export_progress(value)

    def on_export_finished(self, success: bool):
        self.runtime.on_export_finished(success)

    def _on_export_log(self, line: str):
        if hasattr(self, "export_log_edit"):
            self.export_log_edit.append_info(line)

    def _on_error_occurred(self, error_message: str):
        logger.error(f"Video editor error: {error_message}")
        if hasattr(self, "export_log_edit"):
            self.export_log_edit.append_error(error_message)
        if hasattr(self, "tabs") and hasattr(self, "tab_log"):
            self.tabs.setCurrentWidget(self.tab_log)

    def _set_export_progress_state(self, state: str) -> None:
        if not hasattr(self, "export_progress"):
            return
        self.export_progress.setProperty("state", state)
        self.export_progress.update()

    def _browse_output_dir(self, checked: bool = False):
        self.persistence.browse_output_dir()

    def _on_set_favorite_clicked(self, checked: bool = False):
        self.persistence.on_set_favorite_clicked()

    def _on_use_favorite_clicked(self, checked: bool = False):
        self.persistence.on_use_favorite_clicked()

    def _tr(self, text):
        return tr(text, self.current_language)

    def update_language(self, lang_code: str):
        from tabs.image_compare.plugins.video_editor.translations import apply_translations

        apply_translations(self, lang_code or "en")
        self._update_settings_panel_width()

    def _tr_preset(self, preset):
        from tabs.image_compare.plugins.video_editor.services.export_config import ExportConfigBuilder

        key = ExportConfigBuilder.get_preset_translation_key(preset) or f"video.{preset}"
        translated = self._tr(key)
        return preset if translated == key else translated

    def on_dialog_theme_changed(self) -> None:
        self._update_preview_palette()

    def _update_preview_palette(self):
        self.runtime.update_preview_palette()

    def resizeEvent(self, event: QResizeEvent):
        super().resizeEvent(event)
        self.windowResized.emit()

        if hasattr(self, "timeline"):
            self.timeline.update_layout_width()
        self._position_stop_export_button()

    def showEvent(self, event):
        super().showEvent(event)

        # Register before the first show's focus work — the manager's own
        # deferred bootstrap then sees focus already inside the section and
        # skips. Idempotent: re-shows (raise/unminimize) re-register cleanly.
        register_navigation(self)
        # Title bar AFTER the dialog's modal row section: sections registered
        # after a modal section sit above its ceiling, so the bar keeps the
        # keyboard instead of the modal reclaim pulling focus back into the
        # rows. Down from the bar enters the first row via the section's own
        # Down fallback (see shared_toolkit.ui.decorate_dialog).
        register_dialog_title_bar_navigation(self)

        # Children (the timeline) install their own window event filters during
        # *their* showEvent, i.e. before this one — keep the CSD chrome filter
        # ahead of them or the chrome freezes at its show-time size.

        if hasattr(self, "timeline"):
            QTimer.singleShot(100, self._fit_timeline_if_alive)
        self._position_stop_export_button()

        # Prefer timeline over the first settings LineEdit in the focus chain.
        if hasattr(self, "timeline"):
            self.timeline.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        else:
            self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

        QTimer.singleShot(150, self._emit_window_signal_if_alive)
        self._contribute_find_actions()

    def closeEvent(self, event):
        # Drop the nav section first: no late key may reach a section whose
        # owner is mid-teardown. Idempotent (the manager's destroyed-hook is
        # the backstop for WA_DeleteOnClose).
        unregister_navigation(self)
        unregister_dialog_title_bar_navigation(self)
        self._withdraw_find_actions()
        self.persistence.persist_export_settings()
        # Dialog side of the bidirectional disconnect (A-shell S1): drop
        # every presenter→dialog slot BEFORE presenter.cleanup, so no late
        # async completion lands on widgets mid-teardown. Both sides are
        # idempotent — cleanup disconnects again harmlessly.
        try:
            self.runtime.disconnect_presenter_signals()
        except Exception:
            logger.debug(
                "video editor presenter-signal disconnect skipped",
                exc_info=True,
            )
        if hasattr(self, "presenter"):
            self._cleanup_presenter_safe()
        super().closeEvent(event)

    def _cleanup_presenter_safe(self) -> None:
        """Tear the presenter down once, and never through a dead wrapper.

        ``cleanup`` (and ``_on_view_destroyed`` on ``view.destroyed``) ends
        with ``deleteLater()`` on the presenter, whose C++ object owns the
        coordinators' children — ``SettleGate`` and its ``QTimer``,
        ``QThreadPool``s — so once that deletion has been processed the
        Python wrapper still answers attribute lookups while every C++ call
        inside it raises ``RuntimeError: Internal C++ object ... already
        deleted`` (owner traceback 2026-10-28: ``closeEvent`` → ``cleanup`` →
        ``SettleGate.cancel`` → ``QTimer.stop``).
        In that state the teardown already ran and its timers died with it:
        re-entering it can only crash, so skip and log.
        """
        presenter = self.presenter
        try:
            import shiboken6

            alive = bool(shiboken6.isValid(presenter))
        except Exception:
            alive = True
        if not alive:
            logger.debug(
                "video editor presenter already torn down; close cleanup skipped"
            )
            return
        presenter.cleanup()

    def _contribute_find_actions(self) -> None:
        try:
            from tabs.image_compare.plugins.video_editor.actions import (
                OWNER,
                contribute_video_editor_actions,
            )
            from ui.actions.palette import install_dialog_find_action_shortcut

            contribute_video_editor_actions(self)
            install_dialog_find_action_shortcut(self, active_tab=OWNER)
        except Exception:
            logger.debug(
                "[find-action] video_editor _contribute_find_actions failed",
                exc_info=True,
            )

    def _withdraw_find_actions(self) -> None:
        try:
            from tabs.image_compare.plugins.video_editor.actions import (
                withdraw_video_editor_actions,
            )

            withdraw_video_editor_actions()
        except Exception:
            logger.debug(
                "[find-action] video_editor _withdraw_find_actions failed",
                exc_info=True,
            )

    def keyPressEvent(self, event):

        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        focused_widget = self.focusWidget()
        if isinstance(focused_widget, (QLineEdit, CustomLineEdit)):
            focused_widget.clearFocus()
        super().mousePressEvent(event)