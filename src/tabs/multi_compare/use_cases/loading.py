"""Image loading, pyramid build, and "loading full version of image" toast
for the Multi Compare tab -- split out of ``MultiCompareController`` to keep
that class down to wiring/composition, mirroring image_compare's own
``use_cases/loading.py`` split. Every function here takes the controller as
its first argument and delegates toast/pyramid lifecycle to the tab's
``LoadingToastCoordinator`` / ``PyramidBuildCoordinator`` (owned by the
controller, which aliases their state dicts); no direct state writes remain.
Audit-Meta: pattern=thin-owner reason="Controller delegate fan-out: toast/pyramid/full-res stages share one module; batch planning stays beside the primitives it stages"
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.events import CoreErrorOccurredEvent
from tabs._shared.loading_pipeline import (
    _same_fs_path,
    get_or_load_preview,
    start_pyramid_build as _seam_start_pyramid_build,
)
from tabs._shared.user_errors import emit_user_error, load_image_error
from core.log_names import APP_LOGGER_NAME

logger = logging.getLogger(APP_LOGGER_NAME)


def _format_worker_error(err) -> str:
    if isinstance(err, tuple) and len(err) >= 2:
        return str(err[1])
    return str(err)


def _resolve_mc_event_bus(controller):
    """Shared EventBus resolution for MC load-error reporting (IC parity).

    Prefers the TabContext bus; falls back to the main window presenter's
    bus (kept for controllers without TabContext, e.g. tests). None when
    neither is reachable — callers stay silent then, never raise.
    """
    try:
        event_bus = getattr(controller.context, "event_bus", None) if getattr(controller, "context", None) else None
        if event_bus is not None:
            return event_bus
        main_window = getattr(controller.context, "main_window", None) if getattr(controller, "context", None) else None
        presenter = getattr(main_window, "presenter", None) if main_window else None
        fallback_bus = getattr(presenter, "event_bus", None) if presenter else None
        return fallback_bus
    except Exception:
        return None


def _emit_mc_load_error(controller, path: Path | str, err) -> None:
    """Surface a failed MC load via the shared EventBus plumbing (IC parity).

    Mirrors ``image_compare._session_controller._load_image_async`` /
    ``_on_full_resolution_error`` — a dropped corrupt file must not silently
    vanish; it emits ``CoreErrorOccurredEvent`` so MainController shows its
    warning dialog (``error_occurred`` → ``AppMessageDialog.warning`` in
    ``ui/presenters/main_window/actions.py`` — a dialog by policy, not a
    toast; see plan §3.3).
    """
    try:
        # Resolve language via controller/store or tab context; builder owns wording.
        lang = None
        try:
            lang = controller.store.settings.current_language
        except Exception:
            lang = None
        if lang is None:
            try:
                ctx = getattr(controller, "context", None)
                store = getattr(ctx, "store", None) if ctx is not None else getattr(controller, "store", None)
                lang = getattr(getattr(store, "settings", None), "current_language", None)
            except Exception:
                lang = None
        message = load_image_error(language=lang, path=path, error=err)
        emit_user_error(controller, message)
    except Exception:
        logger.exception("Failed to emit MC load error event for %s", path)


# Single source now in tabs._shared.loading_toast (B2 dedup).
from tabs._shared.loading_toast import DECODE_DONE_PROGRESS, PYRAMID_START_PROGRESS  # noqa: F401


def get_toast_manager(controller):
    main_window = getattr(controller.context, "main_window", None) if controller.context else None
    toast_manager = getattr(main_window, "toast_manager", None)
    if toast_manager is None:
        logger.debug(
            "[FullImageLoad] no toast_manager available (context=%r)", controller.context
        )
    return toast_manager


def show_loading_toast(controller, slot_id: int) -> None:
    coord = getattr(controller, "_loading_toast_coordinator", None)
    if coord is not None:
        coord.show(slot_id)
        return


def set_loading_toast_progress(controller, slot_id: int, percent: int) -> None:
    coord = getattr(controller, "_loading_toast_coordinator", None)
    if coord is not None:
        coord.set_progress(slot_id, percent)
        return


def mark_full_res_ready(controller, slot_id: int) -> None:
    coord = getattr(controller, "_loading_toast_coordinator", None)
    if coord is not None:
        coord.mark_full_res_ready(slot_id)
        return


def bump_loading_toast_pyramid_started(controller, slot_id: int) -> None:
    coord = getattr(controller, "_loading_toast_coordinator", None)
    if coord is not None:
        coord.bump_pyramid_started(slot_id)
        return


def finish_loading_toast(controller, slot_id: int) -> None:
    coord = getattr(controller, "_loading_toast_coordinator", None)
    if coord is not None:
        coord.finish(slot_id)
        return


def dismiss_loading_toast(controller, slot_id: int) -> None:
    """Closes a slot's loading toast without the "done" success banner --
    used when the load fails or the slot vanished mid-load, as opposed to
    ``finish_loading_toast``'s success path."""
    coord = getattr(controller, "_loading_toast_coordinator", None)
    if coord is not None:
        coord.dismiss(slot_id)
        return


