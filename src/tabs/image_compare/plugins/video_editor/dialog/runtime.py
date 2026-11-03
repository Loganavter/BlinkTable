import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QKeySequence, QPixmap, QPalette
from PySide6.QtWidgets import QLineEdit

from sli_ui_toolkit.managers import scaled_px
from sli_ui_toolkit.widgets import CustomLineEdit
from tabs.image_compare.plugins.video_editor.layout_geometry import (
    apply_top_row_geometry,
    compute_minimum_dialog_width,
)
from tabs.image_compare.plugins.video_editor.widgets.timeline import (
    VideoTimelineWidget,
)
from ui.theming import polish_themed_dialog
from core.log_names import APP_LOGGER_NAME

logger = logging.getLogger(APP_LOGGER_NAME)


# Single source for the presenter→dialog wiring (A-shell S1): every
# (presenter signal, dialog slot) pair connected in
# ``connect_presenter_signals`` is listed here, and
# ``disconnect_presenter_signals`` (dialog side) plus
# ``VideoEditorPresenter._disconnect_presenter_signals`` (presenter side,
# lazy-imports this table) disconnect exactly these pairs. Add a connect
# without a table entry and the pairing test in
# ``test_video_editor_play_shortcut.py`` fails.
PRESENTER_TO_DIALOG_SLOTS: tuple[tuple[str, str], ...] = (
    ("previewUpdated", "_on_preview_updated"),
    ("previewReady", "_emit_ready_to_show"),
    ("timelinePositionChanged", "_on_timeline_position_changed"),
    ("playbackStateChanged", "_on_playback_state_changed"),
    ("buttonsStateChanged", "_on_buttons_state_changed"),
    ("fitContentAvailableChanged", "_on_fit_content_available_changed"),
    ("exportStarted", "_on_export_started"),
    ("exportLog", "_on_export_log"),
    ("errorOccurred", "_on_error_occurred"),
)


def focused_widget_consumes_space(focused) -> bool:
    """True when bare Space is already handled by the focused control itself.

    The timeline widget accepts bare Space and emits ``playPauseRequested``
    (wired to the playback toggle), so the dialog-level Space shortcut must
    stay silent while it has focus — otherwise one press toggles twice.
    Line edits keep their long-standing exemption (Space types a space).
    Correct under both Qt behaviors: if an accepted key still reaches the
    shortcut map, this guard skips the duplicate; if acceptance suppresses
    the shortcut, the guard never triggers and the widget signal is the
    single toggle.
    """
    return isinstance(focused, (QLineEdit, CustomLineEdit, VideoTimelineWidget))

