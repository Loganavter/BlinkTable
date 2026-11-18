"""Session init — extracted from _session_controller.py (Phase 4)."""

from __future__ import annotations


def init_session_state(controller, store, thread_pool):
    from tabs._shared.loading_toast import LoadingToastCoordinator
    from engine.pyramid.coordinator import PyramidBuildCoordinator
    from tabs.image_compare.pipeline.session import ImageSession

    def _ic_get_toast_manager():
        try:
            return getattr(getattr(controller.presenter, "main_window_app", None), "toast_manager", None)
        except Exception:
            return None

    def _ic_translate(key: str, default: str | None = None):
        from sli_ui_toolkit.i18n import get_current_language, tr as _tr
        try:
            return _tr(key, get_current_language())
        except Exception:
            return default if default is not None else key

    controller._loading_toast_coordinator = LoadingToastCoordinator(get_toast_manager=_ic_get_toast_manager, translate=_ic_translate)
    controller._loading_toasts = controller._loading_toast_coordinator._loading_toasts  # type: ignore
    controller._pyramid_coordinator = PyramidBuildCoordinator(
        get_thread_pool=lambda: controller.thread_pool,
        toast_coordinator=controller._loading_toast_coordinator,
        request_view_update=controller._schedule_image_canvas_update,
        invalidate_render=lambda complete: controller._invalidate_image_canvas_render_state() if complete else None,
        get_store=lambda: controller.store,
    )
    controller._pyramid_builds = controller._pyramid_coordinator._pyramid_builds
    controller._loading_toast_uid_slot = controller._pyramid_coordinator._pyramid_toast_slot
    controller._image_sessions: dict[str, ImageSession] = {}
    def _resolve_active_session_id() -> str:
        try:
            sess = controller.store.get_active_workspace_session()
            return getattr(sess, "id", "default") or "default"
        except Exception:
            return "default"
    controller._resolve_active_session_id = _resolve_active_session_id  # type: ignore
    _default_id = _resolve_active_session_id()
    _default_session = ImageSession(session_id=_default_id)
    # Дефолт сессии должен отражать настройку сразу: иначе fallback
    # `else self.crop_service` в PipelineCache воскресит кроп при OFF.
    try:
        _should = getattr(getattr(store, "settings", None), "auto_crop_black_borders", False)
        _default_session.sync_crop_service(bool(_should))
    except Exception:
        pass
    controller._image_sessions[_default_id] = _default_session
    controller._pipeline_cache = _default_session.cache
    controller.pipeline = _default_session.pipeline
    controller._crop_service = _default_session.crop_service
    controller._image_session = _default_session  # type: ignore
    # keep pending as live views for compat (via proxies below)
    controller.store.on_change(controller._on_store_scoped_change)
