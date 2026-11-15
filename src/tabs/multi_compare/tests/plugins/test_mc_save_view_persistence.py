"""The "save view" choice must survive the dialog (Multi Compare side).

The checkbox is a dialog option, but quick save (Ctrl+S) has no dialog: it
reads ``settings.export_save_view``. If nothing writes that scalar, the
remembered choice never exists and quick save silently exports the whole
canvas while the dialog promised the visible region.

Both directions are pinned here: the accepted options persist the scalar
(through the dispatched action, not a direct ``settings.*`` write — the
direct-write ratchet pins the legacy assignments in that function by line
number), and the next dialog seeds its checkbox from it.
"""

from __future__ import annotations

from types import SimpleNamespace

from core.state_management.settings_actions import SetExportSaveViewAction
from tabs.multi_compare.use_cases import export as mc_export


class _Dispatcher:
    def __init__(self):
        self.actions = []

    def dispatch(self, action, scope=None):
        self.actions.append(action)
        return action


def _settings(**overrides):
    base = dict(
        export_default_dir="/tmp",
        export_favorite_dir=None,
        export_last_format="PNG",
        export_quality=95,
        export_png_compress_level=9,
        export_png_optimize=True,
        export_fill_background=False,
        export_resolution_scale=1.0,
        export_comment_text="",
        export_comment_keep_default=False,
        export_background_color=None,
        export_save_view=False,
        current_language="en",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _controller(settings):
    dispatcher = _Dispatcher()
    state = SimpleNamespace(
        root=None,
        slots=[],
        zoom=1.0,
        pan_x=0.0,
        pan_y=0.0,
        scene_zoom=1.0,
        scene_pan_x=0.0,
        scene_pan_y=0.0,
    )
    return SimpleNamespace(
        widget=SimpleNamespace(state=state),
        store=SimpleNamespace(settings=settings, get_dispatcher=lambda: dispatcher),
        _native_canvas_size=lambda: (800, 600),
        dispatcher=dispatcher,
    )


def _options(**overrides):
    options = dict(
        output_dir="/tmp",
        format="PNG",
        quality=95,
        png_compress_level=9,
        png_optimize=True,
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
    settings = _settings()
    controller = _controller(settings)

    mc_export.persist_export_preferences(controller, _options(save_view=True))

    written = [
        a for a in controller.dispatcher.actions
        if isinstance(a, SetExportSaveViewAction)
    ]
    assert len(written) == 1, "the choice must reach settings, or quick save cannot honour it"
    assert written[0].enabled is True


def test_unchecking_save_view_persists_off():
    settings = _settings(export_save_view=True)
    controller = _controller(settings)

    mc_export.persist_export_preferences(controller, _options(save_view=False))

    written = [
        a for a in controller.dispatcher.actions
        if isinstance(a, SetExportSaveViewAction)
    ]
    assert written and written[-1].enabled is False


def test_dialog_state_seeds_the_remembered_choice():
    controller = _controller(_settings(export_save_view=True))

    fields = mc_export.export_dialog_state_kwargs(controller)

    assert fields["save_view"] is True
    # The camera is at identity here, so the dialog still hides the option:
    # a remembered True must never become an invisible crop.
    assert fields["save_view_available"] is False


def test_dialog_state_seeds_off_when_unset():
    controller = _controller(_settings())

    fields = mc_export.export_dialog_state_kwargs(controller)

    assert fields["save_view"] is False
