"""Closing the video editor must not walk into deleted C++ children.

Owner traceback (2026-10-28)::

    Error calling Python override of QDialog::closeEvent()
    shell.closeEvent -> presenter.cleanup -> SettleGate.cancel
      -> self._timer.stop()
    RuntimeError: libshiboken: Internal C++ object (PySide6.QtCore.QTimer)
    already deleted

Mechanism: the coordinators own ``SettleGate``s parented to the presenter
(``timer_parent=self`` in ``VideoEditorPresenter``), and a gate owns its
``QTimer`` as a QObject child. ``presenter.cleanup()`` and the
``view.destroyed`` handler (``_on_view_destroyed``) both end with
``deleteLater()`` on that presenter, so once Qt processes the deletion the
presenter's children — the gates and their timers — are gone while the Python
wrappers still answer attribute lookups. A later teardown re-entry
(``closeEvent`` after the presenter already tore itself down) then calls into
dead C++ objects.

The contract pinned here is on the surviving close path — the preview
coordinator's ``SettleGate`` and its pool are still presenter children, so the
same re-entry is still unsafe:

* ``VideoEditorDialog.closeEvent`` closes cleanly when the presenter's C++
  object is already gone, instead of re-entering its dead coordinator tree;
* a normal close still tears the presenter down.

The dead state is produced without waiting on a real event loop by deleting
the presenter's C++ object directly (``shiboken6.delete``) — the same state
``deleteLater()`` reaches, and the one the owner's traceback was taken in.

(Until strip atlas phase 3 this file also pinned the thumbnail coordinator's
own gate surviving a deleted gate/timer; that coordinator and its gate are
gone — the strip is one GPU band pass with no settle gate at all.)
"""

from __future__ import annotations

import shiboken6
import pytest
from PySide6.QtWidgets import QApplication

from sli_ui_toolkit.managers import UiScale
from tabs.image_compare.plugins.video_editor.dialog.shell import VideoEditorDialog


class _FakePresenter:
    main_controller = None


class _FakeExportController:
    store = None
    presenter = _FakePresenter()


@pytest.fixture(autouse=True)
def _scale_one(qapp):
    UiScale.get_instance().set_factor(1.0)
    yield
    UiScale.get_instance().set_factor(1.0)


def _open_editor(qapp) -> VideoEditorDialog:
    dialog = VideoEditorDialog([], _FakeExportController(), None)
    dialog.show()
    for _ in range(4):
        qapp.processEvents()
    return dialog


def _destroy(dialog) -> None:
    try:
        if shiboken6.isValid(dialog) and dialog.isVisible():
            dialog.close()
    except RuntimeError:
        pass
    QApplication.processEvents()


def _kill_presenter(dialog) -> None:
    """Reach the field state: presenter (and its child timers) C++-deleted."""
    presenter = dialog.presenter
    shiboken6.delete(presenter)
    # Sanity: this is the state the crash needs — not a mock of it.
    assert not shiboken6.isValid(presenter)


def test_close_event_survives_a_presenter_that_already_tore_down(qapp):
    """The owner's exact frame: ``closeEvent`` re-enters a dead presenter.

    The override is invoked directly instead of through ``close()``: PySide
    swallows exceptions raised inside a virtual override (it prints
    "Error calling Python override ..." and carries on), which is how the
    traceback reached the owner as a console error rather than a crash.
    """
    from PySide6.QtGui import QCloseEvent

    dialog = _open_editor(qapp)
    try:
        _kill_presenter(dialog)

        dialog.closeEvent(QCloseEvent())  # pre-fix: RuntimeError from cancel()
    finally:
        _destroy(dialog)


def test_close_after_a_dead_presenter_still_closes_the_window(qapp):
    from PySide6.QtGui import QCloseEvent

    dialog = _open_editor(qapp)
    try:
        _kill_presenter(dialog)

        dialog.closeEvent(QCloseEvent())
        dialog.close()
        for _ in range(3):
            qapp.processEvents()
        assert not shiboken6.isValid(dialog)
    finally:
        _destroy(dialog)


def test_normal_close_still_tears_the_presenter_down(qapp):
    dialog = _open_editor(qapp)
    presenter = dialog.presenter
    try:
        dialog.close()
        for _ in range(3):
            qapp.processEvents()

        # The presenter dies with the dialog: nothing survives the close.
        assert not shiboken6.isValid(presenter)
    finally:
        _destroy(dialog)