class VideoEditorDialogRuntime:
    def __init__(self, dialog):
        self.dialog = dialog

    def connect_presenter_signals(self):
        d = self.dialog
        d.presenter.previewUpdated.connect(d._on_preview_updated)
        d.presenter.previewReady.connect(d._emit_ready_to_show)
        d.presenter.timelinePositionChanged.connect(d._on_timeline_position_changed)
        d.presenter.playbackStateChanged.connect(d._on_playback_state_changed)
        d.presenter.buttonsStateChanged.connect(d._on_buttons_state_changed)
        d.presenter.fitContentAvailableChanged.connect(d._on_fit_content_available_changed)
        d.presenter.exportStarted.connect(d._on_export_started)
        d.presenter.exportLog.connect(d._on_export_log)
        d.presenter.errorOccurred.connect(d._on_error_occurred)

    def disconnect_presenter_signals(self):
        """Exact mirror of connect_presenter_signals (A-shell S1).

        Drops every presenter→dialog slot so no late async completion
        (preview render, export progress) reaches a closed dialog. Called
        from ``shell.closeEvent`` AND ``presenter.cleanup`` — idempotent:
        every lookup/disconnect is guarded, disconnecting twice is a no-op.
        """
        d = self.dialog
        try:
            presenter = getattr(d, "presenter", None)
        except Exception:
            return
        if presenter is None:
            return
        for signal_name, slot_name in PRESENTER_TO_DIALOG_SLOTS:
            try:
                signal = getattr(presenter, signal_name, None)
                slot = getattr(d, slot_name, None)
            except Exception:
                continue
            if signal is None or slot is None:
                continue
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass

    def update_settings_panel_width(self):
        d = self.dialog
        try:
            apply_top_row_geometry(d)
        except Exception as exc:
            logger.warning(f"Error calculating panel width: {exc}")
            fallback_width = scaled_px(380)
            d.settings_panel.setFixedWidth(fallback_width)
            current_min = d.minimumSize()
            d.setMinimumSize(
                compute_minimum_dialog_width(fallback_width),
                current_min.height(),
            )

    def set_focus_policies(self):
        d = self.dialog
        # Every control the arrow ring must reach is StrongFocus: the nav
        # section's _focusable() only collects StrongFocus children (ClickFocus
        # edits and NoFocus buttons would be invisible to row navigation).
        # Leaving a field is safe by toolkit design: collapsed combos and
        # spinboxes ignore Up/Down, and the section yields Left/Right at a
        # line edit's caret edge.
        for btn in [
            d.btn_undo,
            d.btn_redo,
            d.btn_trim,
            d.btn_play,
            d.btn_export,
            d.btn_lock_ratio,
            d.btn_fit_content,
            d.btn_fit_fill_color,
        ]:
            btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        if hasattr(d, "btn_stop_export"):
            d.btn_stop_export.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        d.timeline.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # Same pattern as ExportDialog (returnPressed drops focus back out of
        # the field), with StrongFocus so the ring can also ENTER a field.
        from sli_ui_toolkit.widgets import ComboBox, LogConsoleWidget, SpinBox

        for edit in [*d.findChildren(QLineEdit), *d.findChildren(CustomLineEdit)]:
            edit.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            if hasattr(edit, "returnPressed"):
                edit.returnPressed.connect(edit.clearFocus)
        for spin in d.findChildren(SpinBox):
            spin.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        for combo in d.findChildren(ComboBox):
            combo.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # Service containers stay out of the ring: QScrollArea defaults to
        # StrongFocus, so without this the tab pages' scroll areas would join
        # the tabs row and their native arrow handling would trap the ring
        # (the settings dialog does the same with NoFocus page scroll areas).
        from PySide6.QtWidgets import QScrollArea

        for scroll in d.findChildren(QScrollArea):
            scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for console in d.findChildren(LogConsoleWidget):
            console.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def position_stop_export_button(self):
        d = self.dialog
        if not hasattr(d, "btn_export") or not hasattr(d, "btn_stop_export"):
            return
        x = scaled_px(12)
        y = max(0, (d.btn_export.height() - d.btn_stop_export.height()) // 2)
        d.btn_stop_export.move(x, y)
        d.btn_stop_export.raise_()

    def setup_shortcuts(self):
        d = self.dialog

        def handle_space():
            focused = d.focusWidget()
            if not focused_widget_consumes_space(focused):
                d._on_play_toggled(not d.btn_play.isChecked())

        from PySide6.QtGui import QShortcut

        d.shortcut_space = QShortcut(QKeySequence(Qt.Key.Key_Space), d)
        d.shortcut_space.activated.connect(handle_space)
        d.shortcut_undo = QShortcut(QKeySequence("Ctrl+Z"), d)
        d.shortcut_undo.activated.connect(d._on_undo_clicked)
        d.shortcut_redo = QShortcut(QKeySequence("Ctrl+Y"), d)
        d.shortcut_redo.activated.connect(d._on_redo_clicked)
        d.shortcut_redo_alt = QShortcut(QKeySequence("Ctrl+Shift+Z"), d)
        d.shortcut_redo_alt.activated.connect(d._on_redo_clicked)

        def handle_delete():
            focused = d.focusWidget()
            if not isinstance(focused, (QLineEdit, CustomLineEdit)):
                d._on_trim_clicked()

        d.shortcut_delete = QShortcut(QKeySequence(Qt.Key.Key_Delete), d)
        d.shortcut_delete.activated.connect(handle_delete)
        d.shortcut_backspace = QShortcut(QKeySequence(Qt.Key.Key_Backspace), d)
        d.shortcut_backspace.activated.connect(handle_delete)

    def set_preview(self, pixmap: QPixmap):
        d = self.dialog
        if not pixmap:
            logger.warning("[set_preview] Received None pixmap!")
            return
        if hasattr(d.preview_label, "set_pixmap"):
            d.preview_label.set_pixmap(pixmap)
            return
        label_w = d.preview_label.width()
        label_h = d.preview_label.height()
        pix_w = pixmap.width()
        pix_h = pixmap.height()
        if label_w > 0 and label_h > 0 and (abs(pix_w - label_w) > 10 or abs(pix_h - label_h) > 10):
            d.preview_label.setPixmap(
                pixmap.scaled(
                    label_w,
                    label_h,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            return
        d.preview_label.setPixmap(pixmap)

    def update_preview_palette(self):
        bg = QColor("#0a0a0a") if self.dialog.theme_manager.is_dark() else QColor("#f5f5f5")
        pal = self.dialog.preview_label.palette()
        pal.setColor(QPalette.ColorRole.Window, bg)
        self.dialog.preview_label.setPalette(pal)
        self.dialog.preview_label.update()

    def apply_style(self):
        polish_themed_dialog(self.dialog.theme_manager, self.dialog)

    def emit_ready_to_show(self):
        d = self.dialog
        if d._ready_to_show_emitted:
            return
        d._ready_to_show_emitted = True
        d.readyToShow.emit()

    def set_export_progress(self, value: int):
        d = self.dialog
        if not d.export_progress.isVisible():
            d.export_progress.setVisible(True)
            d.btn_export.set_override_bg_color(None)
            d.btn_export.setCursor(Qt.CursorShape.ArrowCursor)
            d.btn_export.setText(d._tr("video.rendering"))
            d.btn_stop_export.show()
            self.position_stop_export_button()
        d.export_progress.setValue(value)

    def reset_export_ui_state(self):
        d = self.dialog
        # Late QTimer callback (2000ms post-export): drop when the dialog
        # closed meanwhile — no widget touches past close.
        try:
            if not d._dialog_alive():
                return
        except Exception:
            pass
        d.export_progress.setVisible(False)
        d.export_progress.setValue(0)
        d.btn_export.set_override_bg_color(None)
        d.btn_export.setCursor(Qt.CursorShape.PointingHandCursor)
        d.btn_export.setText(d._tr("image_compare.action.export_video"))
        if hasattr(d, "btn_stop_export"):
            d.btn_stop_export.hide()
        d._set_export_progress_state("active")

    def on_export_finished(self, success: bool):
        d = self.dialog
        if hasattr(d, "export_log_edit"):
            from datetime import datetime
            ts = datetime.now().strftime("%H:%M:%S")
            if success:
                tpl = d._tr("video.export_finished")
                msg = tpl.format(ts=ts) if "{ts}" in tpl else f"{tpl} {ts}" if tpl != "video.export_finished" else f"Export finished {ts}"
                d.export_log_edit.append_status(msg)
            else:
                tpl = d._tr("video.export_failed")
                msg = tpl.format(ts=ts) if "{ts}" in tpl else f"{tpl} {ts}" if tpl != "video.export_failed" else f"Export failed {ts}"
                d.export_log_edit.append_error(msg)
        if success:
            d.export_progress.setValue(100)
            d._set_export_progress_state("success")
            d.btn_export.setText(d._tr("video.done"))
            QTimer.singleShot(2000, self.reset_export_ui_state)
            return
        self.reset_export_ui_state()
        if hasattr(d, "tabs") and hasattr(d, "tab_log"):
            d.tabs.setCurrentWidget(d.tab_log)