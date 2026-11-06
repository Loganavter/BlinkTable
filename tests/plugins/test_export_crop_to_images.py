"""Export dialog: the IC-only "crop to images" option.

Contract:
- the option is offered only when the tab declares a content rect
  (``crop_to_image_available``), so Multi Compare never sees it;
- the accepted options carry the flag only when it is really available;
- toggling keeps the user's scale and re-derives the size pair from the new
  base, so the resolution fields always describe the file that will be written;
- the preview pane is cropped with the same rect the save uses.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from domain.types import Color
from plugins.export.dialog import ExportDialog
from plugins.export.models import ExportDialogState
from resources.translations import add_i18n_root


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _state(
    *,
    available: bool = True,
    crop: bool = False,
    native: tuple[int, int] | None = (1600, 1200),
    crop_size: tuple[int, int] | None = None,
    preview_image=None,
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
        crop_to_image_available=available,
        crop_native_size=crop_size,
        crop_preview_image=preview_image,
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


def test_option_is_hidden_when_the_tab_has_no_content_rect(qapp):
    dialog = _dialog(_state(available=False, crop=True))
    assert dialog.checkbox_crop_to_image.isVisible() is False
    # A stale preference must not leak into the written options.
    assert dialog.get_export_options()["crop_to_image"] is False


def test_option_reports_the_remembered_choice(qapp):
    dialog = _dialog(
        _state(crop=True, crop_size=(1200, 400)),
        native=(1600, 1200),
    )
    assert dialog.checkbox_crop_to_image.isVisible() is True
    assert dialog.checkbox_crop_to_image.isChecked() is True
    assert dialog.get_export_options()["crop_to_image"] is True
    # Fields follow the cropped base at the remembered scale (1.0 here).
    assert dialog.edit_width.text() == "1200"
    assert dialog.edit_height.text() == "400"


def test_toggling_keeps_the_scale_and_swaps_the_base(qapp):
    dialog = _dialog(
        _state(crop=False, crop_size=(1200, 400)),
        native=(1600, 1200),
    )
    assert dialog.edit_width.text() == "1600"
    dialog.edit_width.setText("800")
    dialog._on_width_edited()
    assert (dialog.edit_width.text(), dialog.edit_height.text()) == ("800", "600")

    dialog.checkbox_crop_to_image.setChecked(True)
    QApplication.processEvents()
    # Same 0.5 scale, now against the image area.
    assert (dialog.edit_width.text(), dialog.edit_height.text()) == ("600", "200")
    assert dialog.get_export_options()["crop_to_image"] is True

    dialog.checkbox_crop_to_image.setChecked(False)
    QApplication.processEvents()
    assert (dialog.edit_width.text(), dialog.edit_height.text()) == ("800", "600")
    assert dialog.get_export_options()["crop_to_image"] is False


def test_preview_switches_to_the_cropped_variant(qapp):
    source = QPixmap(100, 60)
    source.fill()
    cropped_source = QPixmap(60, 30)
    cropped_source.fill()
    dialog = _dialog(
        _state(crop=False, crop_size=(60, 30), preview_image=cropped_source),
        native=(100, 60),
        preview=source,
    )
    assert dialog._effective_preview_source().size() == source.size()
    dialog.checkbox_crop_to_image.setChecked(True)
    QApplication.processEvents()
    shown = dialog._effective_preview_source()
    assert (shown.width(), shown.height()) == (60, 30)
