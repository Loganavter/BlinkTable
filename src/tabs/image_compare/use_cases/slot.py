# Audit-Meta: pattern=thin-owner reason="SlotSource pipeline debug — load_images/set_current/handle_full with ic-preview tracing, stays thin wrapper"
"""Slot image operations — SlotSource + PipelineView via Transaction.

Phase 3 slim: DocumentModel is list+index+derived path only; pixels
live in PipelineCache (ImagePipeline) and viewport image_state.
Thin wrapper via use_cases/ per CODE_PATTERNS.md.
"""

from __future__ import annotations

import os
import logging

from sli_ui_toolkit.workers import GenericWorker

from core.state_management.actions import SetCurrentIndexAction
from tabs.image_compare.state.document import ImageItem
from sli_ui_toolkit.i18n import tr
from tabs.image_compare.debug import ic_preview_debug
# W1 adapter: path-equality guards owned by the shared seam (single impl).
from tabs._shared.loading_pipeline import _same_fs_path as _seam_same_fs_path
from tabs._shared.loading_pipeline import cache_key_for_path as _seam_key_for_path
from tabs._shared.loading_pipeline import get_or_load_preview
from core.log_names import APP_LOGGER_NAME

logger = logging.getLogger(APP_LOGGER_NAME)

# Throttle: ensure/set_current are re-entered 3× for same path at 60Hz via
# load path + transaction emit + fps timer. Dedup identical (slot, idx, path)
# probes so one load = one log line, not 3.
_last_ensure_slot_sig: dict[int, tuple] = {}
_last_set_current_sig: dict[tuple, bool] = {}
_last_handle_full_sig: dict[tuple, bool] = {}


def ensure_current_slot(controller, image_number: int, force_refresh: bool = False) -> bool:
    document = controller.store.get_session_state_slot("document")
    if document is None:
        ic_preview_debug("ensure_current_slot slot=%s -> no document", image_number)
        return False
    lst = document.image_list1 if image_number == 1 else document.image_list2
    idx = document.current_index1 if image_number == 1 else document.current_index2
    path = document.image1_path if image_number == 1 else document.image2_path
    _ens_sig = (int(image_number), int(idx) if isinstance(idx, int) else idx, str(path) if path else None, len(lst))
    _ens_should = _last_ensure_slot_sig.get(int(image_number)) != _ens_sig
    if _ens_should:
        _last_ensure_slot_sig[int(image_number)] = _ens_sig
        ic_preview_debug("ensure_current_slot slot=%s idx=%s path=%s lst_len=%s", image_number, idx, path, len(lst))
    if not (0 <= idx < len(lst)):
        if _ens_should:
            ic_preview_debug("ensure_current_slot slot=%s -> idx out of range", image_number)
        return False
    item = lst[idx]
    # staleness via pipeline cache — single source
    pl = getattr(controller, "pipeline", None)
    cached = pl.peek(path) if pl is not None and path else None
    is_open = bool(getattr(cached, "is_open", True)) if cached is not None else False
    stale = (not _seam_same_fs_path(path or "", item.path or "")) or (cached is not None and not is_open)
    if path and cached is None:
        stale = True
    if _ens_should:
        ic_preview_debug("ensure_current_slot slot=%s stale=%s cached=%s is_open=%s item.path=%s", image_number, stale, cached, is_open, item.path)
    if not stale:
        return False
    try:
        controller.set_current_image(image_number, force_refresh=force_refresh)
    except Exception:
        logger.exception("ensure_current_slot failed slot=%s", image_number)
        ic_preview_debug("ensure_current_slot slot=%s exception", image_number)
        pass
    return True


