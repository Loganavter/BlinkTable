"""Image pyramid builds: demand kicks, progress tracking, canvas updates.

The pyramid provides tiled LOD (level-of-detail) for GPU rendering.
Each full-res image store gets a pyramid whose levels build progressively
in a background worker, emitting per-level progress signals.

Demand policy (GIMP-style, IC slots to demand pyramid): unify completion
kicks :meth:`PyramidBuildCoordinator.demand_level` for the fit-view level
(``select_level`` over the full ``estimate_total_levels`` count — QL
parity, ``image_gallery/ui/quick_look/preview_canvas.py``) instead of the
eager full ``start_build``. Coarse/fit levels land; coarser levels wait
for a zoom-out that needs them via :func:`demand_for_zoom`. The engine
builds finest-first, so every level ``<= k_fit`` lands as a side effect
of demanding ``k_fit`` and zoom-ins stay on resident levels (no rebuild).

Toast progress is managed via the controller's bound methods
(``controller._finish_loading_toast``, etc.) — the dependency is
one-directional: **pyramid --> toast** (see ``loading_toast.py``).
"""

from __future__ import annotations

import logging

from tabs.image_compare.debug import ic_preview_debug as _preview_log
# W1 adapter: pyramid kicks go ONLY through the shared seam
# (tabs._shared.loading_pipeline.start_pyramid_build); the tab coordinator
# stays the hook (ownership unchanged), tab-local toast-liveness + abort
# decisions ride along in a closure so no direct engine coordinator call
# exists on this path (the hook is injected from the tab).
from tabs._shared.loading_pipeline import start_pyramid_build as _seam_start_pyramid_build
from core.log_names import APP_LOGGER_NAME

logger = logging.getLogger(APP_LOGGER_NAME)


def _session_abort_callable(controller):
    """Session abort predicate shared by eager and demand kicks (unchanged)."""
    sess = None
    try:
        if hasattr(controller, "_get_image_session"):
            sess = controller._get_image_session()
    except Exception:
        sess = None
    abort_sig = getattr(sess, "abort", None) if sess is not None else None
    # abort signal is single source; fallback to never-aborted signal for fakes
    try:
        from tabs.image_compare.pipeline.abort import AbortSignal as _AbortSignal

        if abort_sig is None or not hasattr(abort_sig, "is_aborted"):
            abort_sig = _AbortSignal()
    except Exception:
        abort_sig = None
    if abort_sig is not None and hasattr(abort_sig, "is_aborted"):
        return abort_sig.is_aborted
    return lambda: False


def _store_size(store) -> tuple[int, int]:
    """Base dimensions of a pixel store; (0, 0) when unresolvable."""
    try:
        w, h = int(getattr(store, "width", 0) or 0), int(getattr(store, "height", 0) or 0)
        if w > 0 and h > 0:
            return w, h
        from engine.store.tiled_pixel_store import pixel_source_size

        w2, h2 = pixel_source_size(store)
        return int(w2), int(h2)
    except Exception:
        return 0, 0


def needed_level_for_view(store, fb_w: int, fb_h: int, zoom) -> int:
    """Ideal pyramid level for the current view (QL parity, never raises).

    ``select_level`` over the full ``estimate_total_levels`` count with
    display-px per full-res px — the same inputs the live sampler uses, so
    the demand target and the renderer can never disagree. ``0`` (base,
    always resident) is the fail-open answer.
    """
    try:
        from engine.cache.lod import demand_level, dest_scale_fit
        from engine.store.pyramid_pixel_store import estimate_total_levels

        w, h = _store_size(store)
        if w <= 0 or h <= 0:
            return 0
        try:
            z = float(zoom)
        except Exception:
            return 0
        dest_scale = dest_scale_fit((fb_w, fb_h), (w, h), z)
        return int(demand_level(dest_scale, estimate_total_levels(w, h)))
    except Exception:
        return 0


