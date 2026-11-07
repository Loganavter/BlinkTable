"""The two stroke rows must survive the real OK path, not just the widget.

Found live 2026-11-06 (leased instance, magnifier enabled, image pair up): the
spin could be driven to 7 and typed into, but pressing OK changed nothing —
``magnifier_border_thickness`` never appeared in the profile and reopening the
dialog showed 2 again.

The dialog carries tab values through ``SettingsDialogData.tab_extras``, which
``get_settings()`` fills with ``SettingsRegistry.read_payloads(dialog)``. That
reader used to be registered only inside the LEGACY
``create_service("contribute_settings", registry)`` path; production installs
sections through the typed ``SettingsContribution`` path
(``capability_routing.contribute_all_settings``), which returns a contribution
and never calls the legacy registration. So the section was empty at apply
time, ``_apply_magnifier_stroke`` fell back to its ``2/2`` defaults, the
feature command saw no change and nothing was saved.

This test drives the hop end to end with the real objects: real settings
dialog, the typed contribution install production uses, the real
``ImageCompareSettingsApplication``, and the real canvas command alias.
"""

from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtWidgets import QApplication

from plugins.settings.dialog import SettingsDialog
from plugins.settings.registry import (
    get_settings_registry,
    install_settings_contributions,
)
from tabs.image_compare.canvas.features.magnifier.state.feature_state import (
    get_magnifier_widget_state,
)
from tabs.image_compare.ui.settings_application import (
    ImageCompareSettingsApplication,
)
from tabs.image_compare.ui.settings_performance import STROKE_SECTION
from tabs.image_compare.use_cases.registration import build_settings_contribution
from ui.canvas_infra.scene.registry import (
    get_canvas_registry,
    register_canvas_feature_package,
)


def _install_production_sections() -> None:
    """The typed install path (``contribute_all_settings``) — no registry arg."""
    tab = SimpleNamespace(session_type="image_compare", i18n_namespace="image_compare")
    install_settings_contributions([build_settings_contribution(tab)])


def _register_canvas_features() -> None:
    """Same package registration ``ImageCompareTab.register_canvas_features`` runs."""
    import tabs.image_compare.canvas.features as features_pkg

    register_canvas_feature_package("image_compare", features_pkg)


def _store():
    view_state = SimpleNamespace(
        optimize_interactive_movement=True,
        canvas_widget_state={},
    )
    render_config = SimpleNamespace(
        interactive_movement_interpolation_method="BILINEAR",
        laser_smoothing_interpolation_method="BILINEAR",
        magnifier_movement_interpolation_method="BILINEAR",
    )
    store = SimpleNamespace(
        viewport=SimpleNamespace(
            view_state=view_state,
            render_config=render_config,
            session_data=SimpleNamespace(image_state=None),
        ),
    )
    store.invalidate_render_cache = lambda: None
    store.emit_state_change = lambda *_a, **_k: None
    store.emit_viewport_change = lambda *_a, **_k: None
    return store


def _dialog(store) -> SettingsDialog:
    return SettingsDialog(
        current_language="en",
        current_theme="dark",
        current_max_length=30,
        min_limit=1,
        max_limit=200,
        debug_mode_enabled=False,
        system_notifications_enabled=True,
        active_tab="image_compare",
        store=store,
    )


def test_ok_lands_the_stroke_rows_in_the_live_feature_state():
    QApplication.instance() or QApplication([])
    _register_canvas_features()
    _install_production_sections()

    store = _store()
    dialog = _dialog(store)
    try:
        spin = dialog.spin_magnifier_border_thickness
        assert spin.value() == 2  # untouched install
        spin.setValue(7)

        applied = {}

        def _on_settings_confirmed() -> None:
            # Mirrors DialogManager._on_settings_confirmed: OK emits
            # ``settings_confirmed``, the host service pushes
            # ``dialog.get_settings()`` through the tab apply service.
            data = dialog.get_settings()
            applied["section"] = dict(data.get_section(STROKE_SECTION))
            ImageCompareSettingsApplication(
                store,
                data,
                False,
                lambda *_a: None,
                lambda *_a: None,
            ).apply()

        dialog.settings_confirmed.connect(_on_settings_confirmed)
        dialog.confirm_settings()  # the OK button's own slot (dialog_shell.py)

        assert applied["section"].get("magnifier_border_thickness") == 7, (
            "the dialog -> data hop dropped the spin value"
        )
        state = get_magnifier_widget_state(store.viewport.view_state)
        assert state.default_border_thickness == 7
        assert get_canvas_registry("image_compare").get_feature_command_by_alias(
            "overlay.settings.get_stroke"
        )(store)["border_thickness"] == 7
    finally:
        dialog.deleteLater()


def test_the_section_reader_is_registered_by_the_typed_install():
    """The registry must know the section from the contribution alone.

    This is the layer that was missing: ``install_settings_contributions``
    installed the page but not the dialog<->data bridge, so ``read_payloads``
    returned ``{}`` and ``get_settings()`` produced no section.
    """
    _install_production_sections()

    registry = get_settings_registry()
    assert STROKE_SECTION in registry._payload_readers
    assert STROKE_SECTION in registry._payload_seeders