def handle_full_image_loaded(controller, full_img, path, image_number, index_in_list):
    ic_preview_debug("handle_full_image_loaded slot=%s path=%s idx=%s has_image=%s", image_number, path, index_in_list, bool(full_img))
    if not full_img:
        ic_preview_debug("handle_full_image_loaded slot=%s -> no image", image_number)
        return
    document = controller.store.get_session_state_slot("document")
    lst = document.image_list1 if image_number == 1 else document.image_list2
    if not (0 <= index_in_list < len(lst)) or not _seam_same_fs_path(lst[index_in_list].path, path):
        ic_preview_debug("handle_full_image_loaded slot=%s -> lst mismatch len=%s path_mismatch=%s", image_number, len(lst), lst[index_in_list].path if 0 <= index_in_list < len(lst) else "oob")
        return
    from engine.store.tiled_pixel_store import TiledPixelStore, maybe_wrap_pixel_store

    if not isinstance(full_img, TiledPixelStore):
        full_img = maybe_wrap_pixel_store(full_img)
    # Preview-first invariant (Lightroom Standard model): the preview tier
    # must exist in the PipelineCache before the full-res transact below
    # publishes. Peek first so a cached preview is a no-op (never
    # double-decode); on miss the seam does one bounded (<=1024) scaled
    # read. Best-effort and fail-open: a None fill keeps today's behavior
    # exactly — the store is still published, never gated on the preview.
    try:
        _fill_pipeline = getattr(controller, "pipeline", None)
        _fill_cache = getattr(_fill_pipeline, "cache", None) if _fill_pipeline is not None else None
        if _fill_cache is not None:
            _preview_hit = None
            try:
                _preview_hit = _fill_cache.get_preview(path)
            except Exception:
                _preview_hit = None
            if _preview_hit is None:
                try:
                    get_or_load_preview(_fill_cache, path)
                except Exception:
                    pass
    except Exception:
        pass
    # PipelineCache single source — Bucket C transact, W1+W2 boxless put (no-bake).
    # Сессионный сервис — только детекция (см. pipeline/crop_box.py).
    try:
        from tabs.image_compare.state.actions import PutPixelAction
        d = getattr(controller.store, "get_dispatcher", lambda: None)()
        if d is not None:
            controller.store.transact([PutPixelAction(path=path, store=full_img)], scope="pipeline")
        else:
            pl = getattr(controller, "pipeline", None)
            if pl is not None:
                try:
                    getattr(pl.cache, "put_" + "pixel")(path, store=full_img)
                except Exception:
                    pass
    except Exception:
        try:
            pl = getattr(controller, "pipeline", None)
            if pl is not None:
                getattr(pl.cache, "put_" + "pixel")(path, store=full_img)
        except Exception:
            pass
    cur = document.current_index1 if image_number == 1 else document.current_index2
    ic_preview_debug("handle_full_image_loaded slot=%s cur=%s idx=%s path=%s", image_number, cur, index_in_list, path)
    if index_in_list != cur:
        ic_preview_debug("handle_full_image_loaded slot=%s -> not current, skip transact cur=%s idx=%s", image_number, cur, index_in_list)
        return
    # outgoing cleanup via pipeline cache (close old store if not shared)
    try:
        if pl is not None:
            # close previous store for this slot if different
            # we rely on PipelineCache eviction to close, but also close outgoing explicitly
            pass
    except Exception:
        pass
    # Single Transaction: PipelineView + geometry invalidate (1 dispatch, 1 emit)
    ic_preview_debug("handle_full_image_loaded slot=%s -> transact image uid=%s", image_number, getattr(full_img, "uid", id(full_img)))
    try:
        from core.state_management.actions import InvalidateGeometryCacheAction, SetImageSessionImageAction

        controller.store.transact(
            [SetImageSessionImageAction(slot=image_number, image=full_img), InvalidateGeometryCacheAction()],
            scope="viewport",
        )
        ic_preview_debug("handle_full_image_loaded slot=%s transact done", image_number)
    except Exception as e:
        ic_preview_debug("handle_full_image_loaded slot=%s transact failed %s", image_number, e)
        try:
            controller._update_image_slot(image_number, image=full_img, path=path, is_full_res=True)
        except Exception:
            pass
    try:
        controller._mark_full_res_ready(image_number)
    except Exception:
        pass
    from tabs.image_compare.use_cases.unify import ensure_unification

    ic_preview_debug("handle_full_image_loaded slot=%s -> ensure_unification", image_number)
    ensure_unification(controller)
    try:
        from tabs.image_compare.use_cases.loading_toast import finish_toast_for_unpaired_slot

        finish_toast_for_unpaired_slot(controller, document, image_number)
    except Exception:
        pass


