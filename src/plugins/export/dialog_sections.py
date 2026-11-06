"""Widget-tree builders for ExportDialog.

Mirrors the video_editor ``dialog_sections`` pattern: builders attach widgets
to ``dialog.*`` attributes; the dialog owns lifecycle, signals targets, and
geometry via ``layout_geometry``.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIntValidator
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

from plugins.export import layout_geometry as export_geo
from plugins.export.preview_pane import build_preview_pane
from plugins.export.search import ACTIONS, BACKGROUND, OUTPUT, RESOLUTION
from shared.image_processing.format_capability import writable_formats
from sli_ui_toolkit.managers import scaled_px
from sli_ui_toolkit.widgets import (
    Button,
    CheckBox,
    ComboBox,
    CustomLineEdit,
    Text,
)
from ui.icon_manager import AppIcon
from ui.widgets.form_controls import DialogActionBar, OutputPathSection
from ui.widgets.slider_hint import ValueSlider

#: Row-label typography for the export dialog. The app registers
#: ``group-title`` as 14 design px / regular (ui/icon_manager.py) — the same
#: scale the video editor's export labels use. The toolkit ``body`` default
#: (12 px) painted these labels visibly smaller than the value text, checkbox
#: captions and buttons in the same dialog.
_LABEL_VARIANT = "group-title"


def build_output_path_section(dialog) -> None:
    section = OutputPathSection(
        directory_label_text=dialog._tr("label.output_directory", "Output directory")
        + ":",
        browse_text=dialog._tr("button.browse", "Browse..."),
        set_favorite_text=dialog._tr("misc.set_as_favorite", "Set as Favorite"),
        use_favorite_text=dialog._tr("tooltip.use_favorite", "Use Favorite"),
        filename_label_text=dialog._tr("label.file_name", "File name") + ":",
        on_browse=dialog._choose_directory,
        on_set_favorite=dialog._set_favorite_from_current,
        on_use_favorite=dialog._use_favorite_dir,
        use_custom_line_edit=True,
        filename_editor_factory=CustomLineEdit,
    )
    section.apply_to(dialog)
    OUTPUT.tag_member(dialog.btn_browse_dir, "button.browse")
    OUTPUT.tag_member(dialog.btn_set_favorite, "misc.set_as_favorite")
    OUTPUT.tag_member(dialog.btn_use_favorite, "tooltip.use_favorite")
    dialog._harden_text_buttons(
        dialog.btn_browse_dir,
        dialog.btn_set_favorite,
        dialog.btn_use_favorite,
    )


def build_format_row(dialog) -> None:
    dialog.fmt_label = Text(
        dialog._tr("label.format", "Format") + ":",
        mode="plain",
        variant=_LABEL_VARIANT,
    )
    dialog.combo_format = ComboBox()
    # Offer only formats this stack can really write (shared.
    # image_processing.format_capability): a format whose encoder is missing
    # is dropped from the combo rather than failing after the user clicks OK
    # (that is how JXL crashed the multi-compare save: offered to both tabs,
    # Pillow-only path for both, KeyError 'JXL').
    for fmt in writable_formats():
        dialog.combo_format.addItem(fmt)
    dialog.combo_format.currentIndexChanged.connect(
        dialog._update_controls_visity_by_format
    )


def build_resolution_row(dialog) -> None:
    from shared.image_processing.progressive_loader import (
        max_supported_image_dimension,
    )

    dialog.resolution_row = QWidget()
    res_layout = QHBoxLayout(dialog.resolution_row)
    res_layout.setContentsMargins(0, 0, 0, 0)
    res_layout.setSpacing(scaled_px(8))
    dialog.resolution_label = Text(
        dialog._tr("label.resolution", "Resolution") + ":",
        mode="plain",
        variant=_LABEL_VARIANT,
    )
    res_layout.addWidget(dialog.resolution_label)
    dialog.edit_width = CustomLineEdit()
    dialog.edit_width.setObjectName("export-width")
    _max_dim = max_supported_image_dimension()
    dialog.edit_width.setValidator(QIntValidator(1, _max_dim))
    dialog.edit_width.setFixedWidth(scaled_px(72))
    dialog.edit_width.setAlignment(Qt.AlignmentFlag.AlignCenter)
    dialog.edit_height = CustomLineEdit()
    dialog.edit_height.setObjectName("export-height")
    dialog.edit_height.setValidator(QIntValidator(1, _max_dim))
    dialog.edit_height.setFixedWidth(scaled_px(72))
    dialog.edit_height.setAlignment(Qt.AlignmentFlag.AlignCenter)
    dialog.btn_lock_ratio = Button(
        icon=(AppIcon.UNLINK, AppIcon.LINK), toggle=True, size=(32, 32)
    )
    dialog.btn_lock_ratio.setChecked(True)
    RESOLUTION.tag_member(dialog.btn_lock_ratio, "export.lock_aspect_ratio")
    res_layout.addWidget(dialog.edit_width)
    res_layout.addWidget(dialog.btn_lock_ratio)
    res_layout.addWidget(dialog.edit_height)
    res_layout.addStretch()

    base_w, base_h = dialog._effective_native_size()
    if base_w > 0 and base_h > 0:
        initial_w = max(1, int(round(base_w * dialog._initial_scale)))
        initial_h = max(1, int(round(base_h * dialog._initial_scale)))
    else:
        initial_w, initial_h = 1920, 1080
    dialog.edit_width.setText(str(initial_w))
    dialog.edit_height.setText(str(initial_h))
    dialog.edit_width.editingFinished.connect(dialog._on_width_edited)
    dialog.edit_height.editingFinished.connect(dialog._on_height_edited)
    dialog.resolution_row.setVisible(
        dialog._native_width > 0 and dialog._native_height > 0
    )


def build_quality_controls(dialog) -> None:
    dialog.quality_row = QWidget()
    quality_layout = QHBoxLayout(dialog.quality_row)
    quality_layout.setContentsMargins(0, 0, 0, 0)
    quality_layout.setSpacing(scaled_px(8))
    dialog.quality_label = Text(
        dialog._tr("label.quality", "Quality") + ":",
        mode="plain",
        variant=_LABEL_VARIANT,
    )
    dialog.slider_quality = ValueSlider(
        Qt.Orientation.Horizontal,
        # Raw value (matches the label next to it) instead of the default
        # percent-of-span text.
        hint_formatter=lambda s: str(s.value()),
    )
    dialog.slider_quality.setRange(1, 100)
    dialog.slider_quality.setValue(95)
    dialog.label_quality_value = Text(
        "95", mode="plain", variant=_LABEL_VARIANT
    )
    dialog.slider_quality.valueChanged.connect(
        lambda v: dialog.label_quality_value.setText(str(v))
    )
    quality_layout.addWidget(dialog.quality_label)
    quality_layout.addWidget(dialog.slider_quality, 1)
    quality_layout.addWidget(dialog.label_quality_value)


def build_png_options(dialog) -> None:
    dialog.png_row = QWidget()
    png_layout = QHBoxLayout(dialog.png_row)
    png_layout.setContentsMargins(0, 0, 0, 0)
    png_layout.setSpacing(scaled_px(8))
    dialog.label_png_compress = Text(
        dialog._tr("export.png_compression_level", "PNG Compression Level") + ":",
        mode="plain",
        variant=_LABEL_VARIANT,
    )
    dialog.slider_png_compress = ValueSlider(
        Qt.Orientation.Horizontal,
        hint_formatter=lambda s: str(s.value()),
    )
    dialog.slider_png_compress.setRange(0, 9)
    dialog.slider_png_compress.setValue(9)
    dialog.label_png_compress_value = Text(
        "9", mode="plain", variant=_LABEL_VARIANT
    )
    dialog.slider_png_compress.valueChanged.connect(
        lambda v: dialog.label_png_compress_value.setText(str(v))
    )
    dialog.checkbox_png_optimize = CheckBox(
        dialog._tr("export.optimize_png", "Optimize PNG")
    )
    png_layout.addWidget(dialog.label_png_compress)
    png_layout.addWidget(dialog.slider_png_compress, 1)
    png_layout.addWidget(dialog.label_png_compress_value)
    png_layout.addWidget(dialog.checkbox_png_optimize)


def build_background_row(dialog) -> None:
    dialog.checkbox_fill_bg = CheckBox(
        dialog._tr("export.fill_background", "Fill background")
    )

    dialog.btn_bg_color = Button(
        text=dialog._tr("export.background_color", "Background Color"),
        variant="surface",
    )
    dialog.btn_bg_color.setMinimumHeight(scaled_px(32))
    dialog.btn_bg_color.clicked.connect(dialog._pick_bg_color)

    dialog.checkbox_fill_bg.toggled.connect(dialog._on_fill_background_toggled)
    BACKGROUND.tag_member(dialog.checkbox_fill_bg, "export.fill_background")
    BACKGROUND.tag_member(dialog.btn_bg_color, "export.select_background_color")
    dialog.bg_color_row = QWidget()
    bg_row = QVBoxLayout(dialog.bg_color_row)
    bg_row.setContentsMargins(0, 0, 0, 0)
    bg_row.setSpacing(scaled_px(6))
    bg_row.addWidget(dialog.checkbox_fill_bg)
    bg_row.addWidget(dialog.btn_bg_color)
    dialog.current_bg_color = QColor(255, 255, 255, 255)


def build_crop_row(dialog) -> None:
    """Image Compare: trim the file to the image area (pads/letterbox dropped).

    Hidden unless the tab declares ``crop_to_image_available`` — the rect the
    crop uses is tab-owned, so a tab without one must not offer the option.
    """
    dialog.checkbox_crop_to_image = CheckBox(
        dialog._tr("export.crop_to_image", "Crop to images")
    )
    dialog.checkbox_crop_to_image.setToolTip(
        dialog._tr(
            "export.crop_to_image_tooltip",
            "Keep only the area the images occupy; empty margins are trimmed.",
        )
    )
    dialog.checkbox_crop_to_image.toggled.connect(
        dialog._on_crop_to_image_toggled
    )
    BACKGROUND.tag_member(dialog.checkbox_crop_to_image, "export.crop_to_image")


def build_metadata_block(dialog) -> None:
    dialog.checkbox_include_metadata = CheckBox(
        dialog._tr("export.include_metadata", "Include metadata")
    )
    dialog.checkbox_include_metadata.toggled.connect(
        dialog._on_include_metadata_toggled
    )
    ACTIONS.tag_member(dialog.checkbox_include_metadata, "export.include_metadata")

    dialog.comment_label = Text(
        dialog._tr("export.comment", "Comment") + ":",
        mode="plain",
        variant=_LABEL_VARIANT,
    )
    dialog.edit_comment = CustomLineEdit()
    dialog.edit_comment.setObjectName("export-comment")
    dialog.checkbox_comment_default = CheckBox(
        dialog._tr("export.remember_by_default", "Remember by default")
    )


def build_action_bar(dialog) -> None:
    dialog.action_bar = DialogActionBar(
        dialog._tr("common.ok", "OK"),
        dialog._tr("common.cancel", "Cancel"),
        primary_min_size=(100, 36),
        secondary_min_size=(100, 36),
    )
    dialog.btn_ok = dialog.action_bar.primary_button
    dialog.btn_cancel = dialog.action_bar.secondary_button
    dialog.btn_ok.clicked.connect(dialog.accept)
    dialog.btn_cancel.clicked.connect(dialog.reject)
    ACTIONS.tag_member(dialog.btn_ok, "common.ok")
    ACTIONS.tag_member(dialog.btn_cancel, "common.cancel")


def assemble_export_form(dialog) -> tuple[QFrame, QFrame]:
    """Build preview + form panes and return ``(left_frame, right_frame)``."""
    left_frame = build_preview_pane(dialog)

    right_frame = QFrame()
    right_frame.setFrameShape(QFrame.Shape.NoFrame)
    dialog.export_form_frame = right_frame
    right_layout = QVBoxLayout(right_frame)
    right_layout.setContentsMargins(scaled_px(8), scaled_px(8), scaled_px(8), scaled_px(8))
    # Base gap between stacked rows; extra height goes to addStretch slots.
    right_layout.setSpacing(scaled_px(8))

    build_output_path_section(dialog)
    build_format_row(dialog)
    build_resolution_row(dialog)
    build_quality_controls(dialog)
    build_png_options(dialog)
    build_background_row(dialog)
    build_crop_row(dialog)
    build_metadata_block(dialog)
    build_action_bar(dialog)

    # Distribute extra height across section gaps (not one blob above OK).
    # Intra-section rows (label→field) stay tight via layout spacing only.
    right_layout.addWidget(dialog.output_section)
    # Fixed floor + shared stretch so filename→format never collapses to
    # the generic 8px row spacing.
    right_layout.addSpacing(export_geo.export_filename_format_min_gap())
    right_layout.addStretch(1)
    right_layout.addWidget(dialog.fmt_label)
    right_layout.addWidget(dialog.combo_format)
    right_layout.addStretch(1)
    right_layout.addWidget(dialog.resolution_row)
    right_layout.addStretch(1)
    right_layout.addWidget(dialog.quality_row)
    right_layout.addStretch(1)
    right_layout.addWidget(dialog.png_row)
    right_layout.addStretch(1)
    right_layout.addWidget(dialog.bg_color_row)
    right_layout.addWidget(dialog.checkbox_crop_to_image)
    right_layout.addStretch(1)
    right_layout.addWidget(dialog.checkbox_include_metadata)
    right_layout.addWidget(dialog.comment_label)
    right_layout.addWidget(dialog.edit_comment)
    right_layout.addWidget(dialog.checkbox_comment_default)
    right_layout.addStretch(1)
    right_layout.addWidget(dialog.action_bar)

    return left_frame, right_frame


def assemble_export_ui(dialog) -> None:
    main_layout = QHBoxLayout(dialog)
    main_layout.setContentsMargins(scaled_px(12), scaled_px(12), scaled_px(12), scaled_px(12))
    main_layout.setSpacing(scaled_px(12))

    left_frame, right_frame = assemble_export_form(dialog)
    main_layout.addWidget(left_frame, 1)
    main_layout.addWidget(right_frame, 0)

    dialog._on_include_metadata_toggled(dialog.checkbox_include_metadata.isChecked())