def _current_view(controller) -> tuple[int, int, float]:
    """(framebuffer w/h, zoom) from the live canvas; fail-open (0, 0, 1.0)."""
    fb_w = fb_h = 0
    zoom = 1.0
    try:
        presenter = getattr(controller, "presenter", None)
        widget = getattr(presenter, "widget", None)
        host = getattr(widget, "image_label", None)
        if host is not None:
            try:
                size = host.size()
                fb_w, fb_h = int(size.width()), int(size.height())
            except Exception:
                try:
                    fb_w, fb_h = int(host.width()), int(host.height())
                except Exception:
                    fb_w = fb_h = 0
            try:
                from ui.canvas_infra.viewport import get_zoom_level

                zoom = float(get_zoom_level(host))
            except Exception:
                pass
    except Exception:
        pass
    return fb_w, fb_h, zoom


def _is_full_store(obj) -> bool:
    """Full-res pixel store guard: open, non-preview (never raises)."""
    if obj is None:
        return False
    try:
        from PySide6.QtGui import QImage as _QImage

        if isinstance(obj, _QImage):
            return False
    except Exception:
        pass
    try:
        open_attr = getattr(obj, "is_open", None)
        is_open = open_attr() if callable(open_attr) else bool(open_attr)
    except Exception:
        return False
    return bool(is_open)


def _live_unified_stores(controller) -> list[tuple]:
    """Current unified full-res stores as ``[(store, slot_id)]`` (never raises)."""
    try:
        sd = getattr(controller.store.viewport, "session_data", None)
        im = getattr(sd, "image_state", None)
        if im is None:
            return []
        out = []
        for slot, cand in ((1, getattr(im, "image1", None)), (2, getattr(im, "image2", None))):
            if _is_full_store(cand):
                out.append((cand, slot))
        return out
    except Exception:
        return []


def _legacy_start_build(coord, store, slot_id, slot_toast_live, should_abort) -> bool:
    """Eager kick with today's toast-live wiring (fallback path).

    Never raises: a kick that blows up (a closed store mid-sweep, a coordinator
    that rejects the shape) reports ``False`` so the caller can settle the
    toast instead of leaving it hanging — the failure this function used to
    leak as ``AttributeError: '_SharedView' object has no attribute '_memmap'``
    (owner report 2026-11-15).
    """
    try:
        try:
            return bool(
                coord.start_build(
                    store,
                    slot_id=slot_id,
                    toast_live=slot_toast_live,
                    should_abort=should_abort,
                )
            )
        except TypeError:
            return bool(coord.start_build(store, slot_id=slot_id))
    except Exception:
        _preview_log(
            "demand_fit_level: legacy kick failed for slot %s (store=%r)",
            slot_id,
            type(store).__name__,
        )
        return False


def _finish_slot_toast(controller, slot_id) -> None:
    """Finish a slot toast when no build is needed (tiny image, k == 0)."""
    try:
        finish = getattr(controller, "_finish_loading_toast", None)
        if callable(finish):
            finish(slot_id)
    except Exception:
        pass


def _remember_pending_demand(controller, store, slot_id: int, k: int) -> None:
    """Park a demand the coordinator refused (in-flight); retried on complete."""
    try:
        from engine.cache.image_identity import image_uid

        uid = int(image_uid(store))
    except Exception:
        return
    try:
        pending = getattr(controller, "_pending_pyramid_demands", None)
        if pending is None or not isinstance(pending, dict):
            pending = {}
            controller._pending_pyramid_demands = pending
        prev = pending.get(uid)
        prev_k = int(prev[2]) if isinstance(prev, tuple) and len(prev) == 3 else 0
        pending[uid] = (store, int(slot_id), max(int(k), prev_k))
    except Exception:
        pass


def _drain_pending_demands(controller) -> None:
    """Retry parked demands after a build completes (never raises).

    A superseded session drops the whole map (its abort predicate already
    fires); entries whose target landed or whose store closed are dropped.
    """
    try:
        pending = getattr(controller, "_pending_pyramid_demands", None)
        if not pending:
            return
        coord = getattr(controller, "_pyramid_coordinator", None)
        demand = getattr(coord, "demand_level", None) if coord is not None else None
        if demand is None:
            return
        should_abort = _session_abort_callable(controller)
        try:
            if bool(should_abort()):
                pending.clear()
                return
        except Exception:
            pass
        try:
            from engine.store.pyramid_registry import pyramid_for
        except Exception:
            pyramid_for = None  # type: ignore
        for uid in list(pending.keys()):
            entry = pending.pop(uid, None)
            if not isinstance(entry, tuple) or len(entry) != 3:
                continue
            store, slot_id, k = entry
            try:
                pyr = pyramid_for(store) if pyramid_for is not None else None
                if pyr is None:
                    continue  # store closed — drop
                if bool(pyr.is_level_built(int(k))):
                    continue  # landed meanwhile — drop
            except Exception:
                continue
            try:
                if not bool(demand(store, int(k), slot_id=int(slot_id), should_abort=should_abort)):
                    _remember_pending_demand(controller, store, int(slot_id), int(k))
            except Exception:
                pass
    except Exception:
        pass


