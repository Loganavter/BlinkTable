# Audit-Meta: pattern=qdialog-wiring reason="one QDialog layout/signal wiring — size from scaled_px/QSS-free boilerplate"
import io
import logging
from pathlib import Path
from typing import Iterable

import PIL.Image
import shiboken6 as sip
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QImage, QMouseEvent, QPixmap
from shared_toolkit.ui.themed_dialog import ThemedDialog
from PySide6.QtWidgets import (
    QFileDialog,
    QLineEdit,
)

from domain.qt_adapters import color_to_qcolor
from plugins.export.dialog_sections import assemble_export_ui
from plugins.export.layout_geometry import apply_export_dialog_geometry
from plugins.export.models import ExportDialogState
from resources.translations import tr as app_tr
from shared.image_processing.pil_save import sanitize_filename_component
from shared_toolkit.ui.layout_sizing import handle_application_font_change
from sli_ui_toolkit.i18n import translatable_text, translatable_tooltip
from sli_ui_toolkit.managers import SettleGate, scaled_px
from sli_ui_toolkit.ui.pending_settle import PendingSettle, settle_on_paint
from sli_ui_toolkit.theme import ThemeManager
from sli_ui_toolkit.widgets import CustomLineEdit, Text
from ui.theming import polish_themed_dialog
from core.log_names import APP_LOGGER_NAME
from utils.resource_loader import resource_path

logger = logging.getLogger(APP_LOGGER_NAME)

#: Catalogue key of the export-side honesty line. Same wording as the
#: properties-panel notice, because it is the same fact: the file the user is
#: exporting does not contain what the viewer showed.
FIRST_FRAME_EXPORT_KEY = "export.export_first_frame"


def first_frame_source_suffixes(
    source_paths: Iterable[str | Path] | None,
) -> tuple[str, ...]:
    """Suffixes among *source_paths* that only contributed one frame.

    Group B of :mod:`shared.image_extensions` (``.gif``/``.apng``/``.mpo``/
    ``.fli``/``.flc``/``.dcx``/``.avifs``/``.psd``) is decoded as one frame or
    one flattened composite, so a composite exported from it is honest only if
    the dialog says so. Pure suffix matching — no file is opened, no decoder
    question is asked, so a missing/odd path cannot make the dialog fail.
    """
    try:
        from shared.image_extensions import FIRST_FRAME_SUFFIXES
    except Exception:
        return ()
    found: list[str] = []
    for raw in source_paths or ():
        try:
            suffix = Path(str(raw)).suffix.lower()
        except Exception:
            continue
        if suffix in FIRST_FRAME_SUFFIXES and suffix not in found:
            found.append(suffix)
    return tuple(found)


def has_first_frame_source(source_paths: Iterable[str | Path] | None) -> bool:
    """True when at least one export input is a first-frame/flattened file."""
    return bool(first_frame_source_suffixes(source_paths))


