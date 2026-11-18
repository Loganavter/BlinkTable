# Audit-Meta: pattern=thin-owner reason="reference thin owner per CODE_PATTERNS — delegates to use_cases/loading,list_ops,navigation"
import logging

from typing import Any

from PySide6.QtCore import QObject, Signal

from tabs.image_compare.events import (
    AnalysisSetChannelViewModeEvent,
    AnalysisSetDiffModeEvent,
)
from tabs.image_compare.services.analysis.cached_diff import CachedDiffService
from tabs.image_compare.use_cases import list_ops, loading, navigation
from tabs.image_compare.use_cases import canvas_invalidate, image_decode
from core.log_names import APP_LOGGER_NAME

logger = logging.getLogger(APP_LOGGER_NAME)


from tabs.image_compare.use_cases.session_api import SessionApiMixin

class SessionController(SessionApiMixin, QObject):

    error_occurred = Signal(str)
    image_loaded = Signal()
    update_requested = Signal()

    def __init__(
        self,
        store,
        thread_pool,
        playlist_manager,
        metrics_service=None,
        diff_service: CachedDiffService | None = None,
        presenter=None,
        event_bus=None,
    ):
        super().__init__()
        self.store = store
        self.thread_pool = thread_pool
        self.playlist_manager = playlist_manager
        self.metrics_service = metrics_service
        self.diff_service = diff_service
        self.presenter = presenter
        self.event_bus = event_bus

        from tabs.image_compare.use_cases.session_init import init_session_state
        init_session_state(self, self.store, self.thread_pool)
        # Last resynced (index, path) per slot — owned by
        # use_cases/session_bootstrap.resync_current_image_slots, which skips
        # slots whose signature did not change so a DnD load into one slot
        # cannot wipe the other slot's live image via the document-emit
        # resync. Initialized here (thin owner); bootstrap lazily repairs it
        # for foreign-constructed controllers (tests).
        self._last_resync_slot_sig: dict[int, tuple] = {}
        # Последний виденный флаг автокропа — для синхронизации сессионных
        # crop-дефолтов при тоггле (см. _on_store_scoped_change).
        self._last_crop_enabled: bool | None = None

    def _get_image_session(self, session_id: str | None = None):
        """Return ImageSession for session_id (or active). Creates on demand."""
        try:
            sid = session_id or self._resolve_active_session_id()
        except Exception:
            sid = session_id or "default"
        sess = self._image_sessions.get(sid)
        if sess is None:
            from tabs.image_compare.pipeline.session import ImageSession

            sess = ImageSession(session_id=sid)
            # Как в session_init: дефолт новой сессии — по текущей настройке.
            try:
                _should = getattr(
                    getattr(self.store, "settings", None),
                    "auto_crop_black_borders",
                    True,
                )
                sess.sync_crop_service(bool(_should))
            except Exception:
                pass
            self._image_sessions[sid] = sess
        # keep live session pointed at active session (thin owner)
        try:
            self._image_session = sess
            self._pipeline_cache = sess.cache
            self.pipeline = sess.pipeline
            try:
                if hasattr(self.pipeline, "set_store"):
                    self.pipeline.set_store(self.store)
            except Exception:
                pass
            self._crop_service = sess.crop_service
        except Exception:
            pass
        return sess

    def _get_crop_service(self):
        """Вернуть CropService если autocrop включён, иначе None."""
        should_crop = getattr(self.store.settings, "auto_crop_black_borders", True)
        if not should_crop:
            return None
        # Актуальный сервис из активной сессии
        try:
            sess = self._get_image_session()
            return getattr(sess, "crop_service", None) or getattr(self, "_crop_service", None)
        except Exception:
            return getattr(self, "_crop_service", None)

    def _get_crop_service_forced(self):
        """Detection-only spare for an explicit per-image On (True).

        The OFF gate (``_get_crop_service`` → None, nulled session/cache
        services) stays the default path untouched; this spare is never
        wired into the decode pipeline and is consulted only when the
        per-image override is True, so Auto/Off behavior is bit-identical
        with the flag on or off. Never raises.
        """
        try:
            svc = getattr(self, "_forced_crop_service", None)
            if svc is None:
                from shared.image_processing.autocrop.service import CropService

                svc = CropService()
                self._forced_crop_service = svc
            return svc
        except Exception:
            return None

    def _on_store_scoped_change(self, scope: str) -> None:
        # Тоггл auto_crop_black_borders идёт через диспетчер без скоупа
        # (дефолт viewport): синхронизируем сессионные crop-дефолты, иначе
        # fallback `else self.crop_service` в PipelineCache воскресит кроп
        # при OFF. Дешёвое сравнение — срабатывает только на смене флага.
        try:
            _cur_crop = bool(
                getattr(
                    getattr(self.store, "settings", None),
                    "auto_crop_black_borders",
                    True,
                )
            )
            if _cur_crop != self._last_crop_enabled:
                self._last_crop_enabled = _cur_crop
                from tabs.image_compare.pipeline.session import (
                    set_sessions_crop_enabled,
                )

                set_sessions_crop_enabled(_cur_crop)
        except Exception:
            pass
        if scope != "document":
            return
        if getattr(self, "_resyncing", False):
            return
        self._resyncing = True  # type: ignore[attr-defined]
        try:
            # Dispatcher reentrant-safe (``Dispatcher``: copy subscribers
            # outside _lock, emit outside lock) — resync may dispatch
            # synchronously without QTimer deferral.
            self._resync_current_image_slots_if_needed()
        except RuntimeError:
            # Reentrant-safe: synchronous fallback without QTimer (was
            # QTimer.singleShot(0, ...) before Phase 2B).
            self._resync_current_image_slots_if_needed()
        finally:
            self._resyncing = False  # type: ignore[attr-defined]

    def _resync_current_image_slots_if_needed(self) -> None:
        from tabs.image_compare.use_cases.loading import resync_current_image_slots

        resync_current_image_slots(self)

    def _update_image_slot(self, slot_number: int, *, image=None, path=None, emit=True, is_preview=False, is_full_res=False):
        from tabs.image_compare.use_cases.document_slot import update_image_slot
        return update_image_slot(self, slot_number, image=image, path=path, emit=emit, is_preview=is_preview, is_full_res=is_full_res)

    def initialize_app_display(self):
        loading.initialize_app_display(self)

    def _load_image_async(self, path, image_number, index_in_list, target_size=None):
        return image_decode.load_image_async(self, path, image_number, index_in_list, target_size)

    def _cancel_pending_unification(self, new_path1: str = "", new_path2: str = "", force: bool = False) -> bool:
        return image_decode.cancel_pending_unification(self, new_path1, new_path2, force)

    def _on_image_loaded(self, result):
        return image_decode.on_image_loaded(self, result)

    def _on_image_loaded_from_worker(self, result):
        self._on_image_loaded(result)

    def _trigger_preview_unification(self, image_number: int):
        loading.trigger_preview_unification(self, image_number)

    def _load_full_resolution_async(self, path, image_number, index_in_list):
        return image_decode.load_full_resolution_async(self, path, image_number, index_in_list)

    def _on_full_resolution_loaded_result(self, result) -> None:
        return image_decode.on_full_resolution_loaded_result(self, result)

    def _on_full_resolution_error(self, path: str, err) -> None:
        return image_decode.on_full_resolution_error(self, path, err)

    def _on_full_image_loaded(self, full_img, path, image_number, index_in_list):
        loading.handle_full_image_loaded(
            self, full_img, path, image_number, index_in_list
        )

    def load_images_from_paths(self, file_paths: list[str], image_number: int):
        loading.load_images_from_paths(self, file_paths, image_number)

    def duplicate_image_to_slot(self, source_slot: int, target_slot: int) -> None:
        loading.duplicate_image_to_slot(self, source_slot, target_slot)

    def _invalidate_image_canvas_render_state(self, clear_overlay_state: bool = False):
        return canvas_invalidate.invalidate_image_canvas_render_state(self, clear_overlay_state)

    def _schedule_image_canvas_update(self):
        return canvas_invalidate.schedule_image_canvas_update(self)

    def set_current_image(
        self, image_number: int, force_refresh: bool = False, emit_signal: bool = True
    ):
        loading.set_current_image(self, image_number, force_refresh, emit_signal)

    def _unify_images_worker_task(
        self, img1, img2, path1, path2, task_or_signal, method_name
    ):
        return image_decode.unify_images_worker_task(self, img1, img2, path1, path2, task_or_signal, method_name)

    def _on_unified_images_ready(self, result):
        loading.on_unified_images_ready(self, result)

    def _start_pyramid_builds(self, *stores):
        loading.start_pyramid_builds(self, *stores)

    def _on_pyramid_level_ready(self, payload):
        loading.on_pyramid_level_ready(self, payload)

    def _get_toast_manager(self):
        return self._loading_toast_coordinator.get_toast_manager()

    # Single source for progress checkpoints: re-export from shared
    # (legacy alias kept for tests that read controller._DECODE_DONE_PROGRESS).
    @property
    def _DECODE_DONE_PROGRESS(self) -> int:  # type: ignore[override]
        from tabs._shared.loading_toast import DECODE_DONE_PROGRESS as _V

        return _V

    @property
    def _PYRAMID_START_PROGRESS(self) -> int:  # type: ignore[override]
        from tabs._shared.loading_toast import PYRAMID_START_PROGRESS as _V

        return _V

    def _show_loading_toast(self, image_number: int) -> None:
        self._loading_toast_coordinator.show(image_number)

    def _set_loading_toast_progress(self, image_number: int, percent: int) -> None:
        self._loading_toast_coordinator.set_progress(image_number, percent)

    def _mark_full_res_ready(self, image_number: int) -> None:
        self._loading_toast_coordinator.mark_full_res_ready(image_number)

    def _finish_loading_toast(self, image_number: int) -> None:
        self._loading_toast_coordinator.finish(image_number)

    def _trigger_metrics_calculation_if_needed(self):
        from tabs.image_compare.use_cases.metrics_trigger import trigger_metrics_calculation_if_needed
        return trigger_metrics_calculation_if_needed(self)
    def _trigger_full_diff_generation(self):
        from tabs.image_compare.use_cases.metrics_trigger import trigger_full_diff_generation
        return trigger_full_diff_generation(self)


