"""Export dialog: the "save view" option (export the region the viewport shows).

Contract (task export-view-dialog, scene-zoom feature):

- the option is offered only when the tab reports a visible-view rect
  (``save_view_available`` + ``view_rect``), so a tab whose view cannot be
  cropped never sees it;
- "save view" and "crop to images" are mutually exclusive — both replace what
  the written file is a picture of, so two crops cannot share one preview;
- the resolution fields follow the view's native size while the option is on,
  keeping the user's scale, and go back to the full canvas when it is off;
- the preview shows the crop of the full preview defined by ``view_rect`` (no
  second raster from the tab);
- the accepted options carry ``save_view`` / ``view_rect`` only when the
  option really applies, and the value survives a settings round-trip.

Red-before: with the option removed from the dialog, every assertion here fails
(no ``checkbox_save_view``, no ``save_view``/``view_rect`` option keys, the
fields stay on the full canvas).

Cropping is always ``(left, top, width, height)`` in canvas px — the same shape
``shared.image_processing.view_geometry.visible_view_rect`` returns.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from core.store import Store
from domain.types import Color
from plugins.export.dialog import ExportDialog
from plugins.export.models import ExportDialogState
from plugins.settings.manager import SettingsManager
from resources.translations import add_i18n_root
from shared.image_processing.view_geometry import visible_view_rect


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _state(
    *,
    available: bool = True,
    save_view: bool = False,
    view_rect: tuple[int, int, int, int] | None = (400, 300, 800, 600),
    view_native_size: tuple[int, int] | None = None,
    crop_available: bool = False,
    crop: bool = False,
    crop_size: tuple[int, int] | None = None,
) -> ExportDialogState:
    return ExportDialogState(
        current_language="en",
        output_dir="/tmp",
        favorite_dir=None,
        last_format="PNG",
        quality=95,
        png_compress_level=9,
        fill_background=False,
        background_color=Color(255, 255, 255, 255),
        comment_text="",
        comment_keep_default=False,
        resolution_scale=1.0,
        virtual_canvas_active=True,
        crop_to_image=crop,
        crop_to_image_available=crop_available,
        crop_native_size=crop_size,
        save_view=save_view,
        save_view_available=available,
        view_rect=view_rect,
        view_native_size=view_native_size,
    )


def _dialog(state, native=(1600, 1200), preview=None) -> ExportDialog:
    add_i18n_root(Path("src/plugins/export/resources/i18n"))
    dialog = ExportDialog(
        dialog_state=state,
        suggested_filename="out",
        preview_image=preview,
        native_size=native,
    )
    dialog.show()
    QApplication.processEvents()
    return dialog


def _sizes(dialog) -> tuple[str, str]:
    return (dialog.edit_width.text(), dialog.edit_height.text())


def test_option_is_hidden_and_inert_without_a_view_rect(qapp):
    dialog = _dialog(_state(available=False, save_view=True))
    assert dialog.checkbox_save_view.isVisible() is False
    # A remembered/stale preference must not leak into the written options
    # nor move the resolution off the full canvas.
    options = dialog.get_export_options()
    assert options["save_view"] is False
    assert options["view_rect"] is None
    assert _sizes(dialog) == ("1600", "1200")


def test_option_reports_the_remembered_choice(qapp):
    dialog = _dialog(_state(save_view=True, view_rect=(400, 300, 800, 600)))
    assert dialog.checkbox_save_view.isVisible() is True
    assert dialog.checkbox_save_view.isChecked() is True
    # Catalogue text, not a raw key, and readable as what it does.
    assert dialog.checkbox_save_view.text() == "Save view"
    assert "native resolution" in dialog.checkbox_save_view.toolTip()
    # Find Action can pulse the control (same tag shape as "crop to images").
    assert (
        dialog.checkbox_save_view.property("action_search_member")
        == "export.save_view"
    )
    options = dialog.get_export_options()
    assert options["save_view"] is True
    assert options["view_rect"] == (400, 300, 800, 600)
    # Fields describe the view at the remembered scale (1.0 here).
    assert _sizes(dialog) == ("800", "600")


def test_the_option_and_crop_to_images_are_mutually_exclusive(qapp):
    dialog = _dialog(
        _state(crop_available=True, crop_size=(1200, 400)),
        native=(1600, 1200),
    )
    dialog.checkbox_save_view.setChecked(True)
    QApplication.processEvents()
    assert dialog.checkbox_crop_to_image.isChecked() is False

    dialog.checkbox_crop_to_image.setChecked(True)
    QApplication.processEvents()
    assert dialog.checkbox_save_view.isChecked() is False
    # One base at a time: the fields describe the image area again.
    assert _sizes(dialog) == ("1200", "400")
    options = dialog.get_export_options()
    assert options["crop_to_image"] is True
    assert options["save_view"] is False
    assert options["view_rect"] is None

    # ... and no signal loop: the surviving option stays checked.
    assert dialog.checkbox_crop_to_image.isChecked() is True


def test_toggling_keeps_the_user_scale_and_swaps_the_base(qapp):
    dialog = _dialog(_state(), native=(1600, 1200))
    assert _sizes(dialog) == ("1600", "1200")

    # 0.5 scale against the full canvas.
    dialog.edit_width.setText("800")
    dialog._on_width_edited()
    assert _sizes(dialog) == ("800", "600")

    dialog.checkbox_save_view.setChecked(True)
    QApplication.processEvents()
    # Same 0.5 scale, now against the visible view (800x600 canvas px).
    assert _sizes(dialog) == ("400", "300")
    assert dialog.get_export_options()["resolution_scale"] == pytest.approx(0.5)

    dialog.checkbox_save_view.setChecked(False)
    QApplication.processEvents()
    assert _sizes(dialog) == ("800", "600")
    assert _sizes(dialog) == ("800", "600")


def test_native_size_is_derived_from_the_rect_when_a_tab_omits_it(qapp):
    dialog = _dialog(_state(save_view=True, view_native_size=None))
    assert dialog._effective_native_size() == (800, 600)
    assert _sizes(dialog) == ("800", "600")


def test_view_rect_matches_the_shared_geometry_helper(qapp):
    """The dialog must not re-derive view geometry: the tab's rect is the seam."""
    rect = visible_view_rect(1600, 1200, zoom=2.0)
    dialog = _dialog(
        _state(save_view=True, view_rect=rect, view_native_size=(rect[2], rect[3]))
    )
    assert dialog._view_rect() == rect
    assert dialog._effective_native_size() == (rect[2], rect[3])