def _get_crop_service(controller):
    """DI helper — mirror SessionController._get_crop_service."""
    try:
        getter = getattr(controller, "_get_crop_service", None)
        if callable(getter):
            return getter()
    except Exception:
        pass
    try:
        svc = getattr(controller, "_crop_service", None)
        if svc is not None:
            # check setting manually if controller has no getter
            store = getattr(controller, "store", None)
            if store is not None:
                should = getattr(getattr(store, "settings", None), "auto_crop_black_borders", False)
                if not should:
                    return None
            return svc
    except Exception:
        pass
    return None


def read_image(controller, path: Path, *, slot_id: int | None = None, start_pyramid: bool = True):
    try:
        from shared.image_processing.pixel_cache_loader import load_pixel_store

        crop_service = _get_crop_service(controller)
        _emb_mc = None
        try:
            _emb_mc = getattr(getattr(controller, "store", None), "get_session_state_slot", lambda *_: None)("pipeline")
            # pipeline state vs cache instance fallback
            from shared.image_processing import embedded_pixel_cache as _emb_mod

            _emb_mc = _emb_mod  # host-owned injection from tab context
        except Exception:
            _emb_mc = None
        store = load_pixel_store(path, crop_service=crop_service, embedded_cache=_emb_mc)
        if start_pyramid:
            # Demand kick (GIMP-style): only k_fit for the fit view, with
            # eager fallback when no view geometry is reachable.
            demand_pyramid_for_slot(controller, store, slot_id=slot_id)
        return store
    except Exception as e:
        logger.error("Failed to load %s: %s", path, e, exc_info=True)
        if slot_id is not None:
            try:
                dismiss_loading_toast(controller, slot_id)
            except Exception:
                pass
        _emit_mc_load_error(controller, path, e)
        return None


def start_pyramid_build(controller, store, *, slot_id: int | None = None) -> None:
    """Kicks off background mipmap-pyramid construction for a slot's
    full-res store, mirroring image_compare's
    ``_session_controller._start_pyramid_builds``. Without this, a slot
    displayed below native resolution samples the unmipmapped native
    texture directly, which aliases; the pyramid lets the per-frame LOD
    selector in ``BaseImagesPass`` pick a downsampled level instead
    (see ``docs/dev/rendering/...`` LOD selection, ``shared/rendering/lod.py``).

    ``slot_id``, when given, drives the "loading full version of image"
    toast (docs/dev/KNOWN_BUGS.md same-slot-swap SSIM follow-up) through
    its pyramid-build stage -- every early return below that skips the
    actual build (nothing to build, no thread pool, already in flight
    with no new work) also finishes that slot's toast instead of leaving
    it stuck at "pyramid started" forever.

    W2 adapter: the build trigger goes through the shared seam
    ``tabs._shared.loading_pipeline.start_pyramid_build`` with a tab-local
    hook closure carrying the coordinator + the MC abort predicate
    (``pyramid.valid``) — toast wiring stays in the adapter, the seam owns
    the trigger shape.
    """
    coord = getattr(controller, "_pyramid_coordinator", None)
    if coord is None:
        return
    # MC abort predicate: pyramid.valid
    from engine.store.pyramid_registry import ensure_pyramid as _ensure

    _pyr = _ensure(store)

    def _should_abort(_p=_pyr):
        return not getattr(_p, "valid", True) if _p is not None else False

    if store is None:
        # No pyramid possible: the coordinator finishes the toast itself
        # (legacy path — the seam is a fail-open no-op for None stores and
        # would never reach the hook, leaving the toast stuck).
        coord.start_build(store, slot_id=slot_id, should_abort=_should_abort)
        return

    def _hook(s, sid):
        coord.start_build(s, slot_id=sid, should_abort=_should_abort)

    _seam_start_pyramid_build(store, slot_id, _hook)