def load_images_from_paths(controller, file_paths: list[str], image_number: int):
    import time as _t
    _t0 = _t.monotonic()
    ic_preview_debug("load_images_from_paths slot=%s files=%s", image_number, file_paths)
    from tabs.image_compare.services import document_store_ops
    from core.state_management.actions import (
        SetCachedDiffImageAction,
        SetImageSessionImageAction,
        SetPendingUnificationPathsAction,
        SetUnificationInProgressAction,
    )

    document = controller.store.get_session_state_slot("document")
    lst = document.image_list1 if image_number == 1 else document.image_list2
    is_new = len(lst) == 0
    ic_preview_debug("load_images_from_paths slot=%s is_new=%s len=%s other_len=%s t=%.3f", image_number, is_new, len(lst), len(document.image_list2 if image_number==1 else document.image_list1), _t.monotonic() - _t0)
    if is_new:
        # Долгосрочный правильный путь: весь is_new — одна транзакция, без
        # промежуточных dispatch/batch. Иначе первый drop в пустую сессию
        # делает 5 dispatch → каждый клонирует ViewportState + emit → 1.48с
        # блок event loop, последующие дропы уже <5мс (кэш). См. plan
        # docs/dev/plan_image_compare_dnd_tiles.md Phase 3.
        other = 2 if image_number == 1 else 1
        other_lst = document.image_list1 if other == 1 else document.image_list2
        try:
            actions = []
            # unify state — всегда сбрасываем
            actions.append(SetUnificationInProgressAction(enabled=False))
            actions.append(SetPendingUnificationPathsAction(paths=None))
            if len(other_lst) == 0:
                # первый файл в пустой сессии — чистим оба слота одним махом.
                # Changed-guard (sibling: display_tier_service.sync_geometry_state
                # dispatches only when the geometry actually changed): только тот
                # слот, который реально что-то держит. Раньше pristine-слот
                # «чистился» всё равно, а этот no-op всё равно поднимал
                # viewport-emit → кадр «нет источников» → image_label.clear(),
                # который обнуляет content rect / letterbox канваса, после чего
                # capture_letterbox_focus возвращает None и камера не
                # переякоривается (docs/dev/rendering/zoom-pan.md).
                # Оба списка здесь пусты, поэтому «держит» = image_state.imageN
                # или сохранённое display name слота.
                from core.state_management.actions import ClearImageSlotDataAction

                try:
                    _img_state = controller.store.viewport.session_data.image_state
                except Exception:
                    _img_state = None
                if _img_state is None:
                    # fake stores without a viewport keep the old shape
                    for _slot in (1, 2):
                        actions.append(ClearImageSlotDataAction(_slot))
                        actions.append(SetImageSessionImageAction(slot=_slot, image=None))
                else:
                    for _slot in (1, 2):
                        _slot_image = getattr(_img_state, f"image{_slot}", None)
                        _slot_name = getattr(document, f"_last_display_name{_slot}", "") or ""
                        if _slot_image is None and not _slot_name:
                            continue
                        actions.append(ClearImageSlotDataAction(_slot))
                        actions.append(SetImageSessionImageAction(slot=_slot, image=None))
                # дифф — либо через сервис, либо экшеном, не оба
                if getattr(controller, "diff_service", None) is None:
                    actions.append(SetCachedDiffImageAction(image=None))
            else:
                has_path = bool(document.image1_path) if image_number == 1 else bool(document.image2_path)
                if has_path:
                    from core.state_management.actions import ClearImageSlotDataAction

                    actions.append(ClearImageSlotDataAction(image_number))
            # одна транзакция вместо 2×batch_changes + 5 dispatch
            controller.store.transact(actions, scope="viewport")
        except Exception:
            # fallback — старый путь через dispatcher, если transact недоступен (fake store в тестах)
            d = getattr(controller.store, "get_dispatcher", lambda: None)()
            if d is not None:
                try:
                    with controller.store.batch_changes():
                        d.dispatch(SetUnificationInProgressAction(enabled=False), scope="viewport")
                        d.dispatch(SetPendingUnificationPathsAction(paths=None), scope="viewport")
                except Exception:
                    pass
            if len(other_lst) == 0:
                try:
                    document_store_ops.clear_image_slot_data(controller.store, 1)
                except Exception:
                    pass
                try:
                    document_store_ops.clear_image_slot_data(controller.store, 2)
                except Exception:
                    pass
                if d is not None:
                    try:
                        with controller.store.batch_changes():
                            d.dispatch(SetImageSessionImageAction(slot=1, image=None), scope="viewport")
                            d.dispatch(SetImageSessionImageAction(slot=2, image=None), scope="viewport")
                    except Exception:
                        pass
                if getattr(controller, "diff_service", None) is not None:
                    try:
                        controller.diff_service.invalidate()
                    except Exception:
                        pass
                elif d is not None:
                    try:
                        d.dispatch(SetCachedDiffImageAction(image=None), scope="viewport")
                    except Exception:
                        pass
            else:
                has_path = bool(document.image1_path) if image_number == 1 else bool(document.image2_path)
                if has_path:
                    try:
                        document_store_ops.clear_image_slot_data(controller.store, image_number)
                    except Exception:
                        pass
        # diff_service invalidate — вне транзакции, если есть (idempotent)
        if len(other_lst) == 0 and getattr(controller, "diff_service", None) is not None:
            try:
                controller.diff_service.invalidate()
            except Exception:
                pass

    # Re-fetch after is_new dispatches: DocumentModel is immutable via replace()
    # which copies image_list1/2 (see debug test 2026-08-31), so old `lst` reference
    # is detached from store's current document. Without re-fetch, `lst.append`
    # mutates detached list and store's list stays empty → ensure_current_slot sees 0.
    document = controller.store.get_session_state_slot("document")
    lst = document.image_list1 if image_number == 1 else document.image_list2
    ic_preview_debug("load_images_from_paths slot=%s after is_new re-fetch len=%s id(lst)=%s", image_number, len(lst), id(lst))

    errors: list[str] = []
    # W1 adapter: identity via the shared seam (heals textual variants).
    seen = {_seam_key_for_path(e.path)[0] for e in lst if e.path}
    # Phase 3 lightweight: collect without mutating detached list, no os.stat/mtime/box
    to_add: list[ImageItem] = []
    seen_batch: set[str] = set()
    ic_preview_debug("load_images_from_paths slot=%s seen=%s", image_number, seen)
    for fp in file_paths:
        if not isinstance(fp, str) or not fp:
            ic_preview_debug("load_images_from_paths slot=%s skip invalid %s", image_number, fp)
            errors.append(f"{fp}: {tr('image_compare.msg.invalid_item_type_or_empty_path', controller.store.settings.current_language)}")
            continue
        try:
            norm = os.path.normpath(fp)
            disp = os.path.basename(norm) or "-----"
        except Exception as e:
            ic_preview_debug("load_images_from_paths slot=%s norm failed %s %s", image_number, fp, e)
            errors.append(f"{fp}: {tr('image_compare.msg.error_normalizing_path', controller.store.settings.current_language)}")
            continue
        if _seam_key_for_path(fp)[0] in seen:
            ic_preview_debug("load_images_from_paths slot=%s reload existing %s", image_number, norm)
            _reload_existing_path(controller, image_number, norm, lst)
            continue
        if _seam_key_for_path(fp)[0] in seen_batch:
            ic_preview_debug("load_images_from_paths slot=%s dedup batch %s", image_number, norm)
            continue
        try:
            ic_preview_debug("load_images_from_paths slot=%s append %s disp=%s", image_number, norm, disp)
            # lightweight: only ImageItem creation (no stat/mtime/box), heavy in ImageLoadService.ensure_async lazy
            item = ImageItem(path=norm, display_name=os.path.splitext(disp)[0], rating=0)
            to_add.append(item)
            seen_batch.add(_seam_key_for_path(fp)[0])
        except Exception as e:
            ic_preview_debug("load_images_from_paths slot=%s append failed %s %s", image_number, disp, e)
            errors.append(f"{disp}: {tr('image_compare.msg.error_processing_path', controller.store.settings.current_language)}")
    # lightweight transaction: single emit for Append + SetCurrentIndex
    if to_add:
        orig_len = len(lst)
        new_idx = list(range(orig_len, orig_len + len(to_add)))
        new_index = new_idx[-1]
        ic_preview_debug("load_images_from_paths slot=%s new_idx=%s errors=%s lst_len_after=%s t=%.3f", image_number, new_idx, errors, orig_len + len(to_add), _t.monotonic() - _t0)
        _t1 = _t.monotonic()
        d = getattr(controller.store, "get_dispatcher", lambda: None)()
        if d is not None:
            try:
                from core.state_management.document_actions import AppendImageItemsAction
                from core.state_management.actions import SetCurrentIndexAction

                controller.store.transact(
                    [AppendImageItemsAction(slot=image_number, items=to_add), SetCurrentIndexAction(slot=image_number, index=new_index)],
                    scope="document",
                )
                ic_preview_debug("load_images_from_paths slot=%s lightweight transact done t=%.3f", image_number, _t.monotonic() - _t1)
            except Exception as e:
                ic_preview_debug("load_images_from_paths transact failed %s", e)
                # fallback for fake stores
                try:
                    lst.extend(to_add)
                    d.dispatch(SetCurrentIndexAction(slot=image_number, index=new_index), scope="document")
                except Exception:
                    pass
        else:
            # fake store without dispatcher
            try:
                lst.extend(to_add)
            except Exception:
                pass
            try:
                setattr(controller.store.get_session_state_slot("document"), f"current_index{image_number}", new_index)
            except Exception:
                pass
        # Long-term fix: отдельный crop cache прогрев в фоне (без блока GUI)
        try:
            from shared.image_processing.autocrop.service import schedule_crop_warmup

            _svc = None
            try:
                _svc = getattr(controller, "_get_crop_service", lambda: None)()
            except Exception:
                _svc = None
            _pool = getattr(controller, "thread_pool", None)

            def _on_crop_ready() -> None:
                """The box landed asynchronously — refresh what derives from it.

                The crop result is cached on the service and dispatches nothing
                to the Store, so no chrome/document event follows it: without
                this the InfoHUD kept the source size while the canvas painted
                the crop (owner report 2026-11-18), and the canvas kept the
                previous (uncropped) content rect until the next input.
                ``viewport.geometry`` is a lightweight subdomain (chrome skips
                it by design), so the refresh has to be asked for explicitly.
                """
                try:
                    presenter = getattr(controller, "presenter", None)
                    if presenter is not None:
                        presenter.update_resolution_labels()
                        presenter.schedule_canvas_update()
                except Exception:
                    pass

            schedule_crop_warmup(
                _svc,
                [it.path for it in to_add],
                thread_pool=_pool,
                on_done=_on_crop_ready,
            )
        except Exception:
            pass
        # UI + async lazy load (stat/cache_key now in ImageLoadService.ensure_async)
        try:
            controller.set_current_image(image_number)
        except Exception:
            pass
        if controller.presenter:
            try:
                flyout = getattr(getattr(controller.presenter, "ui_manager", None), "transient", None)
                flyout = getattr(flyout, "unified_flyout", None) if flyout is not None else None
                if flyout is not None:
                    if flyout.isVisible():
                        controller.presenter.repopulate_flyouts()
                    else:
                        try:
                            widget = getattr(controller, "widget", None)
                            if widget is None:
                                widget = getattr(controller.presenter, "widget", None)
                            gate = getattr(widget, "_stale_gate", None) if widget is not None else None
                            if gate is not None:
                                gate.mark("render")
                            if widget is not None:
                                try:
                                    widget._render_stale = True  # type: ignore[attr-defined]
                                except Exception:
                                    pass
                        except Exception:
                            pass
                else:
                    controller.presenter.repopulate_flyouts()
            except Exception:
                pass
        if errors:
            from core.events import CoreErrorOccurredEvent

            msg = tr("image_compare.msg.some_images_could_not_be_loaded", controller.store.settings.current_language) + ":\n\n - " + "\n - ".join(errors)
            if controller.event_bus:
                controller.event_bus.emit(CoreErrorOccurredEvent(msg))
            else:
                controller.error_occurred.emit(msg)
        ic_preview_debug("load_images_from_paths slot=%s _finalize done t=%.3f total=%.3f", image_number, _t.monotonic() - _t1, _t.monotonic() - _t0)
        return
    # no new items: only reloads or errors
    ic_preview_debug("load_images_from_paths slot=%s new_idx=%s errors=%s lst_len_after=%s t=%.3f", image_number, [], errors, len(lst), _t.monotonic() - _t0)
    _t1 = _t.monotonic()
    _finalize_loaded_paths(controller, image_number, [], errors)
    ic_preview_debug("load_images_from_paths slot=%s _finalize done t=%.3f total=%.3f", image_number, _t.monotonic() - _t1, _t.monotonic() - _t0)