def demand_fit_level(controller, store, slot_id: int) -> bool:
    """Demand the fit-view level for *store* (unify-completion kick).

    Coordinator ``demand_level`` verbatim with the same toast-liveness /
    abort wiring as the eager path; already-built targets never rebuild.
    Falls back to the legacy eager kick when the view is unknown, the
    toast is not live (preview-tier race), or the coordinator predates
    ``demand_level``. Never raises; True iff a worker was kicked or the
    target is already resident.
    """
    coord = getattr(controller, "_pyramid_coordinator", None)
    if coord is None or store is None:
        return False
    pl = getattr(controller, "pipeline", None)
    has_inflight = _has_inflight_for_slot(pl, int(slot_id))
    slot_toast_live = not has_inflight
    should_abort = _session_abort_callable(controller)
    demand = getattr(coord, "demand_level", None)
    fb_w, fb_h, zoom = _current_view(controller)
    if demand is None or fb_w <= 0 or fb_h <= 0:
        kicked = _legacy_start_build(coord, store, slot_id, slot_toast_live, should_abort)
        # Nothing will build: a live toast must not wait forever for a
        # progress event that can never come.
        if not kicked and slot_toast_live:
            _finish_slot_toast(controller, slot_id)
        return kicked
    if not slot_toast_live:
        # Preview-tier race: today's non-live wiring touches no toast.
        return _legacy_start_build(coord, store, slot_id, slot_toast_live, should_abort)
    k = needed_level_for_view(store, fb_w, fb_h, zoom)
    if k <= 0:
        # Base always resident (tiny image or >=100% zoom): no worker, but
        # a live toast still needs its finish (legacy skip-path parity).
        if slot_toast_live:
            _finish_slot_toast(controller, slot_id)
        return True
    try:
        kicked = bool(demand(store, int(k), slot_id=int(slot_id), should_abort=should_abort))
    except TypeError:
        try:
            kicked = bool(demand(store, int(k), slot_id=int(slot_id)))
        except Exception:
            if slot_toast_live:
                _finish_slot_toast(controller, slot_id)
            return False
    except Exception:
        if slot_toast_live:
            _finish_slot_toast(controller, slot_id)
        return False
    if not kicked:
        _remember_pending_demand(controller, store, slot_id, k)
    return kicked


def demand_for_zoom(controller, zoom=None) -> bool:
    """Zoom-hook entry (GIMP-style): demand newly-needed levels, never rebuild.

    Reads the live unified full-res stores, computes each slot's ideal
    level for *zoom* (or the live canvas zoom), and demands it through the
    shared coordinator when unbuilt. Already-built targets are a no-op
    (the coordinator short-circuits without a worker); in-flight targets
    are parked and retried when the running build completes. Never raises;
    True iff any worker was kicked.
    """
    try:
        if controller is None:
            return False
        coord = getattr(controller, "_pyramid_coordinator", None)
        demand = getattr(coord, "demand_level", None) if coord is not None else None
        if demand is None:
            return False
        fb_w, fb_h, live_zoom = _current_view(controller)
        if fb_w <= 0 or fb_h <= 0:
            return False
        try:
            z = float(live_zoom if zoom is None else zoom)
        except Exception:
            return False
        stores = _live_unified_stores(controller)
        if not stores:
            return False
        should_abort = _session_abort_callable(controller)
        try:
            if bool(should_abort()):
                return False
        except Exception:
            return False
        kicked_any = False
        for store, slot_id in stores:
            try:
                k = needed_level_for_view(store, fb_w, fb_h, z)
            except Exception:
                continue
            if k <= 0:
                continue  # base always resident — nothing to demand
            try:
                if bool(demand(store, int(k), slot_id=int(slot_id), should_abort=should_abort)):
                    kicked_any = True
                else:
                    _remember_pending_demand(controller, store, slot_id, k)
            except Exception:
                continue
        return kicked_any
    except Exception:
        return False