def _demand_abort_for_slot(controller, slot_id, path, pyramid=None):
    """Abort predicate for demand builds (E2 ``should_abort`` shape).

    Aborts when the pyramid went stale (base closed / re-unified) or the
    slot vanished / was recycled mid-build (``max+1`` id reuse with a
    different path). A missing slot list reads as not-aborted (headless
    fakes) — never raises.
    """

    def _should_abort(_p=pyramid, _sid=slot_id, _path=path):
        if _p is not None and not getattr(_p, "valid", True):
            return True
        if _sid is None:
            return False
        try:
            slots = list(controller.widget.state.slots)
        except Exception:
            return False
        slot = next((s for s in slots if getattr(s, "id", None) == _sid), None)
        if slot is None:
            return True  # slot removed mid-build → abort, never rebuild
        try:
            return not _same_fs_path(getattr(slot, "path", None), _path)
        except Exception:
            return False

    return _should_abort


def _rect_size(rect):
    """``(w, h)`` from a QRect-like (``width()`` methods) or a ``(w, h)`` tuple."""
    try:
        w, h = rect[0], rect[1]
        return float(w), float(h)
    except Exception:
        pass
    try:
        width = getattr(rect, "width", None)
        height = getattr(rect, "height", None)
        w = width() if callable(width) else width
        h = height() if callable(height) else height
        return float(w), float(h)
    except Exception:
        return None


def _fit_dest_scale(controller, store, slot_id, *, zoom=None, rect=None, fit=None):
    """Fit-view dest scale (display-px per level-0 px) or ``None``.

    Mirrors ``scene.passes.residency.lod_key_and_source`` (``disp = rect *
    fit * zoom``, ``scale = max(disp_w/src_w, disp_h/src_h)``) so the demand
    target and the sampler can never disagree. ``rect``/``fit`` come from
    the zoom caller when it already has them (wheel tick); otherwise the
    canvas leaf is looked up (arrival path, keyboard zoom). ``None`` when
    no view geometry is reachable — callers fall back, never raise.
    """
    try:
        size = getattr(store, "size", None)
        size = size() if callable(size) else size
        src_w, src_h = float(size[0]), float(size[1])
    except Exception:
        return None
    if not src_w > 0 or not src_h > 0:
        return None
    if zoom is None:
        try:
            zoom = float(getattr(controller.widget.state, "zoom", 1.0))
        except Exception:
            zoom = 1.0
    try:
        zoom = float(zoom)
    except Exception:
        return None
    if not zoom > 0:
        return None
    rect_w = rect_h = None
    if rect is not None:
        try:
            rect_w, rect_h = _rect_size(rect)
        except Exception:
            rect_w = rect_h = None
    if not rect_w or not rect_h or rect_w <= 0 or rect_h <= 0:
        rect_w = rect_h = None
        try:
            canvas = getattr(getattr(controller, "widget", None), "canvas", None)
            leaf_rects = canvas._leaf_rects() if canvas is not None else None
        except Exception:
            leaf_rects = None
        try:
            for leaf, rect in leaf_rects or ():
                if getattr(leaf, "slot_id", None) == slot_id:
                    sized = _rect_size(rect)
                    if sized is not None and sized[0] > 0 and sized[1] > 0:
                        rect_w, rect_h = sized
                    break
        except Exception:
            rect_w = rect_h = None
    if rect_w is None or rect_h is None:
        return None
    fit_x = fit_y = None
    if fit is not None:
        try:
            fit_x, fit_y = float(fit[0]), float(fit[1])
        except Exception:
            fit_x = fit_y = None
    if not fit_x or not fit_y or fit_x <= 0 or fit_y <= 0:
        # Aspect-fit inline (same math as canvas.interaction.fit_scale_for —
        # kept local so use_cases never imports canvas).
        try:
            img_ar = src_w / src_h
            cell_ar = rect_w / rect_h
            if img_ar > cell_ar:
                fit_x, fit_y = 1.0, cell_ar / img_ar
            else:
                fit_x, fit_y = img_ar / cell_ar, 1.0
        except Exception:
            return None
    try:
        return max(
            rect_w * fit_x * zoom / src_w,
            rect_h * fit_y * zoom / src_h,
        )
    except Exception:
        return None