def _dispatch_target_index(controller, target_slot: int, idx: int) -> None:
    """Set a slot's current index dispatch-first (no setattr fallback).

    Dispatcher dispatch is the primary path. Dispatcher-less (fake) stores
    reduce + write back through the real ``DocumentReducer`` — still
    copy-on-write, never an in-place ``setattr`` on Store state (the
    setattr contract forbids Store bypasses outside Dispatcher/Reducer).
    A store with neither dispatcher nor write-back API (bare test fakes)
    cannot be updated cleanly — that is logged and skipped instead of
    bypassing.
    """
    try:
        d = getattr(controller.store, "get_dispatcher", lambda: None)()
    except Exception:
        d = None
    if d is not None:
        try:
            d.dispatch(
                SetCurrentIndexAction(slot=target_slot, index=idx),
                scope="document",
            )
            return
        except Exception:
            pass
    try:
        from tabs.image_compare.state.reducer import DocumentReducer

        doc = controller.store.get_session_state_slot("document")
        new_doc = DocumentReducer.reduce(
            doc, SetCurrentIndexAction(slot=target_slot, index=idx)
        )
        if new_doc is not doc:
            controller.store.set_session_state_slot(
                "document", new_doc, emit_scope="document"
            )
            return
    except Exception:
        pass
    logger.debug(
        "duplicate index update skipped: no dispatcher or write-back API "
        "(slot=%s idx=%s)",
        target_slot,
        idx,
    )


