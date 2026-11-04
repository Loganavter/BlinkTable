from __future__ import annotations

import logging

from core.constants import AppConstants
from core.log_names import APP_LOGGER_NAME

logger = logging.getLogger(APP_LOGGER_NAME)

_INTERPOLATION_KEY_MAP = {
    "NEAREST": "magnifier.nearest_neighbor",
    "BILINEAR": "magnifier.bilinear",
    "BICUBIC": "magnifier.bicubic",
    "LANCZOS": "magnifier.lanczos",
    "EWA_LANCZOS": "magnifier.ewa_lanczos",
}

_THEME_KEYS = ("auto", "light", "dark")


def apply_translations(dialog, lang: str) -> None:
    title = dialog._tr("misc.settings", lang)
    dialog.setWindowTitle(title)
    title_bar = getattr(dialog, "_csd_title_bar", None)
    set_title = getattr(title_bar, "set_title", None) if title_bar is not None else None
    if callable(set_title):
        set_title(title)
    _apply_buttons(dialog, lang)
    _apply_simple_texts(dialog, lang)
    _apply_group_titles(dialog, lang)
    _rebuild_theme_combo(dialog, lang)
    _rebuild_font_family_combo(dialog, lang)
    _rebuild_interpolation_combos(dialog, lang)
    _rebuild_rhi_backend_combo(dialog, lang)


def _apply_buttons(dialog, lang: str) -> None:
    dialog.ok_button.setText(dialog._tr("common.ok", lang))
    dialog.cancel_button.setText(dialog._tr("common.cancel", lang))


def _apply_simple_texts(dialog, lang: str) -> None:
    candidates = [
        ("radio_en", "settings.language_en"),
        ("radio_ru", "settings.language_ru"),
        ("radio_zh", "settings.language_zh"),
        ("radio_pt_br", "settings.language_pt_br"),
        ("system_notifications_checkbox", "settings.system_notifications"),
        ("debug_checkbox", "settings.enable_debug_logging"),
        ("radio_ui_mode_beginner", "settings.ui_mode_beginner"),
        ("radio_ui_mode_advanced", "settings.ui_mode_advanced"),
        ("radio_ui_mode_expert", "settings.ui_mode_expert"),
        ("radio_font_builtin", "settings.builtin_font"),
        ("radio_font_system_default", "settings.system_default"),
        ("radio_font_system_custom", "settings.custom"),
        ("optimize_movement_checkbox", "settings.optimize_magnifier_movement"),
        ("laser_smoothing_checkbox", "settings.optimize_laser_smoothing"),
        (
            "magnifier_intersection_highlight_checkbox",
            "settings.magnifier_intersection_highlight",
        ),
        (
            "magnifier_auto_color_checkbox",
            "settings.magnifier_auto_color_new_instances",
        ),
        ("crop_checkbox", "settings.autocrop_black_borders_on_load"),
        ("auto_psnr_checkbox", "settings.autocalculate_psnr"),
        ("auto_ssim_checkbox", "settings.autocalculate_ssim"),
        ("lbl_zoom_interp", "settings.zoom_interpolation"),
        ("lbl_rhi_backend_hint", "settings.render_backend_restart_hint"),
    ]
    for attr, key in candidates:
        widget = getattr(dialog, attr, None)
        if widget is not None:
            widget.setText(dialog._tr(key, lang))


def _apply_group_titles(dialog, lang: str) -> None:
    groups = [
        ("lang_group", "label.language"),
        ("sys_group", "settings.appearance"),
        ("general_group", "settings.general"),
        ("appearance_group", "settings.appearance"),
        ("ui_mode_group", "settings.ui_mode"),
        ("font_group", "settings.ui_font"),
        ("other_ui_group", "settings.maximum_name_length_ui"),
        ("interactive_opt_group", "settings.interactive_optimization"),
        ("render_backend_group", "settings.render_backend"),
        ("auto_group", "settings.auto"),
        ("metrics_group", "label.details"),
    ]

    for attr_name, key in groups:
        group = getattr(dialog, attr_name, None)
        if group is not None:
            group.set_title(dialog._tr(key, lang))