def demand_target_for_slot(controller, store, slot_id, *, zoom=None, rect=None, fit=None):
    """Ideal pyramid level for the slot's current view (E1 ``select_level``).

    Shared LOD all the way down: ``select_level`` over the full
    ``estimate_total_levels`` count with the fit-view dest scale — the same
    inputs the residency sampler uses, so the demand target and the sampler
    agree about which level is which. ``None`` when the target is
    uncomputable (no geometry / bad store) — callers fall back, never raise.
    """
    try:
        from engine.cache.lod import demand_level
        from engine.store.pyramid_pixel_store import estimate_total_levels
    except Exception:
        return None
    try:
        size = getattr(store, "size", None)
        size = size() if callable(size) else size
        w, h = int(size[0]), int(size[1])
    except Exception:
        return None
    if w <= 0 or h <= 0:
        return None
    try:
        scale = _fit_dest_scale(
            controller, store, slot_id, zoom=zoom, rect=rect, fit=fit
        )
    except Exception:
        return None
    if scale is None:
        return None
    try:
        return int(demand_level(float(scale), estimate_total_levels(w, h)))
    except Exception:
        return None


def demand_pyramid_for_slot(controller, store, *, slot_id=None) -> bool:
    """GIMP-style demand kick for a full-res arrival (replaces the eager kick).

    Demands only ``k_fit`` (``demand_target_for_slot``) through the shared
    E2 coordinator — bounded worker, same toast/abort routing as the eager
    path, built levels never rebuild. Falls back to the legacy eager
    ``start_pyramid_build`` when no view geometry is reachable (headless /
    pre-layout slot) so no arrival ever loses its pyramid. Never raises.

    Returns True iff a demand was kicked or already satisfied; False when
    the eager fallback ran or nothing could start.
    """
    coord = getattr(controller, "_pyramid_coordinator", None)
    if coord is None or store is None:
        return False
    try:
        k = demand_target_for_slot(controller, store, slot_id)
    except Exception:
        k = None
    if k is None:
        try:
            start_pyramid_build(controller, store, slot_id=slot_id)
        except Exception:
            pass
        return False
    path = None
    if slot_id is not None:
        try:
            slot = next(
                (s for s in controller.widget.state.slots if s.id == slot_id),
                None,
            )
            path = getattr(slot, "path", None) if slot is not None else None
        except Exception:
            path = None
    try:
        from engine.store.pyramid_registry import ensure_pyramid as _ensure

        pyr = _ensure(store)
    except Exception:
        pyr = None
    try:
        return bool(
            coord.demand_level(
                store,
                int(k),
                slot_id=slot_id,
                should_abort=_demand_abort_for_slot(
                    controller, slot_id, path, pyr
                ),
            )
        )
    except Exception:
        return False