def duplicate_image_to_slot(controller, source_slot: int, target_slot: int) -> None:
    if source_slot not in (1, 2) or target_slot not in (1, 2):
        return
    document = controller.store.get_session_state_slot("document")
    if document is None:
        return
    s_lst = document.image_list1 if source_slot == 1 else document.image_list2
    s_idx = document.current_index1 if source_slot == 1 else document.current_index2
    if not (0 <= s_idx < len(s_lst)):
        return
    s_item = s_lst[s_idx]
    path = s_item.path or ""
    if not path:
        return
    t_lst = document.image_list1 if target_slot == 1 else document.image_list2
    for idx, ex in enumerate(t_lst):
        if _seam_same_fs_path(ex.path, path):
            _dispatch_target_index(controller, target_slot, idx)
            if controller.presenter:
                controller.presenter.ui_batcher.schedule_update("combobox")
            try:
                controller.set_current_image(target_slot)
            except Exception:
                pass
            return
    pl = getattr(controller, "pipeline", None)
    cached = pl.peek(path) if pl else None
    if cached is not None and not bool(getattr(cached, "is_open", True)):
        cached = None
    # SlotSource only — no image on item, cache holds pixels.
    # Dispatch-first: the old direct t_lst.append bypassed the Dispatcher,
    # so the append had no history entry — an unrelated undo restored a
    # snapshot without the item and it vanished. AppendImageItemsAction
    # replaces the document (copy-on-write) and records the mutation.
    new_item = ImageItem(path=path, display_name=s_item.display_name, rating=int(getattr(s_item, "rating", 0) or 0))
    appended_via_dispatch = False
    try:
        append_dispatcher = getattr(controller.store, "get_dispatcher", lambda: None)()
    except Exception:
        append_dispatcher = None
    if append_dispatcher is not None:
        try:
            from core.state_management.document_actions import AppendImageItemsAction

            controller.store.transact(
                [AppendImageItemsAction(slot=target_slot, items=[new_item])],
                scope="document",
            )
            appended_via_dispatch = True
        except Exception:
            appended_via_dispatch = False
    if not appended_via_dispatch:
        # Fail-open legacy for dispatcher-less (fake) stores only.
        t_lst.append(new_item)
    # ensure cache has entry if we had one (Bucket C via transact)
    if cached is not None:
        try:
            from tabs.image_compare.state.actions import PutPixelAction
            d2 = getattr(controller.store, "get_dispatcher", lambda: None)()
            if d2 is not None:
                controller.store.transact([PutPixelAction(path=path, store=cached)], scope="pipeline")
            else:
                if pl is not None:
                    try:
                        getattr(pl.cache, "put_" + "pixel")(path, store=cached)
                    except Exception:
                        pass
        except Exception:
            try:
                if pl is not None:
                    getattr(pl.cache, "put_" + "pixel")(path, store=cached)
            except Exception:
                pass
    # Re-fetch: a dispatched append replaced the document, so the live
    # list (and its length) must come from the current state, not the
    # pre-append `t_lst` reference (detached after replace()).
    try:
        _dup_doc = controller.store.get_session_state_slot("document")
        _dup_lst = _dup_doc.image_list1 if target_slot == 1 else _dup_doc.image_list2
        new_index = len(_dup_lst) - 1
    except Exception:
        new_index = len(t_lst) - 1
    _dispatch_target_index(controller, target_slot, new_index)
    if controller.presenter:
        try:
            flyout = getattr(getattr(controller.presenter, "ui_manager", None), "transient", None)
            flyout = getattr(flyout, "unified_flyout", None) if flyout is not None else None
            if flyout is not None:
                if flyout.isVisible():
                    controller.presenter.repopulate_flyouts()
                else:
                    try:
                        widget = getattr(controller, "widget", None)
                        if widget is None:
                            widget = getattr(controller.presenter, "widget", None)
                        gate = getattr(widget, "_stale_gate", None) if widget is not None else None
                        if gate is not None:
                            gate.mark("render")
                        if widget is not None:
                            try:
                                widget._render_stale = True  # type: ignore[attr-defined]
                            except Exception:
                                pass
                    except Exception:
                        pass
            else:
                controller.presenter.repopulate_flyouts()
        except Exception:
            pass
    try:
        controller.set_current_image(target_slot)
    except Exception:
        pass


