from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from shared.video_policy import FpsPolicy


@dataclass(slots=True)
class SettingsDialogContext:
    current_language: str
    current_theme: str
    current_max_length: int
    min_limit: int
    max_limit: int
    debug_mode_enabled: bool
    system_notifications_enabled: bool
    tr_func: Callable
    current_ui_font_mode: str = "builtin"
    current_ui_font_family: str = ""
    current_ui_scale_factor: float = 1.0
    current_ui_mode: str = "beginner"
    optimize_magnifier_movement: bool = True
    movement_interpolation_method: str = "BILINEAR"
    optimize_laser_smoothing: bool = False
    interpolation_method: str = "LANCZOS"
    zoom_interpolation_method: str = "BILINEAR"
    magnifier_intersection_highlight_enabled: bool = True
    magnifier_auto_color_new_instances: bool = True
    auto_calculate_psnr: bool = False
    auto_calculate_ssim: bool = False
    auto_crop_black_borders: bool = True
    current_video_fps: int = FpsPolicy.DEFAULT
    rhi_backend: str = "default"
    store: object | None = None
    keyboard_overrides: dict[str, str] = field(default_factory=dict)
    tab_extras: dict[str, dict[str, Any]] = field(default_factory=dict)

    def get_section(self, section_id: str) -> dict[str, Any]:
        return self.tab_extras.get(section_id, {})