class ExportDialog(ThemedDialog):
    def __init__(
        self,
        dialog_state: ExportDialogState,
        parent=None,
        tr_func=None,
        preview_image: QPixmap | PIL.Image.Image | None = None,
        suggested_filename: str = "",
        on_set_favorite_dir=None,
        native_size: tuple[int, int] | None = None,
        source_paths: Iterable[str | Path] | None = None,
    ):
        super().__init__(parent)
        self.setWindowIcon(QIcon(resource_path("resources/icons/icon.png")))
        self.setObjectName("ExportDialog")
        # Never assign to ``self.tr`` — QObject.tr() is Qt's own API and
        # shadowing it makes translations look like raw keys.
        self._tr_func = tr_func if callable(tr_func) else app_tr
        self.dialog_state = dialog_state
        self.theme_manager = ThemeManager.get_instance()
        self.suggested_filename = suggested_filename
        # Documents this export is built from. Only their SUFFIX is used: a
        # group-B source (first frame / flattened composite, see
        # ``shared.image_extensions``) hides part of itself, and the dialog
        # owes the user that sentence before the file is written.
        self._first_frame_suffixes = first_frame_source_suffixes(source_paths)
        self._on_set_favorite_dir = on_set_favorite_dir
        self.favorite_dir = dialog_state.favorite_dir
        self._export_options_cache: dict | None = None
        # CSD mask after the deferred geometry lands: a 0-timer rebuilt it
        # one frame late (corner arcs punched through the first frame). The
        # paint boundary rebuilds it before those pixels go out.
        self._csd_settle = PendingSettle()

        nw, nh = native_size if native_size and native_size[0] > 0 and native_size[1] > 0 else (0, 0)
        self._native_width = int(nw)
        self._native_height = int(nh)
        # Base size the resolution fields currently describe: the full canvas,
        # or the image area while "crop to images" is on. The toggle keeps the
        # user's scale and re-derives the pair, so the field never promises a
        # size the file will not have.
        self._resolution_base = (self._native_width, self._native_height)
        self._aspect_ratio = (self._native_width / self._native_height) if self._native_height else 0.0
        self._suppress_ratio_recalc = False
        try:
            self._initial_scale = max(0.05, float(getattr(dialog_state, "resolution_scale", 1.0) or 1.0))
        except (TypeError, ValueError):
            self._initial_scale = 1.0

        self.setWindowTitle(self._tr("misc.export", "Export"))
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        # Non-modal on purpose, exactly like the video editor dialog: mutter
        # treats a *modal* transient as an attached dialog and, on a move
        # drag, grabs the parent's frame instead of the dialog's
        # (``meta_window_is_attached_dialog(window) && meta_grab_op_is_moving``
        # → ``grab_window = get_toplevel_transient_for(window)``, mutter
        # 2014-05-02). The symptom was the main window following the export
        # dialog around the screen. The dialog is still parented to the shell
        # (stacking/activation), but nothing here waits on it: callers
        # continue in a ``finished`` handler (see
        # ``DialogManager.show_export_dialog``), never after ``exec()``.
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setSizeGripEnabled(True)
        settle_on_paint(self, self._csd_settle)

        self._preview_source_pixmap: QPixmap | None = None
        # Cropped variant of the preview (built by the tab when a crop window
        # exists), kept separately so the pane can switch without a re-render.
        self._preview_crop_pixmap: QPixmap | None = None
        # The label stretches the source itself on every paint; this gate only
        # bakes one smooth frame for the size the resize settled on. Rebuilding
        # the pixmap per resize frame is what made the pane look "killed and
        # recreated" instead of stretched (2026-10-28).
        self._preview_resize_settle = SettleGate(
            on_settle=self._apply_preview_pixmap,
            interval_ms=SettleGate.DEFAULT_INTERVAL_MS,
            parent=self,
        )

        if preview_image is not None:
            if isinstance(preview_image, QPixmap):
                self._preview_source_pixmap = preview_image.copy()
            elif isinstance(preview_image, PIL.Image.Image):
                try:
                    self._preview_source_pixmap = self._pixmap_from_pil(preview_image)
                    if self._preview_source_pixmap.isNull():
                        logger.warning(
                            "Preview image conversion resulted in a null QPixmap."
                        )
                        self._preview_source_pixmap = None
                except Exception as e:
                    logger.error(
                        f"Failed to convert preview PIL image to QPixmap: {e}",
                        exc_info=True,
                    )
                    self._preview_source_pixmap = None

        crop_preview = getattr(dialog_state, "crop_preview_image", None)
        if crop_preview is not None:
            try:
                if isinstance(crop_preview, QPixmap):
                    self._preview_crop_pixmap = crop_preview.copy()
                elif isinstance(crop_preview, PIL.Image.Image):
                    pix = self._pixmap_from_pil(crop_preview)
                    self._preview_crop_pixmap = None if pix.isNull() else pix
            except Exception:
                logger.error(
                    "Failed to convert the cropped preview to a QPixmap",
                    exc_info=True,
                )
                self._preview_crop_pixmap = None

        self._init_ui()
        # Keyboard navigation: the dialog had no section at all (see
        # plugins/export/navigation.py).
        from plugins.export.navigation import register_export_navigation

        register_export_navigation(self)
        # Stable focus-attribution names (focus-debug tracer): override the
        # shared OutputPathSection generics with export-specific names.
        self.edit_dir.setObjectName("export-dir")
        self.edit_name.setObjectName("export-name")
        self._bind_translations()
        self._reapply_bound_texts()
        self.install_dialog_geometry(self._apply_dialog_geometry)
        self.mark_theme_ui_ready()
        from shared_toolkit.ui.decorate_dialog import decorate_dialog, install_dialog_help_menu
        decorate_dialog(self, title=self._tr("misc.export", "Export"))
        install_dialog_help_menu(self, page="export")

        self._populate_from_state()
        self._suggest_default_filename()
        self._update_controls_visity_by_format()

        # Line edits stay TabFocus-capable: keyboard users must be able to
        # Tab through directory / name / resolution / comment fields. (ClickFocus
        # alone drops them from the focus chain entirely.)
        line_edits = [
            self.edit_dir,
            self.edit_name,
            self.edit_comment,
            getattr(self, "edit_width", None),
            getattr(self, "edit_height", None),
        ]
        for line_edit in line_edits:
            if line_edit is None:
                continue
            line_edit.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            line_edit.returnPressed.connect(line_edit.clearFocus)
        self.setFocus()

        self.finished.connect(self._withdraw_find_actions)
        self._contribute_find_actions()
        QTimer.singleShot(0, self._finalize_layout_and_size)

    def _tr(self, key: str, default: str | None = None) -> str:
        lang = self.dialog_state.current_language or "en"
        text = self._tr_func(key, lang)
        if text == key and default is not None:
            return default
        return text

    def _tr_for_bind(self, key: str, language: str | None = None) -> str:
        """Adapter for ``translatable_text`` / ``translatable_tooltip``.

        Prefer the dialog's language (``dialog_state``) so the first bind pass
        does not depend on whether the global TranslationManager language has
        already been emitted.
        """
        lang = self.dialog_state.current_language or language or "en"
        text = self._tr_func(key, lang)
        return text

    def _apply_dialog_geometry(self) -> None:
        apply_export_dialog_geometry(self)
        self._pin_window_minimum()

    def _pin_window_minimum(self) -> None:
        """Re-pin the QWindow minimum to the current widget minimum.

        Visibility toggles (format rows, metadata block) change
        ``minimumSize()`` after show; the platform window keeps the stale
        value, so CSD resizes can crush the form. Re-pin after every apply.
        """
        try:
            handle = self.windowHandle()
        except RuntimeError:
            return
        if handle is not None:
            handle.setMinimumSize(self.minimumSize())

    def changeEvent(self, event):
        handle_application_font_change(self, event)
        super().changeEvent(event)

    def update_language(self, language: str) -> None:
        self.dialog_state.current_language = language or "en"
        self.setWindowTitle(self._tr("misc.export", "Export"))
        title_bar = getattr(self, "_csd_title_bar", None)
        if title_bar is not None and hasattr(title_bar, "set_title"):
            title_bar.set_title(self._tr("misc.export", "Export"))
        # Re-apply bound texts for the new dialog language (bind helpers read
        # dialog_state, so a no-op callback refresh is enough via setters).
        self._reapply_bound_texts()
        self._apply_dialog_geometry()

    def _reapply_bound_texts(self) -> None:
        """Force bound widgets to the current ``dialog_state`` language."""
        pairs = (
            (self.export_preview_title, "export.preview", ""),
            (self.output_section.dir_label, "label.output_directory", ":"),
            (self.btn_browse_dir, "button.browse", ""),
            (self.btn_set_favorite, "misc.set_as_favorite", ""),
            (self.btn_use_favorite, "tooltip.use_favorite", ""),
            (self.name_label, "label.file_name", ":"),
            (self.fmt_label, "label.format", ":"),
            (self.resolution_label, "label.resolution", ":"),
            (self.quality_label, "label.quality", ":"),
            (self.label_png_compress, "export.png_compression_level", ":"),
            (self.checkbox_png_optimize, "export.optimize_png", ""),
            (self.checkbox_fill_bg, "export.fill_background", ""),
            (self.btn_bg_color, "export.background_color", ""),
            (self.checkbox_crop_to_image, "export.crop_to_image", ""),
            (self.checkbox_include_metadata, "export.include_metadata", ""),
            (self.comment_label, "export.comment", ":"),
            (self.checkbox_comment_default, "export.remember_by_default", ""),
            (self.btn_ok, "common.ok", ""),
            (self.btn_cancel, "common.cancel", ""),
        )
        for widget, key, suffix in pairs:
            widget.setText(self._tr(key, key) + suffix)
        notice = getattr(self, "first_frame_notice_label", None)
        if notice is not None:
            notice.setText(self._first_frame_notice_text())
        self.btn_lock_ratio.setToolTip(
            self._tr("export.lock_aspect_ratio", "Lock aspect ratio")
        )
        if hasattr(self, "checkbox_crop_to_image"):
            self.checkbox_crop_to_image.setToolTip(
                self._tr(
                    "export.crop_to_image_tooltip",
                    "Keep only the area the images occupy; empty margins are trimmed.",
                )
            )
        if hasattr(self.action_bar, "_apply_button_minimums"):
            self.action_bar._apply_button_minimums()
        if hasattr(self.action_bar, "lock_content_minimum_height"):
            self.action_bar.lock_content_minimum_height()
        self._harden_text_buttons(
            self.btn_browse_dir,
            self.btn_set_favorite,
            self.btn_use_favorite,
            self.btn_bg_color,
        )
        if hasattr(self, "output_section"):
            self.output_section.lock_content_minimum_height()

    @staticmethod
    def _harden_text_buttons(*buttons) -> None:
        """Keep text buttons from collapsing below their content sizeHint.

        Do not use ``setFixedHeight`` here — toolkit text buttons hint at
        ``fontMetrics.height() + 16`` (often > 32), and a tighter fixed height
        clips the bottom edge / corner radius.
        """
        from PySide6.QtWidgets import QSizePolicy

        for button in buttons:
            if button is None:
                continue
            # Drop any leftover fixed-height clamp from construction.
            button.setMinimumHeight(0)
            button.setMaximumHeight(16777215)
            hint = button.sizeHint()
            button.setMinimumSize(
                max(button.minimumWidth(), hint.width()),
                max(scaled_px(32), hint.height()),
            )
            button.setSizePolicy(
                QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed
            )

    def _finalize_layout_and_size(self):
        self._apply_preview_pixmap()
        # Force computed size: CSD adjustSize may already have overgrown from
        # the preview pixmap before this deferred pass runs.
        apply_export_dialog_geometry(self, force_resize=True)
        try:
            from sli_ui_toolkit.ui.windows.csd_helpers import sync_csd_chrome

            sync_csd_chrome(self)
        except Exception:
            pass

    def _bind_translations(self) -> None:
        trb = self._tr_for_bind
        translatable_text(self.export_preview_title, "export.preview", tr_func=trb)
        translatable_text(
            self.output_section.dir_label,
            "label.output_directory",
            suffix=":",
            tr_func=trb,
        )
        translatable_text(self.btn_browse_dir, "button.browse", tr_func=trb)
        translatable_text(self.btn_set_favorite, "misc.set_as_favorite", tr_func=trb)
        translatable_text(self.btn_use_favorite, "tooltip.use_favorite", tr_func=trb)
        translatable_text(
            self.name_label, "label.file_name", suffix=":", tr_func=trb
        )
        translatable_text(self.fmt_label, "label.format", suffix=":", tr_func=trb)
        translatable_text(
            self.resolution_label, "label.resolution", suffix=":", tr_func=trb
        )
        translatable_tooltip(
            self.btn_lock_ratio, "export.lock_aspect_ratio", tr_func=trb
        )
        translatable_text(
            self.quality_label, "label.quality", suffix=":", tr_func=trb
        )
        translatable_text(
            self.label_png_compress,
            "export.png_compression_level",
            suffix=":",
            tr_func=trb,
        )
        translatable_text(
            self.checkbox_png_optimize, "export.optimize_png", tr_func=trb
        )
        translatable_text(
            self.checkbox_fill_bg, "export.fill_background", tr_func=trb
        )
        translatable_text(
            self.btn_bg_color, "export.background_color", tr_func=trb
        )
        translatable_text(
            self.checkbox_include_metadata, "export.include_metadata", tr_func=trb
        )
        translatable_text(
            self.comment_label, "export.comment", suffix=":", tr_func=trb
        )
        translatable_text(
            self.checkbox_comment_default,
            "export.remember_by_default",
            tr_func=trb,
        )
        translatable_text(self.btn_ok, "common.ok", tr_func=trb)
        translatable_text(self.btn_cancel, "common.cancel", tr_func=trb)

    def _init_ui(self):
        assemble_export_ui(self)
        self._build_first_frame_notice()

    def _build_first_frame_notice(self) -> None:
        """One honesty line when a SOURCE document is only partly on screen.

        The export writes the composite built from the frame the viewer holds,
        so for a ``.gif``/``.apng``/``.fli``/``.dcx``/``.mpo``/``.avifs``
        source that is the first frame and for ``.psd`` the flattened
        composite. Hidden entirely for ordinary formats: one row, no panel, no
        change to the rest of the form.
        """
        self.first_frame_notice_label = Text(
            self._first_frame_notice_text(), mode="plain"
        )
        self.first_frame_notice_label.setObjectName("export-first-frame-notice")
        self.first_frame_notice_label.setWordWrap(True)
        self.first_frame_notice_label.setVisible(bool(self._first_frame_suffixes))
        layout = self.export_form_frame.layout()
        if layout is not None and layout.count():
            # Directly above the action bar: the last thing read before OK.
            layout.insertWidget(layout.count() - 1, self.first_frame_notice_label)

    def _first_frame_notice_text(self) -> str:
        if not self._first_frame_suffixes:
            return ""
        return self._tr(FIRST_FRAME_EXPORT_KEY, "Only the first frame is exported")

    def _populate_from_state(self):
        self.edit_dir.setText(self.dialog_state.output_dir)

        fmt = (self.dialog_state.last_format or "PNG").upper()
        idx = self.combo_format.findText(fmt)
        if idx >= 0:
            self.combo_format.setCurrentIndex(idx)
        self.slider_quality.setValue(int(self.dialog_state.quality or 95))
        self.label_quality_value.setText(str(self.slider_quality.value()))
        self.slider_png_compress.setValue(
            int(self.dialog_state.png_compress_level or 9)
        )
        self.label_png_compress_value.setText(
            str(int(self.dialog_state.png_compress_level or 9))
        )
        self.checkbox_png_optimize.setChecked(True)

        if self.dialog_state.background_color is not None:
            self.current_bg_color = color_to_qcolor(self.dialog_state.background_color)
        # Desired preference from settings; _update_controls_visity_by_format
        # clears/disables when virtual canvas is inactive.
        self.checkbox_fill_bg.setChecked(bool(self.dialog_state.fill_background))
        self._sync_background_controls()
        self.checkbox_crop_to_image.setChecked(
            bool(getattr(self.dialog_state, "crop_to_image", False))
        )

        self.checkbox_include_metadata.setChecked(True)

        self.edit_comment.setText(self.dialog_state.comment_text or "")
        self.checkbox_comment_default.setChecked(bool(self.dialog_state.comment_keep_default))

    def _suggest_default_filename(self):
        self.edit_name.setText(self.suggested_filename or "comparison")

    def _choose_directory(self):
        start_dir = self.edit_dir.text().strip() or self.dialog_state.output_dir
        file_dialog = QFileDialog(self)
        file_dialog.setFileMode(QFileDialog.FileMode.Directory)
        file_dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
        file_dialog.setWindowTitle(
            self._tr("export.select_output_directory", "Select Output Directory")
        )
        file_dialog.setDirectory(start_dir)

        polish_themed_dialog(self.theme_manager, file_dialog)

        if file_dialog.exec():
            chosen = (
                file_dialog.selectedFiles()[0] if file_dialog.selectedFiles() else ""
            )
            if chosen:
                self.edit_dir.setText(chosen)

    def _set_favorite_from_current(self):
        path = self.edit_dir.text().strip()
        if path:
            self.favorite_dir = path
            if callable(self._on_set_favorite_dir):
                self._on_set_favorite_dir(path)

    def _use_favorite_dir(self):
        path = self.favorite_dir
        if path:
            self.edit_dir.setText(path)

    # -- crop to images ----------------------------------------------------

    def _crop_to_image_checked(self) -> bool:
        box = getattr(self, "checkbox_crop_to_image", None)
        return bool(box is not None and box.isChecked())

    def _effective_native_size(self) -> tuple[int, int]:
        """Base size the resolution fields describe (canvas or image area)."""
        crop = getattr(self.dialog_state, "crop_native_size", None)
        if self._crop_to_image_checked() and crop and crop[0] > 0 and crop[1] > 0:
            return (int(crop[0]), int(crop[1]))
        return (self._native_width, self._native_height)

    def _on_crop_to_image_toggled(self, _checked: bool = False) -> None:
        """Re-express the resolution at the new base, keeping the user's scale."""
        prev_base = self._resolution_base
        new_base = self._effective_native_size()
        try:
            current_w = int(self.edit_width.text())
        except (AttributeError, ValueError):
            current_w = prev_base[0]
        scale = (
            current_w / float(prev_base[0])
            if prev_base[0] > 0 and current_w > 0
            else self._initial_scale
        )
        self._resolution_base = new_base
        self._aspect_ratio = (
            new_base[0] / float(new_base[1]) if new_base[1] > 0 else 0.0
        )
        if new_base[0] > 0 and new_base[1] > 0 and hasattr(self, "edit_width"):
            self._suppress_ratio_recalc = True
            try:
                self.edit_width.setText(
                    str(max(1, int(round(new_base[0] * scale))))
                )
                self.edit_height.setText(
                    str(max(1, int(round(new_base[1] * scale))))
                )
            finally:
                self._suppress_ratio_recalc = False
        self._apply_preview_pixmap()

    def _effective_preview_source(self):
        """Preview pixmap for the current state (cropped when the option is on)."""
        if self._crop_to_image_checked() and self._preview_crop_pixmap is not None:
            if not self._preview_crop_pixmap.isNull():
                return self._preview_crop_pixmap
        return self._preview_source_pixmap

    def _fill_background_allowed(self) -> bool:
        """Fill only when pads exist (virtual canvas past 0..1) and format has alpha."""
        fmt = (
            self.combo_format.currentText().upper()
            if hasattr(self, "combo_format")
            else "PNG"
        )
        has_transparency = fmt in ("PNG", "TIFF", "WEBP", "JXL")
        return has_transparency and bool(
            getattr(self.dialog_state, "virtual_canvas_active", True)
        )

    def _on_fill_background_toggled(self, _checked: bool = False) -> None:
        self._sync_background_controls()
        self._apply_preview_pixmap()

    def _sync_background_controls(self) -> None:
        """Color picker is only active while fill is enabled (or forced by format)."""
        fmt = (
            self.combo_format.currentText().upper()
            if hasattr(self, "combo_format")
            else "PNG"
        )
        has_transparency = fmt in ("PNG", "TIFF", "WEBP", "JXL")
        fill_allowed = self._fill_background_allowed()
        fill_active = (
            bool(self.checkbox_fill_bg.isChecked()) if fill_allowed else (not has_transparency)
        )
        btn = getattr(self, "btn_bg_color", None)
        if btn is None:
            return
        btn.setEnabled(fill_active)
        # Plain picker — never leave a latch/checked glow when fill is off.
        set_checked = getattr(btn, "setChecked", None)
        if callable(set_checked):
            try:
                set_checked(False, emit=False)
            except TypeError:
                set_checked(False)

    def _pick_bg_color(self):
        if hasattr(self, "btn_bg_color") and not self.btn_bg_color.isEnabled():
            return

        from ui.widgets.color import ColorPickerDialog

        dialog = ColorPickerDialog(
            (
                self.current_bg_color
                if isinstance(self.current_bg_color, QColor)
                else QColor(255, 255, 255, 255)
            ),
            self,
            title=self._tr("export.select_background_color", "Select Background Color"),
            show_alpha=True,
        )
        dialog.setWindowModality(Qt.WindowModality.WindowModal)

        def on_color_selected(color):
            if color.isValid():
                self.current_bg_color = color
                self._sync_background_controls()
                self._apply_preview_pixmap()

        dialog.colorSelected.connect(on_color_selected)
        dialog.show()

    def _update_controls_visity_by_format(self):
        fmt = self.combo_format.currentText().upper()
        lossy = fmt in ("JPEG", "WEBP", "JXL")
        self.quality_row.setVisible(lossy)
        self.png_row.setVisible(fmt == "PNG")

        has_transparency = fmt in ("PNG", "TIFF", "WEBP", "JXL")
        fill_allowed = self._fill_background_allowed()
        self.checkbox_crop_to_image.setVisible(
            bool(getattr(self.dialog_state, "crop_to_image_available", False))
        )
        self.checkbox_fill_bg.setVisible(has_transparency)
        self.checkbox_fill_bg.setEnabled(fill_allowed)
        if has_transparency and not fill_allowed:
            # Unit canvas (0..1): no pads to paint — keep fill off.
            self.checkbox_fill_bg.blockSignals(True)
            self.checkbox_fill_bg.setChecked(False)
            self.checkbox_fill_bg.blockSignals(False)
        elif fill_allowed and not self.checkbox_fill_bg.isChecked():
            # Restore settings preference when pads become relevant again
            # (format switch back to alpha with an active virtual canvas).
            if bool(self.dialog_state.fill_background):
                self.checkbox_fill_bg.blockSignals(True)
                self.checkbox_fill_bg.setChecked(True)
                self.checkbox_fill_bg.blockSignals(False)
        self._sync_background_controls()

        self._apply_preview_pixmap()
        self._apply_dialog_geometry()
    def resizeEvent(self, event):
        super().resizeEvent(event)
        # CSD startSystemResize can ignore QWidget/QWindow minimumSize on some
        # compositors — pull the window back so form controls are not crushed.
        min_size = self.minimumSize()
        if self.width() < min_size.width() or self.height() < min_size.height():
            if not getattr(self, "_restoring_min_size", False):
                self._restoring_min_size = True

                def _restore() -> None:
                    try:
                        if sip.isValid(self):
                            self.resize(
                                max(self.width(), min_size.width()),
                                max(self.height(), min_size.height()),
                            )
                    finally:
                        self._restoring_min_size = False

                QTimer.singleShot(0, _restore)
            return
        self._preview_resize_settle.ping()

    def showEvent(self, event):
        super().showEvent(event)
        self._pin_window_minimum()
        try:
            from sli_ui_toolkit.ui.windows.csd_helpers import sync_csd_chrome

            sync_csd_chrome(self)
        except Exception:
            pass
        # Deferred geometry may land after first show; rebuild CSD mask once
        # more on the next tick so corner arcs cannot punch through the body.
        self._csd_settle.schedule(self, self._sync_csd_chrome_safe)
        # Finalize synchronously: showEvent runs before the first frame is
        # drawn, so the __init__ 0-timer would otherwise leave that frame at
        # the CSD-adjustSize size and reflow on the next tick.
        self._finalize_layout_and_size()
        self._contribute_find_actions()

    def _contribute_find_actions(self) -> None:
        try:
            from plugins.export.actions import (
                _export_dialog_owner_tab,
                contribute_export_dialog_actions,
            )
            from ui.actions.palette import install_dialog_find_action_shortcut

            owner = _export_dialog_owner_tab()
            contribute_export_dialog_actions(self, owner_tab=owner)
            install_dialog_find_action_shortcut(self, active_tab=owner)
        except Exception:
            logger.debug(
                "[find-action] export_dialog _contribute_find_actions failed",
                exc_info=True,
            )

    def _withdraw_find_actions(self, *_args) -> None:
        try:
            from plugins.export.actions import withdraw_export_dialog_actions

            withdraw_export_dialog_actions()
        except Exception:
            logger.debug(
                "[find-action] export_dialog _withdraw_find_actions failed",
                exc_info=True,
            )
    def _sync_csd_chrome_safe(self) -> None:
        if not sip.isValid(self):
            return
        try:
            from sli_ui_toolkit.ui.windows.csd_helpers import sync_csd_chrome

            sync_csd_chrome(self)
        except Exception:
            pass

    def _preview_fill_color(self) -> QColor | None:
        """Background painted under the preview, or ``None`` (transparent frame).

        Filled only when the export frame has pads (a virtual canvas past 0..1)
        and the format keeps alpha — or when the format forces an opaque frame.
        The label's theme chrome around the KeepAspectRatio letterbox stays the
        theme preview surface.
        """
        fmt_current = (
            self.combo_format.currentText().upper()
            if hasattr(self, "combo_format")
            else "PNG"
        )
        formats_with_alpha = {"PNG", "TIFF", "WEBP", "JXL"}
        force_fill = fmt_current not in formats_with_alpha
        fill_allowed = self._fill_background_allowed()
        effective_fill = (
            force_fill
            or (fill_allowed and bool(self.checkbox_fill_bg.isChecked()))
        )
        if not effective_fill:
            return None
        bg = (
            self.current_bg_color
            if isinstance(self.current_bg_color, QColor)
            else QColor(255, 255, 255, 255)
        )
        return QColor(bg)

    def _apply_preview_pixmap(self):
        """Hand the source to the pane and bake the settled frame.

        The pane stretches the source on every paint (see
        ``preview_pane.ExportPreviewLabel``); this only refreshes the
        keyed smooth raster for the size it currently has.
        """
        label = getattr(self, "preview_label", None)
        if label is None:
            return
        source = self._effective_preview_source()
        if source is None or source.isNull():
            label.clear_preview()
            return
        label.set_preview_source(source, self._preview_fill_color())
        label.bake_preview()

    def _pixmap_from_pil(self, pil_img: PIL.Image.Image) -> QPixmap:
        try:

            if pil_img.mode == "RGBA":
                data = pil_img.tobytes("raw", "RGBA")
                qimage = QImage(
                    data,
                    pil_img.width,
                    pil_img.height,
                    pil_img.width * 4,
                    QImage.Format.Format_RGBA8888,
                )
                if not qimage.isNull():
                    # Own the pixels — ``data`` is a temporary Python buffer.
                    return QPixmap.fromImage(qimage.copy())
            elif pil_img.mode == "RGB":
                data = pil_img.tobytes("raw", "RGB")
                qimage = QImage(
                    data,
                    pil_img.width,
                    pil_img.height,
                    pil_img.width * 3,
                    QImage.Format.Format_RGB888,
                )
                if not qimage.isNull():
                    return QPixmap.fromImage(qimage.copy())

            buf = io.BytesIO()
            pil_img.save(buf, format="PNG")
            data = buf.getvalue()
            pix = QPixmap()
            pix.loadFromData(data, "PNG")  # type: ignore[call-overload]  # stub types format as bytes, runtime accepts str
            return pix
        except Exception as e:
            logger.error(f"Error converting PIL Image to QPixmap: {e}", exc_info=True)
            return QPixmap()

    def get_export_options(self) -> dict:
        if not sip.isValid(self):
            return dict(self._export_options_cache or {})
        if hasattr(self, "edit_dir") and not sip.isValid(self.edit_dir):
            return dict(self._export_options_cache or {})
        return self._snapshot_export_options()

    def _snapshot_export_options(self) -> dict:
        fmt = self.combo_format.currentText().upper()
        bg = (
            self.current_bg_color
            if isinstance(self.current_bg_color, QColor)
            else QColor(255, 255, 255, 255)
        )
        # Dialog boundary: the name that leaves the dialog is already a single
        # filename component, so no downstream save can receive a separator.
        # The forgiving sanitiser never raises while the user is typing; an
        # empty field stays empty so the existing "name not specified" error
        # still fires instead of silently writing image.png.
        typed_name = self.edit_name.text().strip()
        opts = {
            "output_dir": self.edit_dir.text().strip(),
            "file_name": (
                sanitize_filename_component(typed_name, max_len=120)
                if typed_name
                else ""
            ),
            "format": fmt,
            "quality": int(self.slider_quality.value()),
            "fill_background": (
                bool(self.checkbox_fill_bg.isChecked())
                if self._fill_background_allowed()
                else False
            ),
            "fill_background_editable": self._fill_background_allowed(),
            "crop_to_image": bool(
                self._crop_to_image_checked()
                and getattr(self.dialog_state, "crop_to_image_available", False)
            ),
            "virtual_canvas_active": bool(
                getattr(self.dialog_state, "virtual_canvas_active", True)
            ),
            "background_color": (bg.red(), bg.green(), bg.blue(), bg.alpha()),
            "png_compress_level": int(self.slider_png_compress.value()),
            "png_optimize": bool(self.checkbox_png_optimize.isChecked()),
            "include_metadata": bool(self.checkbox_include_metadata.isChecked()),
            "comment_text": self.edit_comment.text().strip(),
            "comment_keep_default": bool(self.checkbox_comment_default.isChecked()),
        }
        try:
            w = int(self.edit_width.text())
            h = int(self.edit_height.text())
            if w > 0 and h > 0:
                opts["width"] = w
                opts["height"] = h
                base_w, _base_h = self._effective_native_size()
                if base_w > 0:
                    opts["resolution_scale"] = w / float(base_w)
        except (ValueError, AttributeError):
            pass
        return opts

    def _on_width_edited(self):
        if self._suppress_ratio_recalc:
            return
        text = self.edit_width.text().strip()
        if not text:
            self._reset_resolution_to_native()
            return
        try:
            w = int(text)
        except ValueError:
            return
        if w <= 0:
            self._reset_resolution_to_native()
            return
        if self.btn_lock_ratio.isChecked() and self._aspect_ratio > 0:
            new_h = max(1, int(round(w / self._aspect_ratio)))
            self._suppress_ratio_recalc = True
            self.edit_height.setText(str(new_h))
            self._suppress_ratio_recalc = False

    def _on_height_edited(self):
        if self._suppress_ratio_recalc:
            return
        text = self.edit_height.text().strip()
        if not text:
            self._reset_resolution_to_native()
            return
        try:
            h = int(text)
        except ValueError:
            return
        if h <= 0:
            self._reset_resolution_to_native()
            return
        if self.btn_lock_ratio.isChecked() and self._aspect_ratio > 0:
            new_w = max(1, int(round(h * self._aspect_ratio)))
            self._suppress_ratio_recalc = True
            self.edit_width.setText(str(new_w))
            self._suppress_ratio_recalc = False

    def _reset_resolution_to_native(self):
        base_w, base_h = self._effective_native_size()
        if base_w <= 0 or base_h <= 0:
            return
        self._resolution_base = (base_w, base_h)
        self._suppress_ratio_recalc = True
        self.edit_width.setText(str(base_w))
        self.edit_height.setText(str(base_h))
        self._suppress_ratio_recalc = False

    def accept(self):
        self._export_options_cache = self._snapshot_export_options()
        super().accept()

    def _on_include_metadata_toggled(self, checked: bool):

        if hasattr(self, "comment_label"):
            self.comment_label.setVisible(checked)
        if hasattr(self, "edit_comment"):
            self.edit_comment.setVisible(checked)
        if hasattr(self, "checkbox_comment_default"):
            self.checkbox_comment_default.setVisible(checked)

    def mousePressEvent(self, event: QMouseEvent):
        self.clear_input_focus()
        super().mousePressEvent(event)

    def clear_input_focus(self):
        focused_widget = self.focusWidget()
        if focused_widget and isinstance(focused_widget, (QLineEdit, CustomLineEdit)):
            focused_widget.clearFocus()