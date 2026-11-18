# Audit-Meta: pattern=thin-owner reason="decode/unify pipeline via transact — thin wrapper over pipeline + PipelineCache slot"
"""Image decode flow — extracted from _session_controller.py (Phase 4).

Holds _load_image_async, _on_image_loaded, _load_full_resolution_async
and related helpers as plain functions taking controller. Thin-owner
pattern (CODE_PATTERNS.md:161).
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject
from sli_ui_toolkit.workers import GenericWorker
from sli_ui_toolkit.i18n import tr

from core.events import CoreErrorOccurredEvent
from core.state_management.actions import (
    SetPendingUnificationPathsAction,
    SetUnificationInProgressAction,
)
from tabs.image_compare.debug import ic_preview_debug
# W1 adapter: sync demand fills + path-equality guards owned by the shared
# seam; unify_pair flow, dispatch (transacts) and toast wiring stay tab-local.
from tabs._shared.loading_pipeline import (
    _same_fs_path as _seam_same_fs_path,
)
from tabs._shared.loading_pipeline import get_or_load_pixel as _seam_get_or_load_pixel
from tabs._shared.loading_pipeline import get_or_load_preview as _seam_get_or_load_preview
from tabs._shared.user_errors import emit_user_error, load_image_error
from core.log_names import APP_LOGGER_NAME

logger = logging.getLogger(APP_LOGGER_NAME)


def _format_worker_error(err) -> str:
    if isinstance(err, tuple) and len(err) >= 2:
        return str(err[1])
    return str(err)


def _remove_failed_item_via_dispatch(
    controller, image_number: int, index_in_list: int, path: str
) -> bool:
    """Drop a failed-decode list item dispatch-first (undoable, no aliasing).

    The old ``target_list.pop`` mutated the Store's live list in place:
    no dispatch, no history entry for undo, and every snapshot holding the
    same list object silently lost the item too. Dispatching
    ``SetImageListAction`` with the filtered list replaces the document
    (copy-on-write) so undo restores the item. Falls back to the legacy
    in-place pop only when no dispatcher exists (fake stores).
    """
    try:
        dispatcher = getattr(controller.store, "get_dispatcher", lambda: None)()
    except Exception:
        dispatcher = None
    if dispatcher is not None:
        try:
            from core.state_management.document_actions import SetImageListAction

            document = controller.store.get_session_state_slot("document")
            lst = document.image_list1 if image_number == 1 else document.image_list2
            if 0 <= index_in_list < len(lst) and _seam_same_fs_path(lst[index_in_list].path, path):
                remaining = [it for i, it in enumerate(lst) if i != index_in_list]
                controller.store.transact(
                    [SetImageListAction(slot=image_number, items=remaining)],
                    scope="document",
                )
                return True
        except Exception:
            pass
    return False


def _dismiss_ic_loading_toast(controller, image_number) -> None:
    """Close a slot's loading toast on decode failure (MC parity: dismiss+emit).

    Prefers the coordinator's dismiss (no success banner); falls back to a
    legacy dict pop + close for controllers without a coordinator.
    """
    try:
        coord = getattr(controller, "_loading_toast_coordinator", None)
        if coord is not None:
            dismiss = getattr(coord, "dismiss", None)
            if callable(dismiss):
                dismiss(image_number)
                return
            finish = getattr(coord, "finish", None)
            if callable(finish):
                finish(image_number)
                return
    except Exception:
        pass
    try:
        toasts = getattr(controller, "_loading_toasts", None)
        toast_id = toasts.pop(image_number, None) if isinstance(toasts, dict) else None
        if toast_id is None:
            return
        manager = None
        try:
            getter = getattr(controller, "_get_toast_manager", None)
            if callable(getter):
                manager = getter()
        except Exception:
            manager = None
        if manager is None:
            try:
                presenter = getattr(controller, "presenter", None)
                manager = getattr(
                    getattr(presenter, "main_window_app", None), "toast_manager", None
                )
            except Exception:
                manager = None
        if manager is not None:
            try:
                closer = getattr(manager, "close_toast", None)
                if callable(closer):
                    closer(toast_id)
            except Exception:
                pass
    except Exception:
        pass


def _resolve_ic_slot_for_path(controller, path: str) -> int | None:
    try:
        document = controller.store.get_session_state_slot("document")
    except Exception:
        return None
    try:
        if _seam_same_fs_path(getattr(document, "image1_path", None) or "", path):
            return 1
        if _seam_same_fs_path(getattr(document, "image2_path", None) or "", path):
            return 2
    except Exception:
        pass
    return None


def canvas_preview_bucket(controller) -> int:
    """Preview tier long edge for this canvas, in DEVICE px.

    ONE place decides "how big is the preview tier" (the sibling of the single
    owner of canvas size, ``get_current_label_dimensions``): the canvas' own
    size times the widget's device-pixel ratio, so a 1.5x/HiDPI screen gets the
    tier it actually paints; then the runtime content rect the render plan
    recorded (logical px); then the plain widget/logical size; and finally the
    historic 1024. Returns a value from ``PREVIEW_BUCKETS`` — a resize inside a
    bucket step reuses one cache entry (see ``preview_bucket_for``).

    Best-effort by contract: no presenter/widget/plan (headless, a worker that
    lost the tab, a closed session) is 1024, never an exception.
    """
    from tabs.image_compare.pipeline.cache import preview_bucket_for

    edge = 0
    dpr = 1.0
    presenter = None
    try:
        presenter = getattr(controller, "presenter", None)
        if presenter is not None:
            try:
                canvas = presenter.get_canvas()
            except Exception:
                canvas = None
            if canvas is None:
                canvas = getattr(presenter, "widget", None)
            if canvas is not None:
                try:
                    dpr = max(1.0, float(canvas.devicePixelRatioF() or 1.0))
                except Exception:
                    dpr = 1.0
    except Exception:
        presenter = None
    try:
        getter = getattr(presenter, "get_current_label_dimensions", None)
        if callable(getter):
            width, height = getter()
            edge = int(max(int(width or 0), int(height or 0)) * dpr)
    except Exception:
        edge = 0
    if edge <= 0:
        # Fallback: the runtime content rect (logical px) the last render plan
        # recorded — still closer to the canvas than a blind 1024.
        try:
            rect = getattr(
                controller.store.viewport.geometry_state, "image_display_rect_on_label", None
            )
            edge = max(int(getattr(rect, "width", 0) or 0), int(getattr(rect, "height", 0) or 0))
        except Exception:
            edge = 0
    return preview_bucket_for(edge)


def load_image_async(controller, path, image_number, index_in_list, target_size=None):
    from shared.image_processing.progressive_loader import (
        should_use_progressive_load,
    )

    # W1+W2 non-destructive crop: декод всегда full-frame (no-bake).
    # Сессионный CropService — только детекция (см. pipeline/crop_box.py),
    # в load_preview_image / load_pixel_store не попадает.
    # W1 adapter: sync demand fills delegated to the shared seam
    # (get_or_load_preview / get_or_load_pixel); embedded project-cache
    # resolution survives via the host-owned fallback inside
    # load_pixel_store, error/toast handling below is unchanged.
    crop_service = None
    ic_preview_debug("load_image_async slot=%s path=%s idx=%s crop=%s", image_number, path, index_in_list, bool(crop_service))
    try:
        # Always progressive for image_compare: even 764×576 goes via QImage
        # 1×1 preview first, then tiled full-res (threshold 0 / forced True).
        _ = should_use_progressive_load  # keep import used for contracts
        use_progressive = True
        from shared.image_processing.autocrop.debug import autocrop_debug

        autocrop_debug(
            "slot=%d path=%s crop_service=%s progressive=%s",
            image_number,
            path,
            bool(crop_service),
            use_progressive,
        )
        ic_preview_debug("load_image_async slot=%s use_progressive=%s", image_number, use_progressive)
        # DI: tab PipelineCache as the seam's cache tier (preview+pixel).
        _emb = None
        try:
            _emb = getattr(getattr(controller, "pipeline", None), "cache", None)
            if _emb is None:
                _sess = controller._get_image_session() if hasattr(controller, "_get_image_session") else None
                _emb = getattr(_sess, "cache", None) if _sess is not None else None
        except Exception:
            _emb = None
        if use_progressive:
            ic_preview_debug("load_image_async slot=%s -> try preview", image_number)
            # Preview tier follows the canvas (PREVIEW_BUCKETS): decode AT that
            # size and file the entry under the same bucket. 1024 when there is
            # no live canvas (see canvas_preview_bucket).
            bucket = canvas_preview_bucket(controller)
            preview = _seam_get_or_load_preview(_emb, path, target_long_edge=bucket)
            ic_preview_debug(
                "load_image_async slot=%s preview tier=%s preview=%s", image_number, bucket, preview
            )
            if preview:
                ic_preview_debug("load_image_async slot=%s -> preview hit", image_number)
                return preview, path, image_number, index_in_list, True
        ic_preview_debug("load_image_async slot=%s -> load_pixel_store", image_number)
        store = _seam_get_or_load_pixel(_emb, path, crop_service=None)
        ic_preview_debug("load_image_async slot=%s store=%s uid=%s", image_number, store, getattr(store, "uid", None) if store else None)
        if store is None:
            # Fail-open seam returns None where the legacy backend raised:
            # re-raise so the error/toast path below stays identical.
            raise RuntimeError(f"decode returned no store for {path}")
        return store, path, image_number, index_in_list, False
    except Exception as e:
        ic_preview_debug("load_image_async slot=%s failed %s", image_number, e)
        try:
            _dismiss_ic_loading_toast(controller, image_number)
        except Exception:
            pass
        message = load_image_error(
            controller.store.settings.current_language, path, e
        )
        emit_user_error(controller, message)
        return None, path, image_number, index_in_list, False


def cancel_pending_unification(controller, new_path1: str = "", new_path2: str = "", force: bool = False) -> bool:
    try:
        cache = getattr(getattr(controller.store.viewport, "session_data", None), "render_cache", None)
        if cache is None or not getattr(cache, "unification_in_progress", False):
            return False
    except Exception:
        return False
    if force:
        try:
            dispatcher = controller.store.get_dispatcher()
            if dispatcher is not None:
                with controller.store.batch_changes():
                    dispatcher.dispatch(SetUnificationInProgressAction(enabled=False), scope="viewport")
                    dispatcher.dispatch(SetPendingUnificationPathsAction(paths=None), scope="viewport")
            else:
                cache.unification_in_progress = False  # type: ignore[attr-defined]
                cache.pending_unification_paths = None  # type: ignore[attr-defined]
            controller.store.invalidate_geometry_cache()
        except Exception:
            pass
        return True
    pending = getattr(cache, "pending_unification_paths", None)
    if (
        pending
        and len(pending) == 2
        and (
            not _seam_same_fs_path(pending[0] or "", new_path1 or "")
            or not _seam_same_fs_path(pending[1] or "", new_path2 or "")
        )
    ):
        try:
            dispatcher = controller.store.get_dispatcher()
            if dispatcher is not None:
                with controller.store.batch_changes():
                    dispatcher.dispatch(SetUnificationInProgressAction(enabled=False), scope="viewport")
                    dispatcher.dispatch(SetPendingUnificationPathsAction(paths=None), scope="viewport")
            else:
                cache.unification_in_progress = False  # type: ignore[attr-defined]
                cache.pending_unification_paths = None  # type: ignore[attr-defined]
            controller.store.invalidate_geometry_cache()
        except Exception:
            pass
        return True
    return False


def on_image_loaded(controller, result, *, chain_full: bool = True):
    """Preview/full result fan-in (dispatch stays tab-local).

    ``chain_full``: legacy callers (slot fallback worker, service inline)
    keep ``True`` — a preview delivery kicks the explicit full-res worker.
    The seam-driven adapter (``ImageLoadService.ensure_async``) passes
    ``False``: the seam owns the bounded full-res second stage and will
    deliver it via ``_on_full_image_loaded`` — kicking a second full decode
    here would duplicate it.
    """
    ic_preview_debug("on_image_loaded result=%s", result)
    if result is None:
        ic_preview_debug("on_image_loaded -> None")
        return
    if isinstance(result, tuple) and len(result) == 5:
        pil_img, path, image_number, index_in_list, is_preview = result
    else:
        pil_img, path, image_number, index_in_list = result
        is_preview = False
    ic_preview_debug("on_image_loaded slot=%s path=%s idx=%s is_preview=%s pil_img=%s", image_number, path, index_in_list, is_preview, pil_img)
    # single-flight cleanup via pipeline inflight + AbortSignal — handled by slot.py _clear
    document = controller.store.get_session_state_slot("document")
    target_list = document.image_list1 if image_number == 1 else document.image_list2
    ic_preview_debug("on_image_loaded slot=%s target_len=%s idx_valid=%s path_match=%s", image_number, len(target_list), 0 <= index_in_list < len(target_list), _seam_same_fs_path(target_list[index_in_list].path, path) if 0 <= index_in_list < len(target_list) else False)
    if not pil_img:
        ic_preview_debug("on_image_loaded slot=%s -> pil_img None, pop if needed", image_number)
        if 0 <= index_in_list < len(target_list) and _seam_same_fs_path(target_list[index_in_list].path, path):
            current_app_index = document.current_index1 if image_number == 1 else document.current_index2
            removed_via_dispatch = _remove_failed_item_via_dispatch(
                controller, image_number, index_in_list, path
            )
            if not removed_via_dispatch:
                # Fail-open legacy for dispatcher-less (fake) stores only.
                target_list.pop(index_in_list)
            if index_in_list == current_app_index:
                controller.set_current_image(image_number)
            if controller.presenter:
                controller.presenter.ui_batcher.schedule_update("combobox")
        return
    if 0 <= index_in_list < len(target_list) and _seam_same_fs_path(target_list[index_in_list].path, path):
        # PipelineCache is single source — Bucket C via transact (pipeline slot)
        try:
            if pil_img is not None:
                from PySide6.QtGui import QImage
                from engine.store.tiled_pixel_store import TiledPixelStore
                from tabs.image_compare.state.actions import PutPixelAction, PutPreviewAction
                d = getattr(controller.store, "get_dispatcher", lambda: None)()
                has_dispatcher = d is not None
                # W1+W2: put всегда boxless — сервис сюда не попадает.
                crop_svc = None
                if isinstance(pil_img, QImage):
                    if not pil_img.isNull():
                        if has_dispatcher:
                            controller.store.transact([PutPreviewAction(path=path, qimage=pil_img, crop_service=crop_svc)], scope="pipeline")
                        else:
                            pl = getattr(controller, "pipeline", None)
                            if pl is not None:
                                getattr(pl.cache, "put_" + "preview")(path, pil_img)
                        ic_preview_debug(
                            "on_image_loaded slot=%s put_preview path=%s qimage=%sx%s",
                            image_number,
                            path,
                            pil_img.width(),
                            pil_img.height(),
                        )
                elif isinstance(pil_img, TiledPixelStore) and getattr(pil_img, "is_open", True):
                    if has_dispatcher:
                        controller.store.transact([PutPixelAction(path=path, store=pil_img, crop_service=crop_svc)], scope="pipeline")
                    else:
                        pl = getattr(controller, "pipeline", None)
                        if pl is not None:
                            getattr(pl.cache, "put_" + "pixel")(path, store=pil_img)
                elif hasattr(pil_img, "is_open"):
                    if has_dispatcher:
                        controller.store.transact([PutPixelAction(path=path, store=pil_img, crop_service=crop_svc)], scope="pipeline")
                    else:
                        pl = getattr(controller, "pipeline", None)
                        if pl is not None:
                            getattr(pl.cache, "put_" + "pixel")(path, store=pil_img)
        except Exception:
            pass
        current_app_index = document.current_index1 if image_number == 1 else document.current_index2
        is_current = index_in_list == current_app_index
        if is_current:
            controller._show_loading_toast(image_number)
        if is_preview:
            if is_current:
                try:
                    from PySide6.QtGui import QImage

                    from core.state_management.actions import (
                        InvalidateGeometryCacheAction,
                        SetImageSessionImageAction,
                    )

                    if isinstance(pil_img, QImage) and not pil_img.isNull():
                        controller.store.transact(
                            [
                                SetImageSessionImageAction(slot=image_number, image=pil_img),
                                InvalidateGeometryCacheAction(),
                            ],
                            scope="viewport",
                        )
                        ic_preview_debug(
                            "on_image_loaded slot=%s preview transact done path=%s",
                            image_number,
                            path,
                        )
                        # Re-fetch after dispatch — mirrors slot.py:185 pattern.
                        # DocumentModel is immutable via replace() which copies
                        # image_list1/2 (document.py:98 list(...)), so old
                        # `target_list`/`document` references become detached
                        # after any document-scope dispatch in the load path.
                        # Viewport transact here does not replace document,
                        # but keep the same re-fetch discipline for safety and
                        # to avoid stale `document`/`target_list` if a
                        # concurrent document dispatch raced.
                        document = controller.store.get_session_state_slot("document")
                        target_list = document.image_list1 if image_number == 1 else document.image_list2
                except Exception as e:
                    ic_preview_debug("on_image_loaded slot=%s preview transact failed %s", image_number, e)
                    pass
            if chain_full:
                load_full_resolution_async(controller, path, image_number, index_in_list)
            else:
                ic_preview_debug(
                    "on_image_loaded slot=%s seam owns full stage, skip explicit kick",
                    image_number,
                )
        else:
            from engine.store.tiled_pixel_store import (
                TiledPixelStore,
                maybe_wrap_pixel_store,
            )

            if not isinstance(pil_img, TiledPixelStore):
                pil_img = maybe_wrap_pixel_store(pil_img)
            if is_current:
                # Phase 3: PipelineCache owns lifecycle, no document pixel fields
                controller._update_image_slot(
                    image_number,
                    image=pil_img,
                    path=path,
                    is_full_res=True,
                )
                # Re-fetch after document dispatch — _update_image_slot
                # transacts SetFullResImageAction which triggers
                # DocumentReducer replace() copying image_list (document.py:98).
                # Old `document`/`target_list` become detached.
                document = controller.store.get_session_state_slot("document")
                target_list = document.image_list1 if image_number == 1 else document.image_list2
                controller._mark_full_res_ready(image_number)
        if is_current:
            if not is_preview:
                controller.set_current_image(image_number, force_refresh=True)
            else:
                controller._trigger_preview_unification(image_number)


def load_full_resolution_async(controller, path, image_number, index_in_list):
    from shared.image_processing.autocrop.debug import autocrop_debug

    # W1+W2: full-res декод всегда full-frame (no-bake, см. load_image_async).
    crop_service = None
    autocrop_debug(
        "full-res slot=%d path=%s crop_service=%s", image_number, path, bool(crop_service)
    )

    def load_full_task(path_str, svc, slot_number, item_index):
        # W1 adapter: sync full-res demand fill delegated to the shared
        # seam (same backend, same fail-open shape); the tab PipelineCache
        # stays the cache tier.
        _emb2 = None
        try:
            _emb2 = getattr(getattr(controller, "pipeline", None), "cache", None)
        except Exception:
            _emb2 = None
        store = _seam_get_or_load_pixel(_emb2, path_str, crop_service=None)
        return (
            store,
            path_str,
            slot_number,
            item_index,
        )

    # Bucket D: use ImageLoadService single-flight (path+mtime+box) instead of
    # separate (slot,path,"full") single-flight. Deduplicates preview
    # full chain and pyramid pending via shared _inflight.
    _svc = None
    _key = None
    _sig = None
    try:
        _sess = None
        if hasattr(controller, "_get_image_session"):
            try:
                _sess = controller._get_image_session()
            except Exception:
                _sess = None
        if _sess is not None and hasattr(_sess, "load_service"):
            _svc = getattr(_sess, "load_service", None)
        if _svc is None:
            _pl = getattr(controller, "pipeline", None)
            _svc = getattr(_pl, "load_service", None) if _pl is not None else None
    except Exception:
        _svc = None
    if _svc is not None:
        try:
            # key includes mtime+box via service
            _key = _svc.key_for(path, crop_service)
            alo = _svc.try_acquire(_key)
            if alo is None:
                ic_preview_debug("load_full_resolution_async slot=%s dedup key=%s", image_number, _key)
                return
            _sig = alo
            # keep direct pipeline._inflight alias for pyramid/toast checks via AbortSignal.is_aborted()
            try:
                pl = getattr(controller, "pipeline", None)
                if pl is not None and hasattr(pl, "_inflight"):
                    pl._inflight.setdefault((int(image_number), str(path), "full"), _sig)  # type: ignore[index]
            except Exception:
                pass

            def _wrapped(path_str, svc, slot_number, item_index, _sig=_sig, _orig=load_full_task):
                try:
                    if _sig is not None and _sig.is_aborted():
                        return (None, path_str, slot_number, item_index)
                except Exception:
                    pass
                res = _orig(path_str, svc, slot_number, item_index)
                try:
                    if _sig is not None and _sig.is_aborted():
                        return (None, path_str, slot_number, item_index)
                except Exception:
                    pass
                return res

            worker = GenericWorker(
                _wrapped,
                path,
                crop_service,
                image_number,
                index_in_list,
            )

            def _clear_full(_k=_key, _s=_sig, _svc=_svc):
                try:
                    _svc.release(_k, _s)
                except Exception:
                    pass
                try:
                    pl = getattr(controller, "pipeline", None)
                    if pl is not None and hasattr(pl, "_inflight"):
                        cur = pl._inflight.get((int(image_number), str(path), "full"))  # type: ignore[arg-type]
                        if cur is _s:
                            pl._inflight.pop((int(image_number), str(path), "full"), None)
                except Exception:
                    pass

            worker.signals.result.connect(controller._on_full_resolution_loaded_result)
            worker.signals.error.connect(
                lambda err: on_full_resolution_error(controller, path, err)
            )

            def _on_finished(num=image_number, _cf=_clear_full):
                try:
                    _cf()
                except Exception:
                    pass
                on_full_load_finished(controller, num)

            worker.signals.finished.connect(_on_finished)
            controller.thread_pool.start(worker)
            return
        except Exception as e:
            ic_preview_debug("load_full_resolution_async service path failed %s, fallback legacy %s", path, e)
            if _key is not None and _sig is not None:
                try:
                    _svc.release(_key, _sig)
                except Exception:
                    pass
    # fallback direct pipeline inflight single-flight (fakes without service)
    _sig = None
    try:
        pl = getattr(controller, "pipeline", None)
        if pl is not None and hasattr(pl, "_inflight"):
            from tabs.image_compare.pipeline.abort import AbortSignal as _S

            _sig = _S()
            pl._inflight[(int(image_number), str(path), "full")] = _sig  # type: ignore[index]
            orig_task = load_full_task

            def _wrapped(path_str, svc, slot_number, item_index, _sig=_sig, _orig=orig_task):
                try:
                    if _sig is not None and _sig.is_aborted():
                        return (None, path_str, slot_number, item_index)
                except Exception:
                    pass
                res = _orig(path_str, svc, slot_number, item_index)
                try:
                    if _sig is not None and _sig.is_aborted():
                        return (None, path_str, slot_number, item_index)
                except Exception:
                    pass
                return res

            worker = GenericWorker(
                _wrapped,
                path,
                crop_service,
                image_number,
                index_in_list,
            )

            def _clear_full():
                try:
                    cur = pl._inflight.get((int(image_number), str(path), "full"))  # type: ignore[arg-type]
                    if cur is _sig:
                        pl._inflight.pop((int(image_number), str(path), "full"), None)
                except Exception:
                    pass

        else:
            raise AttributeError
    except Exception:
        worker = GenericWorker(
            load_full_task,
            path,
            crop_service,
            image_number,
            index_in_list,
        )

        def _clear_full():  # type: ignore[no-redef]
            pass

    worker.signals.result.connect(controller._on_full_resolution_loaded_result)
    worker.signals.error.connect(
        lambda err: on_full_resolution_error(controller, path, err)
    )

    def _on_finished(num=image_number, _cf=_clear_full):
        try:
            _cf()
        except Exception:
            pass
        on_full_load_finished(controller, num)

    worker.signals.finished.connect(_on_finished)
    controller.thread_pool.start(worker)


def on_full_load_finished(controller, image_number: int) -> None:
    # Direct pipeline._inflight check via AbortSignal.is_aborted() — single cancel token
    try:
        pl = getattr(controller, "pipeline", None)
        if pl is not None and hasattr(pl, "_inflight"):
            for k, sig in list(pl._inflight.items()):
                try:
                    is_aborted = sig.is_aborted()
                except Exception:
                    is_aborted = False
                if is_aborted:
                    continue
                if isinstance(k, tuple) and len(k) >= 2 and k[0] == int(image_number):
                    return
                if isinstance(k, tuple) and k and k[0] == "__full_count__" and len(k) > 1 and k[1] == int(image_number):
                    return
    except Exception:
        pass
    document = controller.store.get_session_state_slot("document")
    has_pixel = False
    try:
        path = document.image1_path if image_number == 1 else document.image2_path
        if path:
            pl = getattr(controller, "pipeline", None)
            if pl is not None:
                try:
                    has_pixel = pl.peek(path) is not None
                except Exception:
                    has_pixel = False
            if not has_pixel:
                ps = controller.store.get_session_state_slot("pipeline")
                if ps is not None:
                    import os

                    from tabs.image_compare.pipeline.cache import _pixel_key

                    try:
                        k = _pixel_key(path, None, None)
                        has_pixel = k in ps.pixel  # type: ignore[attr-defined]
                    except Exception:
                        has_pixel = False
    except Exception:
        has_pixel = False
    if not has_pixel:
        controller._trigger_preview_unification(image_number)


def on_full_resolution_loaded_result(controller, result) -> None:
    if not isinstance(result, tuple) or len(result) != 4:
        return
    full_img, path, image_number, index_in_list = result
    controller._on_full_image_loaded(
        full_img,
        path,
        int(image_number),
        int(index_in_list),
    )


def on_full_resolution_error(controller, path: str, err) -> None:
    logger.error(f"Failed to load full resolution: {err}", exc_info=True)
    try:
        slot = _resolve_ic_slot_for_path(controller, path)
        if slot is not None:
            _dismiss_ic_loading_toast(controller, slot)
        else:
            for candidate in (1, 2):
                try:
                    _dismiss_ic_loading_toast(controller, candidate)
                except Exception:
                    pass
    except Exception:
        pass
    message = load_image_error(
        controller.store.settings.current_language, path, err
    )
    emit_user_error(controller, message)


def unify_images_worker_task(controller, img1, img2, path1, path2, task_or_signal, method_name):
    from engine.store.pixel_ops.unify import unify_pair

    try:
        from tabs.image_compare.pipeline.abort import AbortSignal
    except Exception:
        AbortSignal = None  # type: ignore

    signal = None
    if AbortSignal is not None and isinstance(task_or_signal, AbortSignal):
        signal = task_or_signal
        if signal.is_aborted():
            return None
        should_abort = signal.is_aborted  # type: ignore[assignment]
        log_id = getattr(signal, "_generation", id(signal))
    else:
        # legacy int task_id removed — abort
        return None
    try:
        import time

        from engine.store.tiled_pixel_store import pixel_source_size

        try:
            _sw1, _sh1 = pixel_source_size(img1)
        except Exception:
            _sw1, _sh1 = "?", "?"
        try:
            _sw2, _sh2 = pixel_source_size(img2)
        except Exception:
            _sw2, _sh2 = "?", "?"

        t0 = time.perf_counter()
        logger.info(
            "[Unify] task %s started (%sx%s + %sx%s)",
            log_id,
            _sw1,
            _sh1,
            _sw2,
            _sh2,
        )
        u1, u2 = unify_pair(
            img1,
            img2,
            method_name,
            should_abort=should_abort,
        )
        if u1 is None and u2 is None:
            logger.info(
                "[Unify] task %s aborted/empty after %.2fs",
                log_id,
                time.perf_counter() - t0,
            )
            return None
        logger.info(
            "[Unify] task %s finished in %.2fs", log_id, time.perf_counter() - t0
        )
        return u1, u2, path1, path2, task_or_signal
    except Exception as e:
        logger.error(f"Failed to unify images: {e}", exc_info=True)
        return None