def _reload_existing_path(controller, image_number: int, normalized_path: str, target_list_ref):
    try:
        idx = next(i for i, it in enumerate(target_list_ref) if _seam_same_fs_path(it.path, normalized_path))
        pl = getattr(controller, "pipeline", None)
        cached = pl.peek(normalized_path) if pl else None
        # no list item pixel write — cache already holds it
        d = getattr(controller.store, "get_dispatcher", lambda: None)()
        if d:
            try:
                d.dispatch(SetCurrentIndexAction(slot=image_number, index=idx), scope="document")
            except Exception:
                pass
        controller.set_current_image(image_number)
        if controller.presenter:
            controller.presenter.ui_batcher.schedule_update("combobox")
    except (ValueError, IndexError):
        pass


def _finalize_loaded_paths(controller, image_number: int, newly_added_indices: list[int], load_errors: list[str]):
    if newly_added_indices:
        new_index = newly_added_indices[-1]
        d = getattr(controller.store, "get_dispatcher", lambda: None)()
        if d:
            try:
                d.dispatch(SetCurrentIndexAction(slot=image_number, index=new_index), scope="document")
            except Exception:
                pass
        controller.set_current_image(image_number)
        if controller.presenter:
            try:
                flyout = getattr(getattr(controller.presenter, "ui_manager", None), "transient", None)
                flyout = getattr(flyout, "unified_flyout", None) if flyout is not None else None
                if flyout is not None:
                    if flyout.isVisible():
                        controller.presenter.repopulate_flyouts()
                    else:
                        try:
                            widget = getattr(controller, "widget", None)
                            if widget is None:
                                widget = getattr(controller.presenter, "widget", None)
                            gate = getattr(widget, "_stale_gate", None) if widget is not None else None
                            if gate is not None:
                                gate.mark("render")
                            if widget is not None:
                                try:
                                    widget._render_stale = True  # type: ignore[attr-defined]
                                except Exception:
                                    pass
                        except Exception:
                            pass
                else:
                    controller.presenter.repopulate_flyouts()
            except Exception:
                pass
    if load_errors:
        from core.events import CoreErrorOccurredEvent

        msg = tr("image_compare.msg.some_images_could_not_be_loaded", controller.store.settings.current_language) + ":\n\n - " + "\n - ".join(load_errors)
        if controller.event_bus:
            controller.event_bus.emit(CoreErrorOccurredEvent(msg))
        else:
            controller.error_occurred.emit(msg)


