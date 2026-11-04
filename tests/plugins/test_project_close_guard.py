"""A close request must not tear the app down under a project worker.

The observation this pins (2026-10-28, mechanism confirmed 2026-11-04): a
project save runs on the app pool, and the shutdown sequence is
``thread_pool.clear()`` + a capped ``waitForDone`` (``core/bootstrap.py``).
``QRunnable`` pool ``clear()`` removes QUEUED runnables only — it does not stop
a running one — and a project with embedded media outlives the cap, so teardown
used to proceed while ``write_project_zip`` was still creating temp files. The
crash landed in the packaging worker (``tempfile._mkstemp_inner``).

The policy (owner decision 2026-11-04): refuse the close and say why, instead of
freezing the window or losing the write.
"""

from __future__ import annotations

from types import SimpleNamespace

from ui.main_window.project.busy import defer_close_for_project_worker


class _Event:
    def __init__(self) -> None:
        self.ignored = False
        self.accepted = False

    def ignore(self) -> None:
        self.ignored = True

    def accept(self) -> None:
        self.accepted = True


class _Toast:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def show_toast(self, message, duration=0, progress=0):
        self.calls.append((message, int(duration)))
        return 1


def _owner(*, in_flight: bool, kind: str = "save", toast=None, translate=None):
    """The real seam: the manager is read off ``owner._window.toast_manager``."""
    return SimpleNamespace(
        project_worker_in_flight=lambda: in_flight,
        project_worker_kind=kind,
        _window=SimpleNamespace(toast_manager=toast),
        _tr=translate or (lambda key, fallback: fallback),
    )


def test_a_running_save_refuses_the_close_and_says_so():
    toast = _Toast()
    event = _Event()

    refused = defer_close_for_project_worker(_owner(in_flight=True, toast=toast), event)

    assert refused is True
    assert event.ignored is True
    assert event.accepted is False
    assert toast.calls and toast.calls[0][0] == "Saving project…"
    assert toast.calls[0][1] > 0, "the refusal toast must clear itself"


def test_a_running_open_names_the_open_not_the_save():
    toast = _Toast()
    event = _Event()

    assert defer_close_for_project_worker(
        _owner(in_flight=True, kind="open", toast=toast), event
    )
    assert toast.calls[0][0] == "Opening project…"


def test_an_idle_project_io_lets_the_close_through():
    toast = _Toast()
    event = _Event()

    refused = defer_close_for_project_worker(_owner(in_flight=False, toast=toast), event)

    assert refused is False
    assert event.ignored is False
    assert toast.calls == []


def test_a_missing_toast_manager_still_refuses_the_close():
    """The guard is the close policy; the toast is only how it explains itself."""
    event = _Event()

    assert defer_close_for_project_worker(_owner(in_flight=True, toast=None), event)
    assert event.ignored is True


def test_a_missing_project_io_never_blocks_the_close():
    """A window without the project mixin (tests, a stripped shell) still closes."""
    event = _Event()

    assert defer_close_for_project_worker(None, event) is False
    assert event.ignored is False


def test_the_busy_flag_is_set_and_cleared_by_the_worker_contract():
    """The flag the guard reads is the one save/open maintain."""
    from ui.main_window.project_io import MainWindowProjectIo

    io = MainWindowProjectIo(SimpleNamespace(), translate=lambda key, fallback: fallback)
    assert io.project_worker_in_flight() is False

    io._mark_project_worker_busy(True)
    assert io.project_worker_in_flight() is True
    assert io.project_worker_kind == "save"

    io._mark_project_worker_busy(True, kind="open")
    assert io.project_worker_kind == "open"

    io._mark_project_worker_busy(False)
    assert io.project_worker_in_flight() is False
