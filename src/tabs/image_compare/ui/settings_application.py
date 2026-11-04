from __future__ import annotations

from typing import Callable

from tabs.image_compare.canvas.registry import registry


class ImageCompareSettingsApplication:
    def __init__(
        self,
        store,
        data,
        render_update_needed: bool,
        save_setting: Callable,
        emit_update_requested: Callable,
        event_bus=None,
    ):
        self.store = store
        self.data = data
        self.render_update_needed = bool(render_update_needed)
        self._save_setting = save_setting
        self._emit_update_requested = emit_update_requested
        self.event_bus = event_bus

    def apply(self) -> bool:
        self._apply_magnifier_movement_optimization()
        self._apply_magnifier_interpolation()
        self._apply_magnifier_behavior_settings()
        self._apply_magnifier_stroke()
        self._apply_metrics_settings()
        return self.render_update_needed

    def _apply_metrics_settings(self) -> bool:
        image_state = self.store.viewport.session_data.image_state
        if image_state is None:
            return False

        from core.state_management.actions import (
            SetAutoCalculatePsnrAction,
            SetAutoCalculateSsimAction,
        )
        from plugins.settings.events import SettingsAnalysisMetricsRequestedEvent

        dispatcher = self.store.get_dispatcher()
        metrics_changed = False

        if self.data.auto_calculate_psnr != image_state.auto_calculate_psnr:
            dispatcher.dispatch(
                SetAutoCalculatePsnrAction(self.data.auto_calculate_psnr),
                scope="viewport",
            )
            self._save_setting("auto_calculate_psnr", self.data.auto_calculate_psnr)
            metrics_changed = True

        if self.data.auto_calculate_ssim != image_state.auto_calculate_ssim:
            dispatcher.dispatch(
                SetAutoCalculateSsimAction(self.data.auto_calculate_ssim),
                scope="viewport",
            )
            self._save_setting("auto_calculate_ssim", self.data.auto_calculate_ssim)
            metrics_changed = True

        if metrics_changed:
            self.store.emit_state_change("viewport")
            if self.event_bus is not None:
                updated_state = self.store.viewport.session_data.image_state
                self.event_bus.emit(
                    SettingsAnalysisMetricsRequestedEvent(
                        payload={
                            "psnr": updated_state.auto_calculate_psnr,
                            "ssim": updated_state.auto_calculate_ssim,
                        }
                    )
                )
        return metrics_changed

    def _execute_alias(self, alias: str, *args):
        command = registry().get_feature_command_by_alias(alias)
        if command is None:
            return None
        return command(*args)

    def _apply_magnifier_movement_optimization(self) -> None:
        view = self.store.viewport.view_state
        value = self.data.optimize_magnifier_movement
        if value == view.optimize_interactive_movement:
            return
        if self._execute_alias(
            "overlay.settings.set_optimize_movement",
            self.store,
            value,
        ):
            self.render_update_needed = True
            self._save_setting("optimize_magnifier_movement", value)

    def _apply_magnifier_interpolation(self) -> None:
        method = self.data.magnifier_interpolation_method
        if not self._execute_alias(
            "overlay.settings.set_movement_interpolation",
            self.store,
            method,
        ):
            return

        self.store.emit_state_change()
        self._save_setting("magnifier_movement_interpolation_method", method)
        self._emit_update_requested()
        self.render_update_needed = True

    def _apply_magnifier_stroke(self) -> None:
        """Loupe outline + detection-ring thickness (settings window, 2026-11-04)."""
        section = {}
        try:
            section = dict(self.data.get_section("image_compare.analysis") or {})
        except Exception:
            section = {}
        border = int(section.get("magnifier_border_thickness", 2))
        capture = int(section.get("magnifier_capture_thickness", 2))
        changes = self._execute_alias(
            "overlay.settings.set_stroke",
            self.store,
            border,
            capture,
        )
        if not changes:
            return
        if "border_thickness" in changes:
            self._save_setting("magnifier_border_thickness", int(changes["border_thickness"]))
        if "capture_thickness" in changes:
            self._save_setting("magnifier_capture_thickness", int(changes["capture_thickness"]))
        self.render_update_needed = True
        self._emit_update_requested()

    def _apply_magnifier_behavior_settings(self) -> None:
        changes = self._execute_alias(
            "overlay.settings.apply_behavior",
            self.store,
            {
                "intersection_highlight_enabled": (
                    self.data.magnifier_intersection_highlight_enabled
                ),
                "auto_color_new_instances": (
                    self.data.magnifier_auto_color_new_instances
                ),
            },
        )
        if not changes:
            return
        if "intersection_highlight_enabled" in changes:
            self._save_setting(
                "magnifier.intersection_highlight.enabled",
                changes["intersection_highlight_enabled"],
            )
        if "auto_color_new_instances" in changes:
            self._save_setting(
                "magnifier.auto_color_new_instances.enabled",
                changes["auto_color_new_instances"],
            )
        self.store.emit_state_change("viewport")
        self._emit_update_requested()
        self.render_update_needed = True