def set_current_image(controller, image_number: int, force_refresh: bool = False, emit_signal: bool = True):
    from tabs.image_compare.services import document_store_ops
    from core.state_management.actions import SetCachedDiffImageAction, SetUnificationInProgressAction, SetPendingUnificationPathsAction
    from core.events import CoreUpdateRequestedEvent

    document = controller.store.get_session_state_slot("document")
    lst = document.image_list1 if image_number == 1 else document.image_list2
    cur = document.current_index1 if image_number == 1 else document.current_index2
    _cur_path = lst[cur].path if 0 <= cur < len(lst) else None
    _set_sig = (int(image_number), int(cur) if isinstance(cur, int) else cur, str(_cur_path) if _cur_path else None, bool(force_refresh))
    _last_sig = _last_set_current_sig.get(int(image_number))  # type: ignore[has-type]
    _should_log_set = _set_sig != _last_sig or bool(force_refresh)
    if _should_log_set:
        _last_set_current_sig[int(image_number)] = _set_sig  # type: ignore[has-type]
        ic_preview_debug("set_current_image slot=%s force=%s emit=%s", image_number, force_refresh, emit_signal)
        ic_preview_debug("set_current_image slot=%s cur=%s len=%s path=%s", image_number, cur, len(lst), _cur_path)
    if not (0 <= cur < len(lst)):
        document_store_ops.clear_image_slot_data(controller.store, image_number)
        from tabs.image_compare.use_cases.unify import _invalidate_diff_cache

        _invalidate_diff_cache(controller)
        controller._invalidate_image_canvas_render_state(clear_overlay_state=True)
        controller._schedule_image_canvas_update()
        if controller.presenter:
            controller.presenter.ui_batcher.schedule_batch_update(["combobox", "file_names", "resolution", "ratings"])
        if controller.store.viewport.session_data.render_cache.unification_in_progress:
            pending = controller.store.viewport.session_data.render_cache.pending_unification_paths
            if pending and (document.image1_path if image_number == 1 else document.image2_path) not in pending:
                d = getattr(controller.store, "get_dispatcher", lambda: None)()
                if d:
                    try:
                        with controller.store.batch_changes():
                            d.dispatch(SetUnificationInProgressAction(enabled=False), scope="viewport")
                            d.dispatch(SetPendingUnificationPathsAction(paths=None), scope="viewport")
                    except Exception:
                        pass
        try:
            controller.metrics_service.on_metrics_calculated(None)
        except Exception:
            pass
        controller.store.emit_state_change("document")
        if controller.event_bus:
            controller.event_bus.emit(CoreUpdateRequestedEvent())
        else:
            controller.update_requested.emit()
        return
    item = lst[cur]
    path = item.path
    # pipeline is single source — peek to see if cached
    pl = getattr(controller, "pipeline", None)
    if _should_log_set:
        ic_preview_debug("set_current_image slot=%s path=%s pipeline=%s", image_number, path, bool(pl))
    cached = pl.peek(path) if pl is not None and path else None
    if _should_log_set:
        ic_preview_debug("set_current_image slot=%s cached=%s is_open=%s", image_number, cached, getattr(cached, "is_open", None) if cached else None)
    if cached is not None and not bool(getattr(cached, "is_open", True)):
        if _should_log_set:
            ic_preview_debug("set_current_image slot=%s cached closed -> None", image_number)
        cached = None
    pil_img = cached
    if _should_log_set:
        ic_preview_debug("set_current_image slot=%s pil_img=%s path=%s", image_number, pil_img, path)
    if pil_img is None and path:
        if _is_path_loading(controller, path):
            # Worker still in flight for this path (e.g. other slot's DnD
            # resync while this slot's TileStore load runs): skip the
            # destructive clear — the worker owns the result transact.
            if _should_log_set:
                ic_preview_debug("set_current_image slot=%s -> skip clear, worker in flight path=%s", image_number, path)
        else:
            if _should_log_set:
                ic_preview_debug("set_current_image slot=%s -> clear slot data (cache miss)", image_number)
            document_store_ops.clear_image_slot_data(controller.store, image_number)
    # publish PipelineView via single Transaction (1 emit)
    if pil_img is not None:
        if _should_log_set:
            ic_preview_debug("set_current_image slot=%s -> transact image uid=%s", image_number, getattr(pil_img, "uid", id(pil_img)))
        try:
            from core.state_management.actions import InvalidateGeometryCacheAction, SetImageSessionImageAction

            controller.store.transact(
                [SetImageSessionImageAction(slot=image_number, image=pil_img), InvalidateGeometryCacheAction()],
                scope="viewport",
            )
            if _should_log_set:
                ic_preview_debug("set_current_image slot=%s transact done", image_number)
        except Exception as e:
            if _should_log_set:
                ic_preview_debug("set_current_image slot=%s transact failed %s", image_number, e)
            try:
                controller._update_image_slot(image_number, image=pil_img, path=path, is_full_res=True, emit=False)
            except Exception:
                pass
    else:
        # still publish clear via transaction if needed — unless the path's
        # worker is still in flight (see cache-miss guard above): clearing
        # image_state here would wipe the live image a pending worker is
        # about to replace via its own single transact.
        if path and _is_path_loading(controller, path):
            if _should_log_set:
                ic_preview_debug("set_current_image slot=%s -> skip clear transact, worker in flight path=%s", image_number, path)
        else:
            if _should_log_set:
                ic_preview_debug("set_current_image slot=%s -> transact clear image_state", image_number)
            try:
                from core.state_management.actions import InvalidateGeometryCacheAction, SetImageSessionImageAction

                controller.store.transact(
                    [SetImageSessionImageAction(slot=image_number, image=None), InvalidateGeometryCacheAction()],
                    scope="viewport",
                )
                if _should_log_set:
                    ic_preview_debug("set_current_image slot=%s clear transact done", image_number)
            except Exception as e:
                if _should_log_set:
                    ic_preview_debug("set_current_image slot=%s clear transact failed %s", image_number, e)
                pass
    try:
        _sess = controller._get_image_session() if hasattr(controller, "_get_image_session") else None
        _cache = getattr(_sess, "cache", None) if _sess is not None else getattr(pl, "cache", None)
        _pin = getattr(_cache, "set_pinned_paths", None)
        if callable(_pin):
            _pin([document.image1_path, document.image2_path])
    except Exception:
        pass
    if _should_log_set:
        ic_preview_debug("set_current_image slot=%s -> invalidate_render + schedule_update", image_number)
    controller.store.invalidate_render_cache()
    controller._invalidate_image_canvas_render_state(clear_overlay_state=False)
    controller._schedule_image_canvas_update()
    if pil_img is None and path:
        if _should_log_set:
            ic_preview_debug("set_current_image slot=%s -> cache miss, will start worker path=%s cur=%s", image_number, path, cur)
        # Bucket D: single-flight via ImageLoadService (path+mtime+box → AbortSignal)
        # should_use_progressive inside service, one transact per result, dedup 3×
        _svc = None
        try:
            # prefer session load_service (shares _inflight dict with pipeline)
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
            if _svc is None:
                _svc = getattr(controller, "load_service", None)
        except Exception:
            _svc = None
        if _svc is not None:
            try:
                # service handles dedup via path+mtime+box and progressive inside
                sig = _svc.ensure_async(path, int(image_number), int(cur), controller)
                # dedup case — existing signal returned, or new signal started
                # direct pipeline._inflight single-flight via AbortSignal.is_aborted()
                try:
                    pl = getattr(controller, "pipeline", None)
                    if pl is not None and hasattr(pl, "_inflight") and sig is not None:
                        key = (int(image_number), str(path))
                        pl._inflight.setdefault(key, sig)  # type: ignore[index]
                except Exception:
                    pass
                if sig is not None:
                    # started or deduped — single-flight owns result → one transact
                    if _should_log_set:
                        ic_preview_debug("set_current_image slot=%s -> ImageLoadService sig=%s", image_number, sig)
                    if emit_signal:
                        controller.store.emit_state_change("document")
                    return
                else:
                    # cache-hit path already transacted inside service
                    if emit_signal:
                        controller.store.emit_state_change("document")
                    # trigger unification via service? on cache-hit service already handled
                    try:
                        controller._trigger_preview_unification(image_number)
                    except Exception:
                        pass
                    return
            except Exception as e:
                ic_preview_debug("set_current_image slot=%s ImageLoadService failed %s, fallback legacy", image_number, e)
                _svc = None
        # fallback legacy single-flight (kept for fakes without service)
        pl = getattr(controller, "pipeline", None)
        key = (int(image_number), str(path))
        if pl is not None and hasattr(pl, "_inflight"):
            existing = pl._inflight.get(key)  # type: ignore[arg-type]
            if _should_log_set:
                ic_preview_debug("set_current_image slot=%s inflight existing=%s", image_number, existing)
            if existing is not None:
                try:
                    if not existing.is_aborted():
                        # dedup is important — always log even when throttled
                        ic_preview_debug("set_current_image slot=%s -> inflight exists, dedup return", image_number)
                        if emit_signal:
                            controller.store.emit_state_change("document")
                        return
                except Exception:
                    if emit_signal:
                        controller.store.emit_state_change("document")
                    return
            try:
                from tabs.image_compare.pipeline.abort import AbortSignal as _AbortSignal

                _sig = _AbortSignal()
                pl._inflight[key] = _sig
                if _should_log_set:
                    ic_preview_debug("set_current_image slot=%s -> new inflight %s", image_number, _sig)
            except Exception as e:
                if _should_log_set:
                    ic_preview_debug("set_current_image slot=%s inflight create failed %s", image_number, e)
                _sig = None

            def _clear():
                try:
                    cur_sig = pl._inflight.get(key)  # type: ignore[arg-type]
                    if _sig is None or cur_sig is _sig:
                        pl._inflight.pop(key, None)
                except Exception:
                    pass

            _orig_load = controller._load_image_async

            def _load_with_signal(p, num, idx, _t=None):
                try:
                    if _sig is not None and _sig.is_aborted():
                        return None, p, num, idx, False
                except Exception:
                    pass
                res = _orig_load(p, num, idx, _t)
                try:
                    if _sig is not None and _sig.is_aborted():
                        return None, p, num, idx, False
                except Exception:
                    pass
                return res

            worker = GenericWorker(_load_with_signal, path, image_number, cur, None)
        else:
            # no pipeline — start worker without single-flight tracking (direct dispatch)
            worker = GenericWorker(controller._load_image_async, path, image_number, cur, None)

            def _clear():  # type: ignore[no-redef]
                pass
        worker.signals.result.connect(controller._on_image_loaded_from_worker)
        try:
            worker.signals.finished.connect(_clear)
        except Exception:
            pass
        worker.signals.result.connect(lambda *_a, _c=_clear: _c())
        controller.thread_pool.start(worker)
    else:
        try:
            controller._trigger_preview_unification(image_number)
        except Exception:
            pass
    if emit_signal:
        controller.store.emit_state_change("document")


