"""Open-project flow: async load worker, missing-file and error handling."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from core.log_names import APP_LOGGER_NAME

if TYPE_CHECKING:
    from ui.main_window.project_io import MainWindowProjectIo

logger = logging.getLogger(APP_LOGGER_NAME)


def open_project(owner: "MainWindowProjectIo") -> None:
    path = owner._choose_project_open_path()
    if not path:
        return
    owner.open_project_at_path(path)


def open_project_at_path(owner: "MainWindowProjectIo", path: str) -> None:
    """Load a project file (File → Open and Session Picker recent)."""
    if not path:
        return
    from pathlib import Path

    if not Path(path).is_file():
        owner._handle_missing_project_file(path)
        return

    window = owner._window
    presenter = owner._presenter()
    controller = getattr(presenter, "main_controller", None) if presenter else None
    if controller is None:
        return

    opening = owner._tr("menu.project_opening", "Opening project…")
    toast, toast_id = owner._begin_project_busy(opening)
    pool = owner._project_thread_pool()

    def _apply_loaded(payload) -> None:
        owner._mark_project_worker_busy(False)
        from services.io.project_io import load_project_data
        from tabs.registry import TabRegistry

        data, warnings = payload
        try:
            load_project_data(
                data,
                controller.workspace,
                window.store,
                TabRegistry(),
                replace_workspace=True,
            )
            owner.remember_project_path(path)
            owner._end_project_busy(toast, toast_id, opening, ok=True)
            if warnings:
                owner._show_project_error(
                    owner._tr("menu.open_project", "Open Project"),
                    "\n".join(warnings),
                )
        except Exception as exc:
            logger.exception("Open project apply failed")
            owner._end_project_busy(toast, toast_id, opening, ok=False)
            owner._show_project_error(
                owner._tr("menu.open_project", "Open Project"),
                owner._tr(
                    "menu.project_open_failed",
                    "Could not open the project file.",
                )
                + f"\n{exc}",
            )

    def _on_error(err_tuple) -> None:
        owner._mark_project_worker_busy(False)
        owner._end_project_busy(toast, toast_id, opening, ok=False)
        exc = (
            err_tuple[1]
            if isinstance(err_tuple, tuple) and len(err_tuple) > 1
            else err_tuple
        )
        if owner._is_missing_project_error(exc):
            owner._handle_missing_project_file(path)
            return
        logger.error("Open project failed: %s", exc, exc_info=err_tuple)
        owner._show_project_error(
            owner._tr("menu.open_project", "Open Project"),
            owner._tr(
                "menu.project_open_failed",
                "Could not open the project file.",
            )
            + f"\n{exc}",
        )

    def _worker_task(**kwargs):
        from services.io.project_io import prepare_project_file_for_load

        progress_callback = kwargs.get("progress_callback")

        def _progress(done: int, total: int, _label: str) -> None:
            if progress_callback is None or total <= 0:
                return
            progress_callback.emit(int(100 * done / max(total, 1)))

        return prepare_project_file_for_load(path, progress=_progress)

    if pool is None:
        try:
            from services.io.project_io import prepare_project_file_for_load

            _apply_loaded(prepare_project_file_for_load(path))
        except Exception as exc:
            _on_error((type(exc), exc, None))
        return

    from sli_ui_toolkit.workers import GenericWorker

    worker = GenericWorker(_worker_task)
    worker.kwargs["progress_callback"] = worker.signals.progress
    owner._project_worker = worker
    # Same close guard as the save path: an open worker reads and unpacks while
    # the app would otherwise be tearing its pool down under it.
    owner._mark_project_worker_busy(True, kind="open")
    worker.signals.progress.connect(
        lambda value: owner._on_project_toast_progress(toast, toast_id, opening, value)
    )
    worker.signals.result.connect(_apply_loaded)
    worker.signals.error.connect(_on_error)
    pool.start(worker)


def is_missing_project_error(exc: object) -> bool:
    import errno

    if isinstance(exc, FileNotFoundError):
        return True
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == errno.ENOENT:
        return True
    return False


def _handle_missing_project_file(owner: "MainWindowProjectIo", path: str) -> None:
    """Refresh Recent to the missing-card state; avoid a raw exception dialog."""
    from pathlib import Path

    logger.warning("Project file missing: %s", path)
    owner.refresh_session_picker_recent()
    name = Path(path).name or path
    message = owner._tr(
        "menu.project_file_missing",
        "The project file is missing. The Recent card was updated.",
    )
    toast = owner._project_toast_manager()
    if toast is not None:
        try:
            toast.show_toast(f"{message}\n{name}", duration=3200)
            return
        except Exception:
            logger.exception("Failed to show missing-project toast")
    owner._show_project_error(
        owner._tr("menu.open_project", "Open Project"),
        f"{message}\n{name}",
    )