def demand_for_zoom(controller, slot_id, zoom, *, rect=None, fit=None) -> bool:
    """Zoom demand entry (thin, canvas calls via ``demand_for_zoom_widget``).

    Demands the newly needed level for ``slot_id`` at ``zoom`` through the
    E2 coordinator — same toast/abort routing as arrivals; built levels
    never rebuild (the coordinator short-circuits them with no worker).
    ``slot_id=None`` fans out over every slot holding a pixel store
    (keyboard zoom has no picked slot). Preview-only slots (no pixel tier)
    are skipped. Never raises; True if any demand kicked or was satisfied.
    """
    coord = getattr(controller, "_pyramid_coordinator", None)
    if coord is None:
        return False
    try:
        slots = list(controller.widget.state.slots)
    except Exception:
        return False
    if slot_id is not None:
        slots = [s for s in slots if getattr(s, "id", None) == slot_id]
        if not slots:
            return False
    cache = getattr(controller, "pixel_cache", None)
    if cache is None:
        return False
    kicked = False
    for slot in slots:
        try:
            path = getattr(slot, "path", None)
            store = cache.get_pixel(path)
        except Exception:
            continue
        if store is None:
            continue  # preview-only slot: nothing to demand
        try:
            k = demand_target_for_slot(
                controller, store, getattr(slot, "id", None),
                zoom=zoom, rect=rect, fit=fit,
            )
        except Exception:
            continue
        if k is None:
            continue  # zoom path never kicks eager builds — fail-open skip
        try:
            from engine.store.pyramid_registry import ensure_pyramid as _ensure

            pyr = _ensure(store)
        except Exception:
            pyr = None
        try:
            if coord.demand_level(
                store,
                int(k),
                slot_id=getattr(slot, "id", None),
                should_abort=_demand_abort_for_slot(
                    controller, getattr(slot, "id", None), path, pyr
                ),
            ):
                kicked = True
        except Exception:
            continue
    return kicked


def demand_for_zoom_widget(widget, slot_id, zoom, *, rect=None, fit=None) -> bool:
    """Canvas-hook entry: resolves the controller, then :func:`demand_for_zoom`.

    The zoom handlers call this single function (no canvas restructuring —
    plain data in, bool out). The controller is resolved through the
    canvas-parent backref (same chain as
    ``scene.passes.residency.controller_for_canvas``). Never raises.
    """
    try:
        from tabs.multi_compare.scene.passes import residency as _residency

        controller = _residency.controller_for_canvas(widget)
    except Exception:
        return False
    if controller is None:
        return False
    try:
        return bool(
            demand_for_zoom(controller, slot_id, zoom, rect=rect, fit=fit)
        )
    except Exception:
        return False


def on_pyramid_level_ready(controller, payload=None) -> None:
    coord = getattr(controller, "_pyramid_coordinator", None)
    if coord is not None:
        # coordinator handles canvas refresh via injected callback, but MC's
        # canvas is on widget; ensure refresh if coordinator didn't inject.
        coord.on_level_ready(payload)
        # keep legacy canvas refresh for fakes without injected callback
        try:
            canvas = getattr(controller.widget, "canvas", None)
            if canvas is not None:
                canvas.request_view_update()
        except Exception:
            pass
        return


def load_full_resolution_async(
    controller, path: Path, slot_id: int, *, keep_slot_on_error: bool = False
) -> None:
    """Full-res second stage (A2): per-slot workers via ``preview_decode``'s bounded FIFO."""
    thread_pool = getattr(controller.context, "thread_pool", None) if controller.context else None
    if thread_pool is None:
        store = read_image(controller, path, start_pyramid=False)
        if store is None:
            # read_image already reported via _emit_mc_load_error — just
            # drop the toast so the failure surfaces exactly once.
            dismiss_loading_toast(controller, slot_id)
            return
        apply_full_resolution(
            controller, slot_id, path, store, keep_slot_on_error=keep_slot_on_error
        )
        return

    from tabs.multi_compare.use_cases import preview_decode as _preview

    _preview.queue_full_resolution(
        controller, path, slot_id, keep_slot_on_error=keep_slot_on_error
    )