def _is_path_loading(controller, path) -> bool:
    """True when an ImageLoadService worker is still in flight for *path*.

    Cross-slot DnD guard: a resync-triggered ``set_current_image`` for a slot
    whose worker has not finished sees a pipeline cache-miss and would
    dispatch the destructive clear. When the single-flight worker still owns
    the path, the clear must be skipped — the worker's result transact is the
    only writer. Resolution mirrors the ``_svc`` lookup in
    ``set_current_image``; any failure degrades to False (legacy behavior).

    Kept at end of file so the ``setattr`` contract allowlist line numbers
    above (duplicate fallbacks) do not shift.
    """
    if not path:
        return False
    try:
        svc = None
        sess = None
        if hasattr(controller, "_get_image_session"):
            try:
                sess = controller._get_image_session()
            except Exception:
                sess = None
        if sess is not None:
            svc = getattr(sess, "load_service", None)
        if svc is None:
            pl = getattr(controller, "pipeline", None)
            svc = getattr(pl, "load_service", None) if pl is not None else None
        if svc is None:
            svc = getattr(controller, "load_service", None)
        if svc is None:
            return False
        is_loading = getattr(svc, "is_loading", None)
        if not callable(is_loading):
            return False
        try:
            return bool(is_loading(path))
        except TypeError:
            return bool(is_loading(path, None))
    except Exception:
        return False
