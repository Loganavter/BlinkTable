from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SettingsDialogData:
    language: str
    theme: str
    max_name_length: int
    debug_enabled: bool
    system_notifications_enabled: bool
    ui_font_mode: str
    ui_font_family: str
    ui_scale_factor: float
    optimize_magnifier_movement: bool
    magnifier_interpolation_method: str
    optimize_laser_smoothing: bool
    laser_interpolation_method: str
    zoom_interpolation_method: str
    magnifier_intersection_highlight_enabled: bool
    magnifier_auto_color_new_instances: bool
    auto_calculate_psnr: bool
    auto_calculate_ssim: bool
    auto_crop_black_borders: bool
    ui_mode: str
    video_recording_fps: int
    rhi_backend: str = "default"
    keyboard_overrides: dict[str, str] = field(default_factory=dict)
    tab_extras: dict[str, dict[str, Any]] = field(default_factory=dict)

    def get_section(self, section_id: str) -> dict[str, Any]:
        return self.tab_extras.get(section_id, {})

    def set_section(self, section_id: str, values: dict[str, Any]) -> None:
        self.tab_extras[section_id] = dict(values)