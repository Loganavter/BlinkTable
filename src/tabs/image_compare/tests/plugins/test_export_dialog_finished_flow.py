"""Image Compare's still-image export continues in the dialog's ``finished`` handler.

The export dialog is **non-modal** now (mutter drags the parent of a modal
transient — see ``tests/plugins/test_export_dialog_nonmodal.py``), so
``ExportPresenter.save_result`` cannot read a return code from a blocking
``exec()`` any more: it opens the dialog, returns, and the export starts when
the dialog reports back.

Red-before: on the previous tree ``save_result`` unpacked
``(result_code, options)`` from the dialog call and started the save
*synchronously*, so a cancel was only expressible as a returned code. Both
tests below fail there — the accepted one because nothing is saved before the
callback, the cancel one because the synchronous path had already run the
save by the time ``save_result`` returned.
"""

from __future__ import annotations

from types import MethodType, SimpleNamespace

from PySide6.QtWidgets import QDialog

from tabs.image_compare.presenters.export_presenter import ExportPresenter


def _fake_presenter(*, has_images: bool = True):
    """A presenter-shaped object with the real methods under test bound.

    ``ExportPresenter.__init__`` builds GPU/export services and a Store-backed
    coordinator graph that have nothing to do with this wiring; the two methods
    under test only touch the collaborators assigned below. Binding them via
    ``MethodType`` keeps the exercised code the production code.
    """
    opened: list[dict] = []
    saved: list[tuple] = []
    persisted: list[dict] = []
    validated: list[dict] = []
    messages: list[tuple] = []

    save_ctx = SimpleNamespace(
        preview_img=None,
        suggested_filename="out",
        native_width=32,
        native_height=24,
        virtual_canvas_active=True,
    )

    class _Dialogs:
        @staticmethod
        def show_export_dialog(*args, **kwargs):
            opened.append({"args": args, "kwargs": kwargs})
            return object()

    fake = SimpleNamespace(
        context_builder=SimpleNamespace(
            has_images=lambda: has_images,
            build_save_context=lambda **_kw: save_ctx,
        ),
        save_flow=SimpleNamespace(
            show_missing_images_warning=lambda: messages.append(("missing",)),
            validate_export_options=lambda opts: validated.append(opts) or True,
            start_save_worker=lambda *, save_ctx, export_opts: saved.append(
                (save_ctx, export_opts)
            ),
        ),
        state=SimpleNamespace(
            build_export_dialog_state=lambda: SimpleNamespace(
                virtual_canvas_active=True
            ),
            persist_export_settings=lambda opts: persisted.append(opts),
        ),
        ui_manager=SimpleNamespace(
            dialogs=_Dialogs(),
            messages=SimpleNamespace(
                show_non_modal_message=lambda **kw: messages.append(("toast", kw))
            ),
        ),
        main_window_app=None,
        _set_export_favorite_dir=lambda _path: None,
        _tr=lambda key, default=None: default or key,
    )
    fake._open_export_dialog = MethodType(ExportPresenter._open_export_dialog, fake)
    fake._publish_view_state = MethodType(ExportPresenter._publish_view_state, fake)
    fake._canvas_view_state = MethodType(ExportPresenter._canvas_view_state, fake)
    fake._resolve_tab_contract = MethodType(
        ExportPresenter._resolve_tab_contract, fake
    )

    fake._start_save_after_dialog = MethodType(
        ExportPresenter._start_save_after_dialog, fake
    )
    return fake, SimpleNamespace(
        opened=opened, saved=saved, persisted=persisted,
        validated=validated, messages=messages,
    )


def _options() -> dict:
    return {
        "output_dir": "/tmp/out",
        "file_name": "shot",
        "format": "PNG",
        "quality": 95,
        "width": 32,
        "height": 24,
        "background_color": (0, 0, 0, 255),
        "fill_background": True,
    }


def test_save_result_hands_the_dialog_a_finished_callback_and_returns():
    """``save_result`` must not block: nothing is exported until the callback."""
    fake, seen = _fake_presenter()

    ExportPresenter.save_result(fake)

    assert len(seen.opened) == 1
    on_finished = seen.opened[0]["kwargs"].get("on_finished")
    assert callable(on_finished), (
        "the dialog service must be given on_finished; without it the export "
        "never starts (the dialog no longer blocks until accepted)"
    )
    assert seen.saved == []
    assert seen.persisted == []
    assert seen.messages == []


def test_accepted_continues_with_the_dialog_options():
    fake, seen = _fake_presenter()
    opts = _options()

    ExportPresenter.save_result(fake)
    on_finished = seen.opened[0]["kwargs"]["on_finished"]
    on_finished(QDialog.DialogCode.Accepted, opts)

    assert seen.validated == [opts]
    assert len(seen.saved) == 1
    save_ctx, export_opts = seen.saved[0]
    assert export_opts is opts, "the accepted options must be the ones used"
    assert save_ctx.suggested_filename == "out"
    assert seen.persisted == [opts]
    assert seen.messages == []


def test_cancel_writes_nothing():
    """Cancel = no validation, no composition, no save worker, no toast."""
    fake, seen = _fake_presenter()

    ExportPresenter.save_result(fake)
    on_finished = seen.opened[0]["kwargs"]["on_finished"]
    on_finished(QDialog.DialogCode.Rejected, {})

    assert seen.validated == []
    assert seen.saved == []
    assert seen.persisted == []
    assert seen.messages == []


def test_missing_images_never_opens_the_dialog():
    fake, seen = _fake_presenter(has_images=False)

    ExportPresenter.save_result(fake)

    assert seen.opened == []
    assert seen.messages == [("missing",)]
