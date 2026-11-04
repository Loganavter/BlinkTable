"""Host project open/save flow for the main window.

Owns path sticky defaults, async load/save workers, recent-list wiring, and
toast progress. Title-bar menus and Find Action runners stay in
``menu_controller`` and call into this class.

Implementation is split by concern under ``ui/main_window/project/`` — this
module is a thin owner (construction/wiring, instance state, and delegator
methods) per the "thin owner + use_cases/ module" pattern in
``docs/dev/CODE_PATTERNS.md``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from ui.main_window.project import busy, dialogs, open as open_, paths, recent, save
from core.log_names import APP_LOGGER_NAME

if TYPE_CHECKING:
    from ui.main_window.window import MainWindow

logger = logging.getLogger(APP_LOGGER_NAME)

# Re-exported: external callers (use_cases/platform_actions.py) import this
# straight from this module's historical location.
resolve_session_picker_host_chrome = recent.resolve_session_picker_host_chrome


class MainWindowProjectIo:
    """File → Open / Save / Save As and Session Picker recent open."""

    def __init__(
        self,
        window: MainWindow,
        *,
        translate: Callable[[str, str], str],
    ) -> None:
        self._window = window
        self._tr = translate
        self.current_project_path: str | None = None
        self._project_worker = None
        #: True while a project open/save worker is on the pool. The close path
        #: refuses to tear the app down under it (see ``window.closeEvent``):
        #: a pool ``clear()`` does not stop a RUNNING runnable and the shutdown
        #: wait is capped, so teardown used to proceed while
        #: ``write_project_zip`` was still creating temp files — the
        #: "Fatal Python error: Segmentation fault" in the packaging worker
        #: (2026-10-28, mechanism confirmed 2026-11-04).
        self._project_worker_busy = False
        #: "save" | "open" — which operation the busy flag refers to.
        self._project_worker_kind = "save"
        # Landing spot for a future "save with pixel cache" UI checkbox.
        self.include_pixel_cache: bool = False

    def project_worker_in_flight(self) -> bool:
        """Whether a project open/save worker still has to finish."""
        return bool(self._project_worker_busy)

    def _mark_project_worker_busy(self, busy: bool, kind: str = "save") -> None:
        self._project_worker_busy = bool(busy)
        if busy:
            self._project_worker_kind = str(kind)

    @property
    def project_worker_kind(self) -> str:
        """Which operation the in-flight worker belongs to ("save" | "open")."""
        return str(getattr(self, "_project_worker_kind", "save"))

    def _presenter(self):
        return getattr(self._window, "presenter", None)

    def project_settings(self):
        return paths.project_settings(self)

    def _default_documents_dir(self) -> str:
        return paths._default_documents_dir(self)

    def _is_downloads_dir(self, path: str) -> bool:
        return paths._is_downloads_dir(self, path)

    def project_start_path(self, *, for_save: bool) -> str:
        return paths.project_start_path(self, for_save=for_save)

    def _custom_session_save_stem(self) -> str | None:
        return paths._custom_session_save_stem(self)

    def _project_path_for_stem(self, directory, stem: str, *, current=None):
        return paths._project_path_for_stem(self, directory, stem, current=current)

    def reconcile_save_path_with_session_title(self, path: str) -> str:
        return paths.reconcile_save_path_with_session_title(self, path)

    def _current_save_path(self) -> str | None:
        return paths._current_save_path(self)

    def remember_project_path(self, path: str) -> None:
        recent.remember_project_path(self, path)

    def _apply_project_name_to_active_session(self, path: str) -> None:
        recent._apply_project_name_to_active_session(self, path)

    def refresh_session_picker_recent(self) -> None:
        recent.refresh_session_picker_recent(self)

    def wire_session_picker_recent(self) -> None:
        """Attach open-project handler to the Session Picker recent panel."""
        recent.wire_session_picker_recent(self)

    def _choose_project_open_path(self) -> str | None:
        return dialogs._choose_project_open_path(self)

    def _choose_project_save_path(self) -> str | None:
        return dialogs._choose_project_save_path(self)

    def _project_toast_manager(self):
        return busy._project_toast_manager(self)

    def _project_thread_pool(self):
        return busy._project_thread_pool(self)

    def _show_project_error(self, title: str, text: str) -> None:
        busy._show_project_error(self, title, text)

    def _begin_project_busy(self, message: str):
        return busy._begin_project_busy(self, message)

    def _end_project_busy(self, toast, toast_id, message: str, *, ok: bool) -> None:
        busy._end_project_busy(self, toast, toast_id, message, ok=ok)

    def _notify_project_saved(self, path: str) -> None:
        busy._notify_project_saved(self, path)

    def _on_project_toast_progress(self, toast, toast_id, message: str, value) -> None:
        busy._on_project_toast_progress(self, toast, toast_id, message, value)

    def open_project(self) -> None:
        open_.open_project(self)

    def open_project_at_path(self, path: str) -> None:
        """Load a project file (File → Open and Session Picker recent)."""
        open_.open_project_at_path(self, path)

    @staticmethod
    def _is_missing_project_error(exc: object) -> bool:
        return open_.is_missing_project_error(exc)

    def _handle_missing_project_file(self, path: str) -> None:
        open_._handle_missing_project_file(self, path)

    def save_project(self) -> None:
        save.save_project(self)

    def save_project_as(self) -> None:
        save.save_project_as(self)

    def write_project(self, path: str) -> None:
        save.write_project(self, path)
