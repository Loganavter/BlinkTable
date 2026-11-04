from __future__ import annotations

import logging

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication

from plugins.settings.events import (
    SettingsChangeLanguageEvent,
    SettingsUIModeChangedEvent,
)
from core.state_management.actions import (
    ClearAllCachesAction,
    SetAutoCropBlackBordersAction,
    SetDebugModeEnabledAction,
    SetKeyboardOverridesAction,
    SetLanguageAction,
    SetMaxNameLengthAction,
    SetRhiBackendAction,
    SetSystemNotificationsEnabledAction,
    SetThemeAction,
    SetUIFontFamilyAction,
    SetUIFontModeAction,
    SetUIScaleFactorAction,
    SetUIModeAction,
    SetVideoRecordingFpsAction,
    SetZoomInterpolationMethodAction,
)
from shared_toolkit.ui.managers.font_manager import FontManager
from shared.video_policy import FpsPolicy, clamp_fps
from ui.theming import refresh_application_styles
from core.log_names import APP_LOGGER_NAME

from .models import SettingsDialogData
from .ui_appearance import apply_appearance


class SettingsApplicationService(QObject):
    def __init__(self, store, main_controller, event_bus=None, parent=None):
        super().__init__(parent)
        self.store = store
        self.main_controller = main_controller
        self.event_bus = event_bus

    def apply(self, data: SettingsDialogData) -> None:
        render_update_needed = self._apply_general_settings(data)
        self._apply_language_settings(data)
        self._apply_ui_font_settings(data)
        self._apply_ui_scale_settings(data)
        render_update_needed = self._apply_viewport_interactive_settings(
            data, render_update_needed
        )
        self._apply_misc_settings(data)

        # Single funnel: the settings file is only ever written as a full
        # snapshot of the Store (see SettingsManager.schedule_persist) — the
        # apply path mutates the Store via dispatches above and schedules the
        # snapshot here. Incremental per-key writes from widget state are
        # forbidden (a stale dialog widget silently overwrites good values
        # with defaults; contract test dogma 4).
        self._schedule_persist()

        if render_update_needed:
            self.store.emit_state_change()
            self._emit_update_requested()

    def _apply_general_settings(self, data: SettingsDialogData) -> bool:
        render_update_needed = False
        dispatcher = self.store.get_dispatcher()

        if data.theme != self.store.settings.theme:
            dispatcher.dispatch(SetThemeAction(data.theme))
            window_shell = (
                self.main_controller.window_shell if self.main_controller else None
            )
            if window_shell is not None:
                # The one appearance-apply path owns the theme push (and its
                # placement relative to font/scale); this call site only says
                # which axis changed.
                apply_appearance(
                    self.store,
                    theme=data.theme,
                    window=window_shell.main_window_app,
                )

        if data.max_name_length != self.store.viewport.render_config.max_name_length:
            dispatcher.dispatch(
                SetMaxNameLengthAction(data.max_name_length), scope="viewport"
            )
            render_update_needed = True

        if data.debug_enabled != self.store.settings.debug_mode_enabled:
            dispatcher.dispatch(SetDebugModeEnabledAction(data.debug_enabled))

        if (
            getattr(self.store.settings, "system_notifications_enabled", True)
            != data.system_notifications_enabled
        ):
            dispatcher.dispatch(
                SetSystemNotificationsEnabledAction(data.system_notifications_enabled)
            )
            self.store.emit_state_change("settings")
        # Always push the live store flag into NotificationService (even when
        # the checkbox did not change) so a stale _enabled cannot outlive OK.
        self._sync_notification_service_enabled()

        return render_update_needed

    def _apply_language_settings(self, data: SettingsDialogData) -> None:
        if data.language == self.store.settings.current_language:
            return
        # Real call site for SetLanguageAction: the Store is mutated via
        # dispatch (tracer-visible, history-recorded), then the ONE live
        # retranslate entry runs.
        self.store.get_dispatcher().dispatch(SetLanguageAction(data.language))
        # Retranslate from HERE, not from the ``SettingsChangeLanguageEvent``
        # subscriber: the dispatch above already moved the Store, so
        # ``SettingsController.change_language`` sees "no change" and
        # early-returns before it would ever emit — the exact path that used to
        # swallow a live Settings-OK language change
        # (plan-live-appearance-fixes-2026-10-19). This is the same single
        # entry the in-app control ``set_language`` path uses
        # (core/control/handlers.py) — do not add a second retranslate entry.
        from resources.translations import emit_language_changed

        emit_language_changed(data.language)
        # The CSD menu strip (File/Help) builds its labels from the Store
        # language, not from an i18n binding, so it must be rebuilt as part of
        # the language apply — it used to be refreshed only from the unrelated
        # keyboard-override apply.
        self._rebuild_csd_menu(data.language)
        if self.event_bus is not None:
            self.event_bus.emit(SettingsChangeLanguageEvent(data.language))
        elif (
            self.main_controller is not None
            and self.main_controller.event_bus is not None
        ):
            self.main_controller.event_bus.emit(
                SettingsChangeLanguageEvent(data.language)
            )

    def _rebuild_csd_menu(self, lang_code: str) -> None:
        """Rebuild the CSD menu strip so its labels match ``lang_code``.

        Fail-open: the strip is host chrome that may be absent (headless apply,
        tests), and a stale menu label must never break a language apply.
        """
        try:
            window = self._resolve_main_window()
            menu = None
            if window is not None:
                # Public accessor first (window.menu_controller); fall back to
                # the private attr for stub windows that only set that.
                menu = getattr(window, "menu_controller", None) or getattr(
                    window, "_menu_controller", None
                )
            rebuild = (
                getattr(menu, "_on_language_changed", None)
                if menu is not None
                else None
            )
            if callable(rebuild):
                rebuild(lang_code)
        except Exception:
            logging.getLogger(APP_LOGGER_NAME).exception(
                "Failed to rebuild the CSD menu after a language change"
            )

    def _apply_ui_scale_settings(self, data: SettingsDialogData) -> None:
        new_factor = float(getattr(data, "ui_scale_factor", 1.0) or 1.0)
        current = float(
            getattr(self.store.settings, "ui_scale_factor", 1.0) or 1.0
        )
        if abs(new_factor - current) < 1e-9:
            return

        dispatcher = self.store.get_dispatcher()
        dispatcher.dispatch(SetUIScaleFactorAction(new_factor))

        # One appearance apply: scale → QSS re-push → font re-apply, in that
        # order, inside one paint freeze + one appearance-bus batch. ``font=True``
        # carries the pinned-font re-apply the QSS push requires (see
        # apply_appearance) — the order lives in the path, not in a comment here.
        apply_appearance(
            self.store,
            scale=new_factor,
            font=True,
            app=QApplication.instance(),
        )

    def _apply_ui_font_settings(self, data: SettingsDialogData) -> None:
        font_mode_normalized = (
            "system_default" if data.ui_font_mode == "system" else data.ui_font_mode
        )
        font_mode_changed = font_mode_normalized != getattr(
            self.store.settings, "ui_font_mode", "builtin"
        )
        font_family_changed = (data.ui_font_family or "") != (
            getattr(self.store.settings, "ui_font_family", "") or ""
        )
        if not (font_mode_changed or font_family_changed):
            return

        dispatcher = self.store.get_dispatcher()
        dispatcher.dispatch(SetUIFontModeAction(font_mode_normalized))
        dispatcher.dispatch(SetUIFontFamilyAction(data.ui_font_family or ""))

        apply_appearance(self.store, font=True, app=QApplication.instance())
        app = QApplication.instance()
        if app is not None:
            refresh_application_styles(app)

        font_manager = FontManager.get_instance()

        window_shell = self.main_controller.window_shell if self.main_controller else None
        if window_shell is not None:
            try:
                main_window = window_shell.main_window_app
                if hasattr(main_window, "font_path_absolute"):
                    main_window.font_path_absolute = (
                        font_manager.get_font_path_for_image_text(self.store)
                    )
            except Exception:
                logging.getLogger(APP_LOGGER_NAME).warning(
                    "font_path_absolute sync failed after font change",
                    exc_info=True,
                )

    def _apply_viewport_interactive_settings(
        self, data: SettingsDialogData, render_update_needed: bool
    ) -> bool:
        render_update_needed = self._apply_tab_viewport_settings(
            data, render_update_needed
        )
        render_update_needed = self._apply_zoom_interpolation(
            data, render_update_needed
        )
        return render_update_needed

    def _apply_tab_viewport_settings(
        self, data: SettingsDialogData, render_update_needed: bool
    ) -> bool:
        from tabs.registry import TabRegistry

        registry = TabRegistry()
        registry.discover()
        result = registry.create_service(
            "settings_viewport_application",
            self.store,
            data,
            render_update_needed,
            # Tab viewport apply used to write its own keys incrementally;
            # it now only needs to schedule the full snapshot (the store was
            # already mutated via dispatches) — same single-funnel rule as
            # the rest of the apply path.
            lambda _key, _value: self._schedule_persist(),
            self._emit_update_requested,
            self.event_bus,
        )
        if result is None:
            return render_update_needed
        return bool(result)

    def _apply_zoom_interpolation(
        self, data: SettingsDialogData, render_update_needed: bool
    ) -> bool:
        vp = self.store.viewport
        dispatcher = self.store.get_dispatcher()
        if (
            data.zoom_interpolation_method
            == getattr(vp.render_config, "zoom_interpolation_method", "BILINEAR")
        ):
            return render_update_needed

        dispatcher.dispatch(
            SetZoomInterpolationMethodAction(data.zoom_interpolation_method),
            scope="viewport",
        )
        self.store.emit_state_change("viewport")
        self._emit_update_requested()
        return True

    def _apply_misc_settings(self, data: SettingsDialogData) -> None:
        dispatcher = self.store.get_dispatcher()

        if (
            getattr(self.store.settings, "auto_crop_black_borders", True)
            != data.auto_crop_black_borders
        ):
            dispatcher.dispatch(SetAutoCropBlackBordersAction(data.auto_crop_black_borders))
            # Инвалидируем явные кэши всех CropService (DI, без глобала)
            try:
                from shared.image_processing.autocrop import invalidate_all_services

                invalidate_all_services()
            except Exception:
                pass
            # Совместимость: старый глобальный кэш (если ещё жив)
            try:
                from shared.image_processing.autocrop_service import invalidate_all as _legacy_inv

                _legacy_inv()
            except Exception:
                pass
            # Apply-to-loaded (W5): the toggle must also re-decode images
            # that are already in the pipeline tiers — cache keys are
            # box-less, so without eviction stale crop verdicts persist.
            # Evict via the existing core pipeline action (Store-native
            # dispatch; the tab's PipelineCacheReducer owns the pixel/
            # preview tiers), then mirror the zoom-interpolation branch:
            # viewport emit + update request retriggers the natural reload
            # path (no hand re-decode).
            # NOTE: per-path EvictPipelineAction is tab-internal and the
            # host may not import tabs.* (test_ui_does_not_import_tab_internals),
            # so the global toggle evicts all pipeline tiers — every loaded
            # path is affected by a global toggle anyway.
            try:
                dispatcher.dispatch(ClearAllCachesAction(), scope="pipeline")
            except Exception:
                pass
            self.store.emit_state_change("viewport")
            self._emit_update_requested()

        if getattr(self.store.settings, "ui_mode", "beginner") != data.ui_mode:
            dispatcher.dispatch(SetUIModeAction(data.ui_mode))
            self._emit_ui_mode_changed(data.ui_mode)

        fps = clamp_fps(data.video_recording_fps)
        if (
            getattr(self.store.settings, "video_recording_fps", FpsPolicy.DEFAULT)
            != fps
        ):
            dispatcher.dispatch(SetVideoRecordingFpsAction(fps))

        prev_backend = getattr(self.store.settings, "rhi_backend", "default") or "default"
        new_backend = (data.rhi_backend or "default").strip().lower()
        if new_backend != prev_backend:
            # Dispatched (reducer-owned, tracer-visible) plus emitted: no
            # direct store.settings write. Takes effect after restart —
            # the notice below tells the user.
            dispatcher.dispatch(SetRhiBackendAction(new_backend))
            self.store.emit_state_change("settings")
            self._notify_render_backend_restart_required(new_backend)

        self._apply_keyboard_overrides(data)

    def _apply_keyboard_overrides(self, data: SettingsDialogData) -> None:
        import json

        from ui.actions.keymap import (
            exclusive_overrides,
            repair_legacy_cross_tab_unbounds,
        )
        from ui.actions.keymap_uniqueness import verify_unique_or_log
        from plugins.settings.keymap_defaults import collect_keymap_defaults

        defaults = collect_keymap_defaults()
        defaults_map = {
            entry.action_id: (entry.default_shortcut, entry.owner_tab)
            for entry in defaults.all_entries()
        }
        # Repair first: profiles poisoned by the old all-tabs conflict ranking
        # carry "" on a chord that only another TAB owns — dead in its own tab
        # too (the binder installs one tab at a time, so it never conflicted).
        new_overrides = exclusive_overrides(
            defaults_map,
            repair_legacy_cross_tab_unbounds(
                defaults_map,
                dict(getattr(data, "keyboard_overrides", None) or {}),
            ),
        )
        # Applied state must satisfy the one-chord-one-action-per-tab
        # invariant; a violation after the exclusive pass is resolver damage
        # and is reported with the colliding ids before anything is stored.
        verify_unique_or_log(
            defaults_map, new_overrides, source="settings apply keyboard overrides"
        )
        current = dict(getattr(self.store.settings, "keyboard_overrides", None) or {})
        if new_overrides == current:
            return
        dispatcher = self.store.get_dispatcher()
        dispatcher.dispatch(SetKeyboardOverridesAction(new_overrides))
        try:
            from ui.actions.binder import resync_action_shortcuts

            window_shell = (
                self.main_controller.window_shell if self.main_controller else None
            )
            window = (
                getattr(window_shell, "main_window_app", None)
                if window_shell is not None
                else None
            )
            if window is None:
                window = getattr(self.parent(), "parent_widget", None)
            resync_action_shortcuts(window, overrides=new_overrides)
            # NOTE: the CSD menu strip is (re)built only by the language apply
            # (``_apply_language_settings`` → ``_rebuild_csd_menu``); the menu
            # hook dedupes on the File/Help label, which a keyboard override
            # does not change, so calling it here was a no-op for its stated
            # purpose (shortcut labels) anyway.
        except Exception:
            import logging

            logging.getLogger(APP_LOGGER_NAME).exception(
                "Failed to resync action shortcuts after keyboard override apply"
            )

    def _notify_render_backend_restart_required(self, backend: str) -> None:
        from shared_toolkit.ui.message_dialog import AppMessageDialog

        window_shell = self.main_controller.window_shell if self.main_controller else None
        parent = getattr(window_shell, "main_window_app", None) if window_shell else None
        tr_lang = getattr(self.store.settings, "current_language", "en")
        try:
            from resources.translations import tr as app_tr
            title = app_tr("settings.render_backend_restart_title", tr_lang)
            text = app_tr("settings.render_backend_restart_message", tr_lang)
            ok_text = app_tr("common.ok", tr_lang)
        except Exception:
            title = "Restart required"
            text = "The render backend will change after restart."
            ok_text = "OK"
        AppMessageDialog.information(
            parent,
            title,
            text.format(backend=backend),
            ok_text=ok_text,
        )

    def _emit_ui_mode_changed(self, ui_mode: str) -> None:
        if self.event_bus is not None:
            self.event_bus.emit(SettingsUIModeChangedEvent(ui_mode))
        elif (
            self.main_controller is not None
            and self.main_controller.event_bus is not None
        ):
            self.main_controller.event_bus.emit(
                SettingsUIModeChangedEvent(ui_mode)
            )

    def _schedule_persist(self) -> None:
        """Schedule the debounced full-snapshot save (single funnel).

        The apply path never writes individual keys — the file is only ever
        written as a coherent snapshot of the Store (see
        ``SettingsManager.schedule_persist``), so a stale dialog widget
        cannot silently overwrite good values with its defaults.
        """
        if (
            self.main_controller is not None
            and self.main_controller.settings_manager is not None
        ):
            self.main_controller.settings_manager.schedule_persist(self.store)

    def _resolve_main_window(self):
        window_shell = (
            self.main_controller.window_shell if self.main_controller else None
        )
        window = (
            getattr(window_shell, "main_window_app", None)
            if window_shell is not None
            else None
        )
        if window is None:
            parent = self.parent()
            window = getattr(parent, "parent_widget", None) if parent is not None else None
        return window

    def _sync_notification_service_enabled(self) -> None:
        window = self._resolve_main_window()
        service = getattr(window, "notification_service", None) if window else None
        if service is None:
            return
        enabled = bool(
            getattr(self.store.settings, "system_notifications_enabled", True)
        )
        service.set_enabled(enabled)

    def _emit_update_requested(self) -> None:
        if self.main_controller is not None:
            self.main_controller.update_requested.emit()