_RHI_BACKEND_KEY_MAP = {
    "default": "settings.render_backend_default",
    "opengl": "settings.render_backend_opengl",
    "vulkan": "settings.render_backend_vulkan",
    "d3d11": "settings.render_backend_d3d11",
    "d3d12": "settings.render_backend_d3d12",
    "metal": "settings.render_backend_metal",
}


def _restore_combo_selection(combo, current, combo_name: str) -> None:
    """Restore ``current`` after a language rebuild, or signal the reset.

    Rebuilding clears the combo, so a value with no matching entry would
    silently reset the field to its first item. When the value survives,
    selection is preserved; otherwise a warning names the lost value so the
    reset is visible instead of silent.
    """
    if current is None:
        return
    idx = combo.findData(current)
    if idx != -1:
        combo.setCurrentIndex(idx)
        return
    logger.warning(
        "settings translations: %s lost selection %r on language rebuild — "
        "reset to first item",
        combo_name,
        current,
    )


def _rebuild_rhi_backend_combo(dialog, lang: str) -> None:
    combo = getattr(dialog, "combo_rhi_backend", None)
    if combo is None:
        return
    current = combo.currentData()
    items = [
        (combo.itemData(i), _RHI_BACKEND_KEY_MAP.get(combo.itemData(i), ""))
        for i in range(combo.count())
    ]
    combo.clear()
    for value, key in items:
        text = dialog._tr(key, lang) if key else str(value)
        combo.addItem(text, userData=value)
    _restore_combo_selection(combo, current, "combo_rhi_backend")


def _rebuild_theme_combo(dialog, lang: str) -> None:
    combo = getattr(dialog, "combo_theme", None)
    if combo is None:
        return
    current = combo.currentData()
    combo.clear()
    for key in _THEME_KEYS:
        combo.addItem(dialog._tr(f"settings.{key}", lang), key)
    _restore_combo_selection(combo, current, "combo_theme")


def _rebuild_font_family_combo(dialog, lang: str) -> None:
    from PySide6.QtGui import QFontDatabase

    combo = getattr(dialog, "combo_font_family", None)
    if combo is None:
        return
    current = combo.currentData()
    combo.clear()
    for fam in QFontDatabase.families():
        combo.addItem(fam, fam)
    _restore_combo_selection(combo, current or "", "combo_font_family")


def _rebuild_interpolation_combos(dialog, lang: str) -> None:
    if not all(
        hasattr(dialog, name)
        for name in ("combo_mag_interp", "combo_laser_interp", "combo_zoom_interp")
    ):
        return
    current_mag = dialog.combo_mag_interp.currentData()
    current_laser = dialog.combo_laser_interp.currentData()
    current_zoom = dialog.combo_zoom_interp.currentData()

    dialog.combo_mag_interp.clear()
    dialog.combo_laser_interp.clear()
    for key in AppConstants.INTERPOLATION_METHODS_MAP.keys():
        text = dialog._tr(_INTERPOLATION_KEY_MAP.get(key, key), lang)
        dialog.combo_mag_interp.addItem(text, key)
        dialog.combo_laser_interp.addItem(text, key)

    dialog.combo_zoom_interp.clear()
    for key in ("NEAREST", "BILINEAR"):
        dialog.combo_zoom_interp.addItem(
            dialog._tr(_INTERPOLATION_KEY_MAP[key], lang), key
        )

    for combo, value, name in (
        (dialog.combo_mag_interp, current_mag, "combo_mag_interp"),
        (dialog.combo_laser_interp, current_laser, "combo_laser_interp"),
        (dialog.combo_zoom_interp, current_zoom, "combo_zoom_interp"),
    ):
        _restore_combo_selection(combo, value, name)