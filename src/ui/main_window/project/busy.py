"""Busy-cursor and toast progress plumbing for project open/save."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from core.log_names import APP_LOGGER_NAME

if TYPE_CHECKING:
    from ui.main_window.project_io import MainWindowProjectIo

logger = logging.getLogger(APP_LOGGER_NAME)


def _project_toast_manager(owner: "MainWindowProjectIo"):
    return getattr(owner._window, "toast_manager", None)


def project_worker_in_flight(owner: "MainWindowProjectIo") -> bool:
    """Whether a project open/save worker still has to finish."""
    predicate = getattr(owner, "project_worker_in_flight", None)
    if not callable(predicate):
        return False
    try:
        return bool(predicate())
    except Exception:
        logger.debug("project worker probe failed", exc_info=True)
        return False


def defer_close_for_project_worker(owner: "MainWindowProjectIo | None", event) -> bool:
    """Refuse a window close while a project worker runs; say why.

    Returns ``True`` when the close was refused (``event.ignore()`` already
    called). A pool ``clear()`` removes QUEUED runnables only and the shutdown
    wait is capped (``core/bootstrap.py``), so accepting the close used to let
    teardown proceed while ``write_project_zip`` was still creating temp files —
    the "Fatal Python error: Segmentation fault" in the packaging worker
    (observed 2026-10-28, mechanism confirmed 2026-11-04). The user closes
    again once the toast clears.
    """
    if owner is None or not project_worker_in_flight(owner):
        return False
    kind = getattr(owner, "project_worker_kind", "save")
    if kind == "open":
        key, fallback = "menu.project_opening", "Opening project…"
    else:
        key, fallback = "menu.project_saving", "Saving project…"
    translate = getattr(owner, "_tr", None)
    message = translate(key, fallback) if callable(translate) else fallback
    logger.info("Close deferred: a project worker is still running (%s)", kind)
    try:
        event.ignore()
    except Exception:
        logger.debug("close event ignore failed", exc_info=True)
    try:
        manager = _project_toast_manager(owner)
        if manager is not None:
            manager.show_toast(message, duration=3000, progress=0)
    except Exception:
        logger.debug("close-deferred toast failed", exc_info=True)
    return True


def _project_thread_pool(owner: "MainWindowProjectIo"):
    pool = getattr(owner._window, "thread_pool", None)
    if pool is not None:
        return pool
    presenter = owner._presenter()
    controller = getattr(presenter, "main_controller", None) if presenter else None
    return getattr(controller, "thread_pool", None) if controller else None


def _show_project_error(owner: "MainWindowProjectIo", title: str, text: str) -> None:
    from shared_toolkit.ui.message_dialog import AppMessageDialog

    AppMessageDialog.warning(owner._window, title=title, text=text)


def _notify_project_saved(owner: "MainWindowProjectIo", path: str) -> None:
    """Tray state + system notification after a project is written.

    The save flow used to end in the in-app toast only - and in nothing at all
    when the toast manager had not resolved (owner report 2026-10-28: "кнопка
    сохранить проект не шлёт тост в аппе и в систему"). A project save is the
    same promise as an image export, which already notifies
    (``image_export/save_flow.py::_on_success_notify``).
    """
    import os

    window = owner._window
    actions = getattr(window, "action_registry", None)
    if actions is None:
        return
    try:
        actions.set_last_saved_path(path)
        actions.update_tray_actions_visibility()
    except Exception:
        logger.exception("Project save: tray refresh failed")
    settings = getattr(getattr(window, "store", None), "settings", None)
    if not bool(getattr(settings, "system_notifications_enabled", True)):
        return
    try:
        actions.notify_system(
            owner._tr("msg.saved", "Saved"),
            f"{owner._tr('msg.saved', 'Saved')}: {path}",
            image_path=path if os.path.isfile(path) else None,
            timeout_ms=4000,
        )
    except Exception:
        logger.exception("Project save: system notification failed")


def _begin_project_busy(owner: "MainWindowProjectIo", message: str):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    toast = owner._project_toast_manager()
    toast_id = None
    if toast is None:
        # Silent before: the whole progress surface vanished with the manager.
        logger.warning(
            "Project busy: no toast manager on the window - progress stays invisible"
        )
    else:
        try:
            toast_id = toast.show_toast(message, duration=0, progress=0)
        except Exception:
            logger.exception("Failed to show project progress toast")
            toast_id = None
    return toast, toast_id


def _end_project_busy(
    owner: "MainWindowProjectIo", toast, toast_id, message: str, *, ok: bool
) -> None:
    from PySide6.QtWidgets import QApplication

    QApplication.restoreOverrideCursor()
    if toast is None or toast_id is None:
        return
    try:
        if ok:
            toast.update_toast(toast_id, message, success=True, duration=1800, progress=100)
        else:
            toast.close_toast(toast_id)
    except Exception:
        logger.exception("Failed to finish project progress toast")


def _on_project_toast_progress(
    owner: "MainWindowProjectIo", toast, toast_id, message: str, value
) -> None:
    if toast is None or toast_id is None:
        return
    try:
        progress = int(value) if value is not None else 0
        toast.update_toast(
            toast_id,
            message,
            success=False,
            duration=0,
            progress=max(0, min(100, progress)),
        )
    except Exception:
        pass
