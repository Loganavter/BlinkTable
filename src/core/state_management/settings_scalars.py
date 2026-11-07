"""Settings-scalar registry (P2 stage 2) — one record per persisted scalar.

Single source of truth linking the five places every settings scalar
touches: ``SettingsState`` field, ``ActionType`` member, ``Set*Action``
dataclass, ``SettingsReducer`` branch, and the ``SettingsManager``
typed load/save pair. The registry itself changes no behavior; the
contract test ``tests/contracts/test_settings_scalar_registry.py`` pins
every record, so adding/removing a scalar without updating all five
places fails the suite instead of drifting silently.

``key`` is the persistence key (``_get_setting``/``_save_setting`` literal),
except ``keyboard_overrides`` which persists through a dedicated JSON
load/save pair (``_load_keyboard_overrides``/``_save_keyboard_overrides``).
``SetWindowGeometryAction`` carries four scalars under one action type,
so it contributes four records.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScalarSpec:
    """One persisted settings scalar and its five touch points."""

    key: str
    setting_field: str
    action_type: str
    action_class: str
    action_field: str


SETTINGS_SCALARS: tuple[ScalarSpec, ...] = (
    ScalarSpec("language", "current_language", "SET_LANGUAGE", "SetLanguageAction", "language"),
    ScalarSpec("ui_mode", "ui_mode", "SET_UI_MODE", "SetUIModeAction", "mode"),
    ScalarSpec("auto_crop_black_borders", "auto_crop_black_borders", "SET_AUTO_CROP_BLACK_BORDERS", "SetAutoCropBlackBordersAction", "enabled"),
    ScalarSpec("theme", "theme", "SET_THEME", "SetThemeAction", "theme"),
    ScalarSpec("ui_font_mode", "ui_font_mode", "SET_UI_FONT_MODE", "SetUIFontModeAction", "mode"),
    ScalarSpec("ui_font_family", "ui_font_family", "SET_UI_FONT_FAMILY", "SetUIFontFamilyAction", "family"),
    ScalarSpec("ui_scale_factor", "ui_scale_factor", "SET_UI_SCALE_FACTOR", "SetUIScaleFactorAction", "factor"),
    ScalarSpec("debug_mode_enabled", "debug_mode_enabled", "SET_DEBUG_MODE_ENABLED", "SetDebugModeEnabledAction", "enabled"),
    ScalarSpec("system_notifications_enabled", "system_notifications_enabled", "SET_SYSTEM_NOTIFICATIONS_ENABLED", "SetSystemNotificationsEnabledAction", "enabled"),
    ScalarSpec("video_recording_fps", "video_recording_fps", "SET_VIDEO_RECORDING_FPS", "SetVideoRecordingFpsAction", "fps"),
    ScalarSpec("rhi_backend", "rhi_backend", "SET_RHI_BACKEND", "SetRhiBackendAction", "backend"),
    ScalarSpec("window_x", "window_x", "SET_WINDOW_GEOMETRY", "SetWindowGeometryAction", "x"),
    ScalarSpec("window_y", "window_y", "SET_WINDOW_GEOMETRY", "SetWindowGeometryAction", "y"),
    ScalarSpec("window_width", "window_width", "SET_WINDOW_GEOMETRY", "SetWindowGeometryAction", "width"),
    ScalarSpec("window_height", "window_height", "SET_WINDOW_GEOMETRY", "SetWindowGeometryAction", "height"),
    ScalarSpec("window_was_maximized", "window_was_maximized", "SET_WINDOW_WAS_MAXIMIZED", "SetWindowWasMaximizedAction", "was_maximized"),
    ScalarSpec("export_favorite_dir", "export_favorite_dir", "SET_EXPORT_FAVORITE_DIR", "SetExportFavoriteDirAction", "path"),
    ScalarSpec("export_crop_to_image", "export_crop_to_image", "SET_EXPORT_CROP_TO_IMAGE", "SetExportCropToImageAction", "enabled"),
    ScalarSpec("keyboard_overrides", "keyboard_overrides", "SET_KEYBOARD_OVERRIDES", "SetKeyboardOverridesAction", "overrides"),
)
