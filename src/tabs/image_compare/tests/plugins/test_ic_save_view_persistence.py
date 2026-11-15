"""The "save view" choice must survive the dialog (Image Compare side).

Quick save has no dialog and reads ``settings.export_save_view``; if the
accepted options never write that scalar, the remembered choice does not exist
and quick save exports the whole frame while the dialog promised the visible
region.

Pinned here: the accepted options persist the scalar through the dispatched
action (the legacy ``settings.*`` assignments in the same function are pinned
by line number, so a direct write would break the ratchet), and the next
dialog seeds its checkbox from the persisted value.
"""

from __future__ import annotations

from types import SimpleNamespace

from core.state_management.settings_actions import SetExportSaveViewAction
from tabs.image_compare.services.image_export.state import ExportStateCoordinator


class _Dispatcher:
    def __init__(self):
        self.actions = []

    def dispatch(self, action, scope=None):
        self.actions.append(action)
        return action


def _settings(**overrides):
    base = dict(
        current_language="en",
        export_default_dir="/tmp",
        export_favorite_dir=None,
        export_last_format="PNG",
        export_quality=95,
        export_png_compress_level=9,
        export_png_optimize=True,
        export_fill_background=False,
        export_crop_to_image=False,
        export_save_view=False,
        export_background_color=None,
        export_comment_text="",
        export_comment_keep_default=False,
        export_resolution_scale=1.0,
        export_last_filename="x",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _coordinator(settings):
    dispatcher = _Dispatcher()
    store = SimpleNamespace(settings=settings, get_dispatcher=lambda: dispatcher)
    return ExportStateCoordinator(
        store=store, main_controller=None, main_window_app=None
    ), dispatcher


def _options(**overrides):
    options = dict(
        output_dir="/tmp",
        file_name="comparison",
        format="PNG",
        quality=95,
        png_compress_level=9,
        fill_background=False,
        background_color=(0, 0, 0, 255),
        resolution_scale=1.0,
        comment_text="",
        comment_keep_default=False,
        save_view=True,
    )
    options.update(overrides)
    return options


def test_accepting_save_view_persists_the_scalar_through_the_action():
    coordinator, dispatcher = _coordinator(_settings())

    coordinator.persist_export_settings(_options(save_view=True))

    written = [a for a in dispatcher.actions if isinstance(a, SetExportSaveViewAction)]
    assert len(written) == 1, "quick save reads this scalar; without the write it never sees the choice"
    assert written[0].enabled is True


def test_unchecking_save_view_persists_off():
    coordinator, dispatcher = _coordinator(_settings(export_save_view=True))

    coordinator.persist_export_settings(_options(save_view=False))

    written = [a for a in dispatcher.actions if isinstance(a, SetExportSaveViewAction)]
    assert written and written[-1].enabled is False


def test_dialog_state_seeds_the_remembered_choice():
    coordinator, _dispatcher = _coordinator(_settings(export_save_view=True))

    state = coordinator.build_export_dialog_state()

    assert state.save_view is True


def test_dialog_state_seeds_off_when_unset():
    coordinator, _dispatcher = _coordinator(_settings())

    state = coordinator.build_export_dialog_state()

    assert state.save_view is False
