"""Save-project flow: snapshot, async package worker, missing-media handling."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from core.log_names import APP_LOGGER_NAME

if TYPE_CHECKING:
    from ui.main_window.project_io import MainWindowProjectIo

logger = logging.getLogger(APP_LOGGER_NAME)


def save_project(owner: "MainWindowProjectIo") -> None:
    path = owner._current_save_path()
    if path is None:
        owner.save_project_as()
        return
    path = owner.reconcile_save_path_with_session_title(path)
    owner.write_project(path)


def save_project_as(owner: "MainWindowProjectIo") -> None:
    path = owner._choose_project_save_path()
    if not path:
        return
    owner.write_project(path)


def write_project(owner: "MainWindowProjectIo", path: str) -> None:
    window = owner._window
    from services.io.project_io import build_project_data
    from services.io.project_package import iter_session_media_paths
    from tabs.registry import TabRegistry

    # Snapshot session state on the UI thread (viewport / widgets).
    try:
        project_data = build_project_data(window.store, TabRegistry())
    except Exception as exc:
        logger.exception("Project snapshot failed")
        owner._show_project_error(
            owner._tr("menu.save_project", "Save Project"),
            owner._tr(
                "menu.project_save_failed",
                "Could not save the project file.",
            )
            + f"\n{exc}",
        )
        return

    preview_png = None
    try:
        from services.io.project_preview import capture_project_preview_png

        preview_png = capture_project_preview_png(window)
    except Exception:
        logger.debug("Project preview capture skipped", exc_info=True)

    pixel_cache_sources = None
    if owner.include_pixel_cache:
        try:
            from services.io.project_io import collect_pixel_cache_sources

            pixel_cache_sources = collect_pixel_cache_sources(window.store, TabRegistry())
        except Exception:
            logger.exception("Failed to collect pixel cache sources")
            pixel_cache_sources = None

    media_count = len(set(iter_session_media_paths(project_data)))
    logger.info("Saving project to %s (%d unique media path(s))", path, media_count)

    saving = owner._tr("menu.project_saving", "Saving project…")
    toast, toast_id = owner._begin_project_busy(saving)
    pool = owner._project_thread_pool()

    def _on_done(missing) -> None:
        owner._mark_project_worker_busy(False)
        owner.remember_project_path(path)
        owner._end_project_busy(toast, toast_id, saving, ok=True)
        owner._notify_project_saved(path)
        if missing:
            owner._show_project_error(
                owner._tr("menu.save_project", "Save Project"),
                owner._tr(
                    "menu.project_missing_media",
                    "Project saved, but some image files were missing and were not embedded.",
                )
                + "\n"
                + "\n".join(list(missing)[:12]),
            )

    def _on_error(err_tuple) -> None:
        owner._mark_project_worker_busy(False)
        owner._end_project_busy(toast, toast_id, saving, ok=False)
        exc = (
            err_tuple[1]
            if isinstance(err_tuple, tuple) and len(err_tuple) > 1
            else err_tuple
        )
        logger.exception("Save project failed: %s", exc)
        owner._show_project_error(
            owner._tr("menu.save_project", "Save Project"),
            owner._tr(
                "menu.project_save_failed",
                "Could not save the project file.",
            )
            + f"\n{exc}",
        )

    def _worker_task(**kwargs):
        from services.io.project_io import package_project_data

        progress_callback = kwargs.get("progress_callback")

        def _progress(done: int, total: int, _label: str) -> None:
            if progress_callback is None or total <= 0:
                return
            progress_callback.emit(int(100 * done / max(total, 1)))

        return package_project_data(
            project_data,
            path,
            progress=_progress,
            preview_png=preview_png,
            pixel_cache_sources=pixel_cache_sources,
        )

    if pool is None:
        try:
            from services.io.project_io import package_project_data

            _on_done(
                package_project_data(
                    project_data,
                    path,
                    preview_png=preview_png,
                    pixel_cache_sources=pixel_cache_sources,
                )
            )
        except Exception as exc:
            _on_error((type(exc), exc, None))
        return

    from sli_ui_toolkit.workers import GenericWorker

    worker = GenericWorker(_worker_task)
    worker.kwargs["progress_callback"] = worker.signals.progress
    owner._project_worker = worker
    # The close path must not tear the app down under a running worker (a pool
    # clear() does not stop a running runnable); the flag clears on both
    # outcomes below, so a failed save cannot leave the window unclosable.
    owner._mark_project_worker_busy(True)
    worker.signals.progress.connect(
        lambda value: owner._on_project_toast_progress(toast, toast_id, saving, value)
    )
    worker.signals.result.connect(_on_done)
    worker.signals.error.connect(_on_error)
    pool.start(worker)
