"""Settings-OK language apply reaches the single live retranslate entry.

Owner report 2026-10-19 (plan-live-appearance-fixes-2026-10-19): a live language
change in the Settings dialog + OK did nothing. ``_apply_language_settings``
dispatched ``SetLanguageAction`` (moving the Store to the new language first)
and then only emitted ``SettingsChangeLanguageEvent``. That event's subscriber
``SettingsController.change_language`` bails at ``if not changed: return``
because the Store already holds the new value — so
``resources.translations.emit_language_changed`` (the ONE live retranslate
entry, the same one the MCP ``set_language`` path uses) was never called.

The CSD menu strip rebuild (File/Help labels) lived only in the unrelated
keyboard-override apply, so it too never ran for a language change.

These tests pin the fixed contract: the Settings-OK language apply drives the
retranslate entry AND the CSD menu rebuild, exactly once each.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugins.settings.application_service import SettingsApplicationService
from plugins.settings.events import SettingsChangeLanguageEvent
from plugins.settings.models import SettingsDialogData


class _MenuControllerStub:
    """Stands in for ``MainWindowMenuController`` — records rebuild calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def _on_language_changed(self, lang: str) -> None:
        self.calls.append(lang)


def _dialog_data(store, *, language: str) -> SettingsDialogData:
    return SettingsDialogData(
        language=language,
        theme=store.settings.theme,
        max_name_length=store.viewport.render_config.max_name_length,
        debug_enabled=store.settings.debug_mode_enabled,
        system_notifications_enabled=True,
        ui_font_mode="builtin",
        ui_font_family="",
        ui_scale_factor=1.0,
        optimize_magnifier_movement=True,
        magnifier_interpolation_method="BILINEAR",
        optimize_laser_smoothing=False,
        laser_interpolation_method="BILINEAR",
        zoom_interpolation_method="BILINEAR",
        magnifier_intersection_highlight_enabled=True,
        magnifier_auto_color_new_instances=True,
        auto_calculate_psnr=False,
        auto_calculate_ssim=False,
        auto_crop_black_borders=True,
        ui_mode="beginner",
        video_recording_fps=60,
        rhi_backend="default",
    )


def _build(monkeypatch, *, store_language: str = "en"):
    """Real Store + real EventBus + real controller wiring, with spies.

    The Store language is set through the Dispatcher (never a direct write) so
    the setup mirrors the real boot path.
    """
    from core.plugin_system.event_bus import EventBus
    from core.state_management.actions import SetLanguageAction
    from core.state_management.dispatcher import Dispatcher
    from core.store import Store

    import resources.translations as translations

    store = Store()
    store.set_dispatcher(Dispatcher(store))
    if store_language != store.settings.current_language:
        store.get_dispatcher().dispatch(SetLanguageAction(store_language))

    retranslates: list[str] = []
    # The service imports ``emit_language_changed`` from this module at call
    # time, so patching the module attribute is what the code under test sees.
    monkeypatch.setattr(
        translations, "emit_language_changed", retranslates.append
    )

    menu = _MenuControllerStub()
    window = SimpleNamespace(_menu_controller=menu)
    window_shell = SimpleNamespace(main_window_app=window)
    main_controller = SimpleNamespace(window_shell=window_shell)
    bus = EventBus()
    service = SettingsApplicationService(store, main_controller, event_bus=bus)
    return store, service, bus, retranslates, menu


def test_settings_ok_language_change_retranslates_and_rebuilds_csd_menu(monkeypatch):
    """The OK path drives retranslate + CSD rebuild, once each.

    Red before the fix: ``_apply_language_settings`` called neither, so
    ``retranslates == []`` and ``menu.calls == []``.
    """
    store, service, _bus, retranslates, menu = _build(monkeypatch, store_language="en")

    service._apply_language_settings(_dialog_data(store, language="ru"))

    assert store.settings.current_language == "ru"  # dispatch happened first
    assert retranslates == ["ru"]
    assert menu.calls == ["ru"]


def test_settings_ok_language_change_still_retranslates_when_store_already_moved(
    monkeypatch,
):
    """Regression form: the Store is updated by the apply's own dispatch first.

    This is exactly the case that silently died before the fix: by the time the
    ``SettingsChangeLanguageEvent`` reached ``SettingsController`` the Store
    already held "ru", so ``change_language`` early-returned and no retranslate
    ran. Wiring the real controller onto the real bus keeps that path alive in
    the test, so a future regression that moves the retranslate back into the
    event handler fails here.
    """
    from plugins.settings.controller import SettingsController

    store, service, bus, retranslates, menu = _build(monkeypatch, store_language="en")

    controller = SettingsController(store, None, event_bus=bus)
    bus.subscribe(SettingsChangeLanguageEvent, controller.on_change_language)

    service._apply_language_settings(_dialog_data(store, language="ru"))

    # The real controller saw the event but its store-mutation was a no-op
    # (apply dispatched first); the retranslate must still have run, once.
    assert store.settings.current_language == "ru"
    assert retranslates == ["ru"]
    assert menu.calls == ["ru"]


def test_settings_ok_language_change_is_idempotent_no_op(monkeypatch):
    """No language change -> no retranslate, no CSD rebuild, no dispatch."""
    store, service, _bus, retranslates, menu = _build(monkeypatch, store_language="en")

    before = len(store.get_dispatcher().get_action_history())
    service._apply_language_settings(_dialog_data(store, language="en"))

    assert len(store.get_dispatcher().get_action_history()) == before
    assert retranslates == []
    assert menu.calls == []