def on_full_resolution_error(
    controller, path: Path, slot_id: int, err, *, keep_slot_on_error: bool = False
) -> None:
    """Full-res worker failed: orphan/recycled stays silent, live slot reports via bus."""
    from tabs.multi_compare.pipeline.cache import resolve_slot_source

    slot = None
    try:
        slots = list(controller.widget.state.slots)
    except Exception:
        slots = None
    if slots is not None:
        slot = next((s for s in slots if s.id == slot_id), None)
        if slot is None:
            # Orphan (slot removed mid-load, e.g. user-cancelled): silent
            # dismiss, no error-toast — the load was abandoned on purpose.
            dismiss_loading_toast(controller, slot_id)
            return
        try:
            same = _same_fs_path(slot.path, path)
        except Exception:
            same = False
        if not same:
            # Id recycled by a newer slot: its toast is live, don't touch it.
            return
    logger.error("Failed to load full resolution for %s: %s", path, err, exc_info=True)
    has_tier = False
    if slots is not None and slot is not None:
        try:
            has_tier = (
                resolve_slot_source(getattr(controller, "pixel_cache", None), slot)
                is not None
            )
        except Exception:
            has_tier = False
    if not has_tier and not keep_slot_on_error:
        # Imageless (preview-miss path): sync-UX parity — drop the
        # pre-created slot instead of leaving an imageless leaf (blank
        # hole). A slot holding its preview tier keeps it; only its toast
        # is dismissed. B1 restore fills (keep_slot_on_error) keep the
        # restored slot regardless.
        from tabs.multi_compare.scene import actions as mc_actions

        try:
            controller.widget.store.dispatch(mc_actions.remove_slot(slot_id))
        except Exception:
            logger.exception("Failed to remove failed-load slot %s", slot_id)
    dismiss_loading_toast(controller, slot_id)
    _emit_mc_load_error(controller, path, err)