def _has_inflight_for_slot(pipeline, image_number: int) -> bool:
    try:
        if pipeline is None or not hasattr(pipeline, "_inflight"):
            return False
        for k, sig in list(pipeline._inflight.items()):
            try:
                if sig.is_aborted():
                    continue
            except Exception:
                pass
            if isinstance(k, tuple) and len(k) >= 2 and isinstance(k[0], int) and k[0] == int(image_number):
                return True
            if isinstance(k, tuple) and k and k[0] == "__full_count__" and len(k) > 1 and k[1] == int(image_number):
                try:
                    if not sig.is_aborted():
                        return True
                except Exception:
                    return True
        return False
    except Exception:
        return False


def start_pyramid_builds(controller, *stores) -> None:
    # Called as start_pyramid_builds(controller, u1, u2) -- positional order
    # matches image_state.image1/image2 at the call site, so slot number is
    # simply the 1-based position here.
    # Prefer shared coordinator when present (B3 dedup); fallback keeps fake
    # controllers without coordinator working.
    # Demand policy: each slot kicks demand_fit_level (bounded demand for the
    # fit-view level) instead of the eager full start_build.
    coord = getattr(controller, "_pyramid_coordinator", None)
    if coord is not None:
        for slot_offset, store in enumerate(stores):
            if store is None:
                _preview_log(
                    "start_pyramid_builds: slot %s has no store yet, skipped",
                    slot_offset + 1,
                )
                continue
            image_number = slot_offset + 1
            # W1: the kick routes through the shared seam; the coordinator
            # stays the hook — the seam's plain-callable shape carries
            # (store, slot) and the closure upgrades the kick to demand
            # (toast-liveness + abort decisions live in demand_fit_level,
            # same single-flight/abort sources as the eager path before).
            def _hook(store_arg, slot_arg, _controller=controller):
                demand_fit_level(_controller, store_arg, int(slot_arg))

            _seam_start_pyramid_build(store, image_number, toast_hook=_hook)
        return


def on_pyramid_level_ready(controller, payload) -> None:
    coord = getattr(controller, "_pyramid_coordinator", None)
    if coord is not None:
        coord.on_level_ready(payload)
        try:
            complete = isinstance(payload, tuple) and len(payload) == 4 and bool(payload[3])
        except Exception:
            complete = False
        if complete:
            # A parked zoom demand (refused while this build was in flight)
            # gets its retry now that the worker is done.
            _drain_pending_demands(controller)
        return
    from tabs._shared.loading_toast import PYRAMID_START_PROGRESS

    uid, level_count, total_levels, complete = payload
    # Tile replacement marker: the pyramid just wrote a LOD level into the
    # store that backs the live canvas. Only `complete` drops the pick
    # signatures (preview -> store flip); intermediate levels only arm a
    # repaint, which render_flow then gates on its signatures.
    _preview_log(
        "tiles replaced: pyramid level ready (uid=%s level=%d/%d complete=%s)",
        uid,
        level_count,
        total_levels,
        complete,
    )
    # Only a *completed* pyramid can flip pick_display_image from the
    # preview tier to the tiled store — that's the only publish that
    # needs the pick signatures dropped. Intermediate levels just need
    # a repaint so the per-frame LOD selector can use them; a full
    # invalidation per level caused plan re-applies mid-interaction
    # (docs/dev/rendering/display-image-pipeline.md, preview→store flip).
    if complete:
        controller._invalidate_image_canvas_render_state()
    controller._schedule_image_canvas_update()
    image_number = controller._loading_toast_uid_slot.get(uid)
    if image_number is None:
        return
    if complete:
        controller._loading_toast_uid_slot.pop(uid, None)
        controller._finish_loading_toast(image_number)
    else:
        fraction = level_count / max(total_levels, 1)
        percent = PYRAMID_START_PROGRESS + int(
            fraction * (100 - PYRAMID_START_PROGRESS)
        )
        controller._set_loading_toast_progress(image_number, percent)