def test_preview_shows_the_crop_defined_by_the_view_rect(qapp):
    source = QPixmap(800, 600)
    source.fill()
    dialog = _dialog(
        _state(save_view=False, view_rect=(400, 300, 800, 600)),
        native=(1600, 1200),
        preview=source,
    )
    # Full preview while the option is off.
    assert dialog._effective_preview_source().size() == source.size()

    dialog.checkbox_save_view.setChecked(True)
    QApplication.processEvents()
    shown = dialog._effective_preview_source()
    # Half of the canvas in each direction → half of the preview raster.
    assert (shown.width(), shown.height()) == (400, 300)

    dialog.checkbox_save_view.setChecked(False)
    QApplication.processEvents()
    assert dialog._effective_preview_source().size() == source.size()


def test_preview_falls_back_to_full_canvas_without_a_preview(qapp):
    dialog = _dialog(_state(save_view=True), preview=None)
    assert dialog._effective_preview_source() is None


def _manager_for(ini_path, org, app) -> SettingsManager:
    settings = QSettings(ini_path, QSettings.Format.IniFormat)
    manager = SettingsManager(org, app)
    manager.settings = settings
    manager._backup_path = settings.fileName() + ".backup"
    return manager


def test_setting_survives_a_manager_round_trip(qapp, tmp_path):
    """``export_save_view`` persists through the typed load/save pair."""
    ini_path = str(tmp_path / "save-view.ini")
    writer = _manager_for(ini_path, "improve-imgsli-save-view-w", "save-view-w")
    store = Store()
    store.settings.export_save_view = True
    writer.save_all_settings(store)
    writer.settings.sync()
    qapp.processEvents()

    fresh = _manager_for(ini_path, "improve-imgsli-save-view-r", "save-view-r")
    reloaded = Store()
    fresh.load_all_settings(reloaded)
    assert reloaded.settings.export_save_view is True
    # The sibling option keeps its own value (no key collision).
    assert reloaded.settings.export_crop_to_image is False