def apply_full_resolution(
    controller, slot_id: int, path: Path, store, *, keep_slot_on_error: bool = False
) -> None:
    """Full-res tier arrival: cache the store, note it, build its pyramid.

    B1: the store lands in the session cache (sole owner — eviction close
    can never break an undo snapshot, which holds paths only), then a
    ``note_slot_pixels`` dispatch bumps the slot revision so subscribers
    rebuild. A same-path sibling's cached store wins on collision (one
    shared object, no duplicate decode); the displaced worker-fresh store
    is closed. Ownerless stores (orphan/recycled slot) are closed unless
    they are the cache's live entry (shared with a live slot).

    Preview-first invariant: before the full tier lands, the preview tier
    for ``path`` is demand-filled via the shared seam
    ``get_or_load_preview`` (bounded ~1024px, fail-open) — peek first so a
    same-path sibling's cached preview is never re-decoded; a fill miss
    (``None``) preserves today's behavior exactly.
    """
    _ = keep_slot_on_error  # full tier never drops the slot (preview stage owns that call)
    if store is None:
        # Full-res decode yielded no tier (worker abort / empty decode):
        # orphan/recycled stays silent (toast belongs to nobody / the newer
        # generation); a live slot reports via the shared helper like every
        # other load failure instead of just dismissing its toast.
        try:
            _slots = list(controller.widget.state.slots)
        except Exception:
            _slots = []
        _slot = next((s for s in _slots if s.id == slot_id), None)
        if _slot is None:
            dismiss_loading_toast(controller, slot_id)
            return
        try:
            _same = _same_fs_path(_slot.path, path)
        except Exception:
            _same = False
        if not _same:
            return
        logger.error("Failed to load full resolution for %s: decode returned no data", path)
        dismiss_loading_toast(controller, slot_id)
        _emit_mc_load_error(controller, path, RuntimeError("decode returned no image data"))
        return
    slot = next((s for s in controller.widget.state.slots if s.id == slot_id), None)
    if slot is None:
        # Slot was removed while the full-res decode was in flight —
        # dismiss (never finish: an orphan must not show toast-done).
        from tabs.multi_compare.use_cases.preview_decode import (
            _close_unless_cached as _close_shared,
        )

        _close_shared(controller, path, store)
        dismiss_loading_toast(controller, slot_id)
        return
    if not _same_fs_path(slot.path, path):
        # Id recycled (``max+1``): the newer generation owns id+toast —
        # touch neither. Close this ownerless store unless shared.
        # Normalized compare (P8): str-vs-Path/"./" variants must not read
        # as stale and orphan a good decode.
        from tabs.multi_compare.use_cases.preview_decode import (
            _close_unless_cached as _close_shared,
        )

        _close_shared(controller, path, store)
        return
    # Preview-first: ensure the preview tier exists before the full note.
    # Peek first (same-path sibling may already hold it — never
    # double-decode); seam fill is bounded + fail-open, so a miss keeps
    # today's behavior exactly.
    try:
        _preview_cache = getattr(controller, "pixel_cache", None)
        _preview_hit = None
        if _preview_cache is not None:
            try:
                _preview_hit = _preview_cache.get_preview(path)
            except Exception:
                _preview_hit = None
        try:
            _preview_usable = _preview_hit is not None and not _preview_hit.isNull()
        except Exception:
            _preview_usable = _preview_hit is not None
        if not _preview_usable:
            try:
                get_or_load_preview(_preview_cache, path)
            except Exception:
                pass
    except Exception:
        pass
    try:
        cache = getattr(controller, "pixel_cache", None)
        if cache is not None:
            try:
                existing = cache.get_pixel(path)
            except Exception:
                existing = None
            if existing is not None and existing is not store:
                # Same-path sibling already cached (double-add dedup):
                # share it, close the duplicate decode.
                from engine.store.shared_pixel_registry import release_store

                try:
                    release_store(store)
                except Exception:
                    pass
                store = existing
            else:
                cache.put_pixel(path, store)
    except Exception:
        logger.exception("mc: pixel cache put failed for slot %s", slot_id)
    mark_full_res_ready(controller, slot_id)
    from tabs.multi_compare.scene import actions as mc_actions

    controller.widget.store.dispatch(mc_actions.note_slot_pixels(slot_id, "full"))
    # Demand kick (GIMP-style): only k_fit for the current view — the
    # resolve pyramid gate keeps holding the preview until the needed tier
    # is ready, so this never flips the display early.
    demand_pyramid_for_slot(controller, store, slot_id=slot_id)


def load_external_paths(controller, paths, side=None) -> int:
    """Chrome/carry/paste drops without a canvas position (P3A).

    Direct-load like IC (``image_compare.use_cases.drag_drop.handle_drop``):
    auto-place each file through the P2 async path — imageless slot +
    loading toast synchronously, bounded preview in a ``GenericWorker``
    (``load_preview_async``), full-res second stage. No
    ``begin_pending_paste`` arming: nothing waits for a canvas click, so
    there is no armed highlight and ``Esc`` has nothing to cancel for this
    path. The ``(None, False)`` target falls back to auto placement inside
    ``add_image_at`` (empty canvas → root, otherwise largest-leaf split),
    so window-chrome ``slot`` hints and carry drops with no position both
    map to append-like placement. Internal-drag Move stays in
    ``ui/drag_drop`` and is untouched by this path.

    ``side`` is the placement axis a caller may name when it has no canvas
    position but does have intent (the control surface's
    ``open_folder(side=...)``): it drives the split axis of the files after
    the first, so a multi-file drop composes a row/column instead of the
    auto-picked one. ``None`` (every mouse drive) keeps auto placement
    exactly as before.

    Returns the number of slots created synchronously.
    """
    from shared.image_extensions import is_loadable_image_path

    valid: list[Path] = []
    for raw in paths or []:
        path = raw if isinstance(raw, Path) else Path(raw)
        if not is_loadable_image_path(path):
            logger.debug("load_external_paths: skipped non-image/missing file %s", path)
            continue
        valid.append(path)
    if not valid:
        return 0
    before = len(controller.widget.state.slots)
    on_images_dropped(controller, valid, (None, False), side)
    return len(controller.widget.state.slots) - before


def resolve_auto_triple(widget, scratch):
    """Auto-placement triple ``(target_path, side, target_root)`` without dispatch.

    Read-only replica of ``placement.add_image_auto`` target resolution
    (``placement.py`` itself is untouched — A1 owns it): empty tree →
    root, otherwise the widget's auto target. A canvas that cannot answer
    (headless fakes) falls back to the deterministic ``((), "right")``
    A1 will converge on, instead of raising mid-drop.
    """
    if scratch.root is None:
        return None, None, True
    try:
        target_path, side = widget._pick_auto_target()
        return target_path, side, False
    except Exception:
        return (), "right", False


def on_images_dropped(controller, paths: list, target, side) -> None:
    """target: tuple (target_path_or_None, target_root_bool); side: 'left'/'right'/...

    P2: only placement + imageless slot creation run on the GUI thread —
    no decode here (a preview-miss used to be a full synchronous
    ``read_image`` stall, measured 381ms drop→finish). Each slot's
    preview decodes in a ``GenericWorker`` (``load_preview_async``) and
    fills the slot on result, with full-res as the second stage.
    Internal-drag Move semantics live in ``ui/drag_drop`` and are
    untouched by this path.

    A3: the N per-file ``add_image_at/auto`` dispatches are planned first
    against a scratch state through the pure ``scene.store.reduce`` (same
    chain rule — file 0 at the drop target, later files beside the
    previously added slot, auto fallback when its path is gone) and
    committed with one ``store.transact`` → 1 dispatch / 1 emit. Toast +
    preview workers stay per-slot (not store dispatches) and run after the
    single commit.
    """
    from tabs.multi_compare.models import find_path
    from tabs.multi_compare.scene import actions as mc_actions
    from tabs.multi_compare.scene.store import reduce as mc_reduce
    from tabs.multi_compare.use_cases import preview_decode as _preview

    target_path, target_root = (
        target if isinstance(target, tuple) else (None, False)
    )

    widget = controller.widget
    scratch = widget.state
    built: list = []
    planned: list[tuple[int, Path]] = []
    last_added: int | None = None
    for i, raw_path in enumerate(paths):
        path = Path(raw_path) if not isinstance(raw_path, Path) else raw_path
        if i == 0:
            eff_path, eff_side, eff_root = target_path, side, target_root
            if (
                not eff_root
                and (eff_path is None or eff_side is None)
                and scratch.root is not None
            ):
                eff_path, eff_side, eff_root = resolve_auto_triple(widget, scratch)
            else:
                eff_root = bool(eff_root or scratch.root is None)
            action = mc_actions.add_slot(
                path=path,
                label=path.stem,
                target_path=eff_path,
                side=eff_side,
                target_root=eff_root,
            )
        else:
            next_side = "right" if side in ("left", "right") else "bottom"
            next_path: tuple[int, ...] | None = None
            if last_added is not None:
                found = find_path(scratch.root, last_added)
                next_path = tuple(found) if found is not None else None
            if next_path is None:
                auto_path, auto_side, auto_root = resolve_auto_triple(widget, scratch)
                action = mc_actions.add_slot(
                    path=path,
                    label=path.stem,
                    target_path=auto_path,
                    side=auto_side,
                    target_root=auto_root,
                )
            else:
                action = mc_actions.add_slot(
                    path=path,
                    label=path.stem,
                    target_path=next_path,
                    side=next_side,
                    target_root=False,
                )
        scratch = mc_reduce(scratch, action)
        built.append(action)
        last_added = scratch.slots[-1].id
        planned.append((last_added, path))
    if not built:
        return
    store = widget.store
    transact = getattr(store, "transact", None)
    if callable(transact):
        store.transact(built)
    else:  # headless fakes pre-dating the batch API: same end state, N dispatches
        for sub in built:
            store.dispatch(sub)
    live_ids = {s.id for s in widget.state.slots}
    for sid, path in planned:
        if sid not in live_ids:
            continue
        show_loading_toast(controller, sid)
        _preview.load_preview_async(controller, path, sid)