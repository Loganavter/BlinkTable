"""Chrome input handling for multi-compare canvas (zoom/pan/keys/context).

Audit-Meta: pattern=thin-owner reason="Wheel/pan/key/context share Qt event-name surface delegated by canvas widget; zoom-tick fit-cache + throttles live here by necessity, divider/slot gestures already split under canvas/features"

Feature-specific gestures (dividers, slot drag) stay in
``canvas/features/*/input/`` and are routed via ``gesture_resolver``.
"""

from __future__ import annotations

import time
from contextlib import nullcontext

from PySide6.QtCore import QMimeData, QPoint, QRect, Qt
from PySide6.QtGui import QContextMenuEvent, QDrag, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QWidget

from engine.frame.context import make_ids
from tabs.multi_compare.canvas.features.grid_dividers.input.hit import divider_at
from tabs.multi_compare.canvas.gesture_resolver import (
    GesturePressContext,
    iter_active,
    resolve_press,
)
from tabs.multi_compare.debug import mc_dnd_debug, mc_dnd_debug_enabled
from tabs.multi_compare.models import CompareSlot, LeafNode
from tabs.multi_compare.scene import actions
from tabs.multi_compare.scene.projection import clamp_scene_pan, scene_camera
from tabs.multi_compare.ui import hit_projection
from tabs.multi_compare.ui.canvas_helpers import INTERNAL_SLOT_MIME, _dividers_locked
from ui.context_menu.manager import open_context_menu
from ui.context_menu.models import ContextMenuRequest, ContextMenuTarget

#: IC parity (``compute_zoom_wheel_transform``): zoom deltas at/below this
#: absolute epsilon are invisible -- swallow the tick instead of dispatching
#: a full fan-out for float dust (e.g. clamp-boundary remainders).
_ZOOM_IDENTICAL_EPS = 1e-6

#: Fit-scale memo bound (entries are tiny tuples; cleared oldest-first).
_FIT_CACHE_MAX = 128


def clamp_pan_values(
    pan_x: float, pan_y: float, zoom: float,
    fit_x: float = 1.0, fit_y: float = 1.0,
) -> tuple[float, float]:
    """Cover-range clamp: the viewport never shows void past the image.

    QuickLook parity (``tabs/image_gallery/ui/quick_look/preview_zoom.py``
    ``clamp_pan``): at/below the cover point the fitted frame is fully in
    view, so pan is forced to the origin (a leftover offset would fake a
    shift). Zoomed in, each axis clamps to ``|pan| <= (cover-1)/(2*cover)``
    with ``cover = fit*zoom`` -- the exact range where the shader's
    ``uv = (slotUV-0.5)/fit/zoom+0.5-pan`` chain (``shaders/qrhi/
    multi_compare.frag``, identical in the ``slot_array.frag`` tile-array
    path) keeps the cell covered. Axes where the fitted frame is smaller
    than the cell (``fit*zoom <= 1``, letterboxed) center instead (pan 0).

    ``fit`` is the caller's reference-slot aspect-fit (picked leaf for
    wheel/drag, focused slot for keys) -- the same ``fit`` the delta math
    already uses. Fail-open: bad inputs return the inputs unchanged.
    """
    try:
        px, py = float(pan_x), float(pan_y)
        z = float(zoom)
    except Exception:
        return pan_x, pan_y
    if not (z > 0):
        return pan_x, pan_y
    out = []
    for p, fit in ((px, fit_x), (py, fit_y)):
        try:
            f = float(fit)
        except Exception:
            out.append(p)
            continue
        if not (f > 0):
            out.append(p)
            continue
        cover = f * z
        if cover <= 1.0:
            out.append(0.0)
        else:
            r = (cover - 1.0) / (2.0 * cover)
            out.append(max(-r, min(r, p)))
    return out[0], out[1]


def fit_scale_for(slot: CompareSlot, rect: QRect, source=None) -> tuple[float, float]:
    """Fit scale from the slot's cached tier (B1: ``source`` passed by the caller).

    ``None`` source (imageless slot) reads as ``(1.0, 1.0)``, same as an
    imageless slot before B1.
    """
    if source is None or rect.width() <= 0 or rect.height() <= 0:
        return 1.0, 1.0
    from engine.store.tiled_pixel_store import pixel_source_size

    w, h = pixel_source_size(source)
    if h <= 0 or w <= 0:
        return 1.0, 1.0
    img_ar = w / h
    cell_ar = rect.width() / rect.height()
    if img_ar > cell_ar:
        return 1.0, cell_ar / img_ar
    return img_ar / cell_ar, 1.0


def _source_for(widget, slot):
    """Cached tier for hit-test math (canvas owns the ``pixel_cache`` ref).

    Parametric-crop clip (lane mc-crop-render): passes the slot through
    ``residency.cropped_source_for_slot`` (same resolve as the canvas's
    own ``_slot_sources``, sharing its view cache), so zoom/pan fit math
    below measures the cropped size on cropped slots. ``None`` on empty
    crop intersection (gap) or imageless slots.
    """
    try:
        from tabs.multi_compare.pipeline.cache import resolve_slot_source
        from tabs.multi_compare.scene.passes import residency as _residency

        real = resolve_slot_source(getattr(widget, "pixel_cache", None), slot)
        if real is None:
            return None
        return _residency.cropped_source_for_slot(
            real,
            _residency.controller_for_canvas(widget),
            slot,
            cache=getattr(widget, "_crop_clip_views", None),
        )
    except Exception:
        return None


def _frame_context_from_widget(widget):
    """Best-effort live FrameContext via ``widget._renderer``.

    Mirrors image_compare's ``loupe_trace.frame_context_from_widget``
    (which reads ``widget._rhi_renderer``): lets widget-side writers (the
    wheel tick below) record into the renderer-owned context without
    renderer rewiring. ``None`` for fakes/widgets without a renderer --
    callers then skip the record.
    """
    renderer = getattr(widget, "_renderer", None)
    return getattr(renderer, "frame_context", None)


def _cache_generation(cache) -> int:
    try:
        return int(getattr(cache, "generation", 0) or 0)
    except Exception:
        return 0


def fit_scale_cached(widget, slot: CompareSlot, rect: QRect) -> tuple[float, float]:
    """``fit_scale_for`` memoized on the zoom hot path.

    The size inputs (source dims via an ``os.stat``-keyed cache resolve +
    rect dims) do not change between wheel ticks of one gesture, so repeat
    ticks hit the cache and skip the resolve entirely. The key carries the
    slot ``revision`` (bumps per decoded-tier arrival) and the pixel-cache
    ``generation`` (bumps on any put/evict, e.g. an on-disk file replacement
    resolving to a new content key), plus the rect size, so stale dims can
    never stick. Cursor-anchor math and clamping are untouched -- only the
    redundant re-resolve is skipped.
    """
    store = None
    key = None
    box = None
    try:
        from tabs.multi_compare.scene.passes import residency as _residency

        box = _residency.crop_box_for_slot(
            _residency.controller_for_canvas(widget), slot
        )
    except Exception:
        box = None
    try:
        store = getattr(widget, "_zoom_fit_cache", None)
        if store is None:
            store = {}
            widget._zoom_fit_cache = store
        key = (
            getattr(slot, "id", None),
            getattr(slot, "revision", 0),
            _cache_generation(getattr(widget, "pixel_cache", None)),
            rect.width(),
            rect.height(),
            # Crop toggle flips no revision/generation, so the effective
            # box itself keys the memo -- otherwise a toggled slot keeps
            # fit for the wrong size.
            box,
        )
        hit = store.get(key)
        # Tick-marker atom: remember whether this lookup hit, so the wheel
        # path's invalidate.mc_zoom_tick record can carry the fit-cache
        # hit/miss without recomputing the key (best-effort widget attr,
        # read back pre-dispatch; stale/absent reads as miss).
        try:
            widget._zoom_fit_cache_last_hit = hit is not None
        except Exception:
            pass
        if hit is not None:
            return hit
    except Exception:
        store = None
        key = None
    value = fit_scale_for(slot, rect, _source_for(widget, slot))
    try:
        if store is not None and key is not None:
            store[key] = value
            while len(store) > _FIT_CACHE_MAX:
                store.pop(next(iter(store)))
        widget._zoom_fit_cache_last_hit = False
    except Exception:
        pass
    return value


def leaf_at(pos: QPoint, leaf_rects) -> tuple[LeafNode, QRect] | None:
    for leaf, rect in leaf_rects:
        if rect.contains(pos):
            return leaf, rect
    return None


def _focused_image_rect(widget) -> QRect | None:
    """Widget-px rect of the actually displayed image while focused.

    Mirrors ``canvas/features/focus_dim/passes.py``'s letterbox math (same
    ``_canvas_layout()`` the renderer's own hit-testing uses) so clicks
    outside it are recognized as landing on the dimmed chrome, not the image.
    """
    layout = widget._canvas_layout()
    if layout is None:
        return None
    canvas_w, canvas_h, sr, ox, oy = layout
    return QRect(
        int(round(ox)),
        int(round(oy)),
        max(1, int(round(canvas_w * sr))),
        max(1, int(round(canvas_h * sr))),
    )


def _swallow_focus_dim_click(widget, pos: QPoint) -> bool:
    """If focused and ``pos`` lands outside the image (on the dimmed
    letterbox chrome), exit focus and report the click as consumed."""
    if not widget.state.is_focused:
        return False
    rect = _focused_image_rect(widget)
    if rect is not None and rect.contains(pos):
        return False
    widget._do_dispatch(actions.set_focus(None))
    return True


def _event_modifiers(event):
    """Modifiers of a Qt input event, fail-open.

    Test fakes (and any event object that only mirrors ``key()``) have no
    ``modifiers()``; reading through this helper keeps every existing
    unmodified binding byte-identical. Same for events whose ``modifiers()``
    raises during teardown.
    """
    getter = getattr(event, "modifiers", None)
    if callable(getter):
        try:
            return getter()
        except Exception:
            pass
    return Qt.KeyboardModifier.NoModifier


def _ctrl_held(event) -> bool:
    return bool(_event_modifiers(event) & Qt.KeyboardModifier.ControlModifier)


def _scene_composition_a(widget, canvas_w: float, canvas_h: float, zoom: float):
    """Fraction of the framebuffer the composition covers at ``zoom``.

    ``a = canvas * fit * zoom / framebuffer`` per axis (``fit`` = the
    composition letterbox fit). The scene camera's anchor math is expressed
    in these units: at identity the composition covers ``canvas*fit`` and
    each zoom step grows it by ``zoom``.
    """
    fb_w = max(1.0, float(widget.width()))
    fb_h = max(1.0, float(widget.height()))
    fit = min(fb_w / canvas_w, fb_h / canvas_h)
    return (
        canvas_w * fit * zoom / fb_w,
        canvas_h * fit * zoom / fb_h,
    )


def _scene_zoom_tick(widget, event: QWheelEvent, delta: int) -> None:
    """Ctrl+wheel: zoom the whole composition, anchored under the cursor.

    Separate (and first) branch of :func:`handle_wheel_event` — the content
    zoom below stays byte-for-byte. Same rules as the content path: scale by
    the delta magnitude (120 units/notch), clamp to ``[1, ZOOM_MAX]``, swallow
    sub-epsilon changes, snap pan to the origin at the floor, and dispatch at
    most one action per tick.

    Anchor: with ``a`` the framebuffer fraction the composition covers,
    ``screen_uv - 0.5 = a * (canvas_uv - 0.5 + pan)``, so holding the point
    under the cursor still means ``pan' = pan + (s - 0.5) * (1/a' - 1/a)``
    (``s`` = cursor in framebuffer-normalized units). Pan is clamped to
    ``|pan| <= (zoom-1)/(2*zoom)`` per axis, which is exactly the range where
    the shader's content-UV test still passes across the whole viewport —
    i.e. the viewport never shows void.
    """
    layout = hit_projection.canvas_layout(widget)
    if layout is None:
        event.ignore()
        return
    canvas_w = max(1.0, float(layout[0]))
    canvas_h = max(1.0, float(layout[1]))
    fb_w = max(1.0, float(widget.width()))
    fb_h = max(1.0, float(widget.height()))

    zoom1, pan_x, pan_y = scene_camera(widget.state)
    notches = delta / 120.0
    zoom2 = max(1.0, min(float(widget.ZOOM_MAX), zoom1 * widget.ZOOM_STEP**notches))
    if abs(zoom2 - zoom1) <= _ZOOM_IDENTICAL_EPS:
        # IC parity (``compute_zoom_wheel_transform`` swallows <= 1e-6):
        # invisible change -- no dispatch, no render, no composition rebuild.
        event.accept()
        return

    if zoom2 <= 1.0:
        # At the floor the whole composition is in view -- a carried pan would
        # only fake a shift (same snap as the content path's ZOOM_MIN branch).
        new_pan_x, new_pan_y = 0.0, 0.0
    else:
        a1_x, a1_y = _scene_composition_a(widget, canvas_w, canvas_h, zoom1)
        a2_x, a2_y = _scene_composition_a(widget, canvas_w, canvas_h, zoom2)
        if a1_x <= 0.0 or a1_y <= 0.0:
            event.accept()
            return
        s_x = event.position().x() / fb_w
        s_y = event.position().y() / fb_h
        new_pan_x = pan_x + (s_x - 0.5) * (1.0 / a2_x - 1.0 / a1_x)
        new_pan_y = pan_y + (s_y - 0.5) * (1.0 / a2_y - 1.0 / a1_y)
        new_pan_x, new_pan_y = clamp_scene_pan(new_pan_x, new_pan_y, zoom2)

    # One dispatch per tick (same as the content path). No LOD demand call:
    # the scene camera moves the whole composition in the frame, it does not
    # change any slot's on-screen texel density (that is the content zoom's
    # ``demand_for_zoom_widget`` fan-out).
    widget._do_dispatch(actions.set_scene_zoom(zoom2, new_pan_x, new_pan_y))
    event.accept()


def handle_wheel_event(widget, event: QWheelEvent) -> None:
    debug_ticks = mc_dnd_debug_enabled()
    tick_t0 = time.perf_counter() if debug_ticks else 0.0
    delta = event.angleDelta().y()
    if delta == 0:
        event.accept()
        return
    if _ctrl_held(event):
        # Ctrl+wheel is the scene camera; everything below is the untouched
        # content-zoom path (same chord grammar as Ctrl+drag / Ctrl+arrows).
        _scene_zoom_tick(widget, event, delta)
        return
    leaf_rects = widget._leaf_rects()
    if not leaf_rects:
        event.ignore()
        return

    pos = event.position().toPoint()
    picked = leaf_at(pos, leaf_rects)
    leaf, rect = picked or (None, None)
    if leaf is None:
        event.ignore()
        return
    assert rect is not None

    slot = next((s for s in widget.state.slots if s.id == leaf.slot_id), None)
    if slot is None:
        event.ignore()
        return

    fit_x, fit_y = fit_scale_cached(widget, slot, rect)
    cell_u = (pos.x() - rect.x()) / rect.width()
    cell_v = (pos.y() - rect.y()) / rect.height()

    # Perf spans (Wave D, plan-engine-perf-debugger-2026-09-20): the
    # wheel-to-photon chain opens here, mirroring IC's ``ic.wheel_tick``.
    # ``trace_id`` carries the ``wheel-<id>`` join atom so Perfetto can
    # join this tick with its downstream mc.enqueue/prepare/realize_slots/
    # mips/submit spans. Span/record only — the math and dispatch below
    # are unchanged. None-safe: fakes/widgets without a renderer-owned
    # context skip the span, not the zoom.
    z1 = widget.state.zoom
    _pan_before_x = widget.state.pan_x
    _pan_before_y = widget.state.pan_y
    _tick_context = _frame_context_from_widget(widget)
    if _tick_context is not None:
        _tick_context.bump("mc.wheel_tick")
        _wheel_id = (
            f"wheel-{_tick_context.frame_id}"
            f"-{_tick_context.count('mc.wheel_tick')}"
        )
        _wheel_cm = _tick_context.span(
            "mc.wheel_tick",
            detail=lambda: {
                "trace_id": _wheel_id,
                "angle_delta_y": delta,
                "zoom": [z1, z2],
                "pan": [[_pan_before_x, _pan_before_y], [new_pan_x, new_pan_y]],
            },
        )
    else:
        _wheel_id = None
        _wheel_cm = nullcontext()
    z2: float | None = None
    new_pan_x: float | None = None
    new_pan_y: float | None = None
    with _wheel_cm:
        # Scale by delta magnitude (Qt's 120-units-per-notch convention), not
        # just sign -- see docs/dev/rendering/tile-array-atlas-plan.md Findings
        # (image_compare's compute_zoom_wheel_transform had the same fixed-step-
        # per-event bug: a coalesced multi-notch burst produced the same tiny
        # step as a single click).
        notches = delta / 120.0
        factor = widget.ZOOM_STEP**notches
        z2 = max(widget.ZOOM_MIN, min(widget.ZOOM_MAX, z1 * factor))
        if abs(z2 - z1) <= _ZOOM_IDENTICAL_EPS:
            # IC parity (``compute_zoom_wheel_transform`` swallows <= 1e-6):
            # invisible change -- skip the whole dispatch fan-out (reducer,
            # subscribers, composition rebuild, uploads check, LOD switch).
            event.accept()
            return

        if z2 <= widget.ZOOM_MIN:
            # At the zoom floor there's no meaningful cursor-anchored offset left
            # (the whole cell is in view) -- snap pan to origin instead of
            # carrying over the last wheel step's clamped remainder, which would
            # otherwise leave a tiny nonzero pan that keeps the zoom indicator
            # visible even though zoom is back at its default.
            new_pan_x, new_pan_y = 0.0, 0.0
        else:
            # IC parity (``compute_zoom_wheel_transform`` clamps uv to
            # [0,1]): the content point under the cursor stays put, but a
            # cursor over letterbox (uv outside [0,1]) anchors to the image
            # edge instead of void -- otherwise zooming in a corner pushes
            # the image away and shows void. Unclamped this reduces to the
            # old ``pan + (cell-0.5)/fit * (1/z2-1/z1)`` form.
            uv_x = (
                (cell_u - 0.5) / max(fit_x, 1e-6) / max(z1, 1e-6)
                + 0.5
                - widget.state.pan_x
            )
            uv_y = (
                (cell_v - 0.5) / max(fit_y, 1e-6) / max(z1, 1e-6)
                + 0.5
                - widget.state.pan_y
            )
            uv_x = max(0.0, min(1.0, uv_x))
            uv_y = max(0.0, min(1.0, uv_y))
            new_pan_x = (
                0.5
                + (cell_u - 0.5) / max(fit_x, 1e-6) / max(z2, 1e-6)
                - uv_x
            )
            new_pan_y = (
                0.5
                + (cell_v - 0.5) / max(fit_y, 1e-6) / max(z2, 1e-6)
                - uv_y
            )
            new_pan_x, new_pan_y = clamp_pan_values(
                new_pan_x, new_pan_y, z2, fit_x, fit_y
            )
        # IC parity: the wheel path never kicks window activation (no per-tick
        # raise/activate round-trip). That storm is what Mutter answers with
        # busy-cursor feedback; the Wayland/Vulkan stale-canvas catch-up is
        # handled on gesture settle by the debounced compositor sync
        # (see divider_sync) instead.
        pre_dispatch_ms = (
            (time.perf_counter() - tick_t0) * 1000.0 if debug_ticks else 0.0
        )
        # Frame-bus tick marker (record-only, pre-dispatch): joins this wheel
        # tick to the frame it kicks -- tick id only, no timing (timing belongs
        # to the perf-debugger epic). None-safe: fakes/widgets without a
        # renderer-owned context skip the record. The existing mc_dnd_debug tick
        # log below is untouched.
        if _tick_context is not None:
            try:
                _fit_hit = bool(getattr(widget, "_zoom_fit_cache_last_hit", False))
            except Exception:
                _fit_hit = False
            _tick_context.record_invalidation(
                "mc_zoom_tick",
                f"wheel tick delta={delta} zoom {z1:.6f}->{z2:.6f}"
                f" fit_cache={'hit' if _fit_hit else 'miss'}",
                ids=make_ids(
                    _tick_context.frame_id,
                    delta,
                    (round(z1, 6), round(z2, 6)),
                    "fit-hit" if _fit_hit else "fit-miss",
                ),
            )
        # Present proxy (enqueue-side ONLY, mirrors IC's ic.present_proxy):
        # this dispatch is what schedules the compositor present; the span
        # stops when dispatch returns, so compositor time is EXCLUDED by
        # construction — never read this as photon timing.
        _pfc = _frame_context_from_widget(widget)
        with (
            _pfc.span(
                "mc.present_proxy",
                detail=lambda: {"trace_id": _wheel_id, "source": "wheel"},
            )
            if _pfc is not None
            else nullcontext()
        ):
            widget._do_dispatch(actions.set_zoom(z2, new_pan_x, new_pan_y))
        # MC demand pyramid (GIMP-style): a zoom past built levels kicks
        # only the newly needed level via use_cases (E2 coordinator, same
        # toast/abort routing; built levels no-op). One fail-open call —
        # no canvas restructuring, never raises.
        try:
            from tabs.multi_compare.use_cases import loading as _mc_loading

            _mc_loading.demand_for_zoom_widget(
                widget,
                slot.id,
                z2,
                rect=(rect.width(), rect.height()),
                fit=(fit_x, fit_y),
            )
        except Exception:
            pass
        if debug_ticks:
            mc_dnd_debug(
                "[mc-zoom] tick delta=%d zoom=%.6f->%.6f pan=(%.4f,%.4f) "
                "pre_dispatch_ms=%.3f dispatch_ms=%.3f",
                delta,
                z1,
                z2,
                new_pan_x,
                new_pan_y,
                pre_dispatch_ms,
                (time.perf_counter() - tick_t0) * 1000.0 - pre_dispatch_ms,
            )
        event.accept()


def _rmb_surface_override():
    """Optional A/B: ``IMGSLI_MC_RMB_SURFACE=in_window|popup``."""
    from shared.debug_flags import env_value as _env_value

    raw = (_env_value("IMGSLI_MC_RMB_SURFACE", "") or "").strip().lower()
    if raw in ("in_window", "popup"):
        return raw
    return None


def handle_context_menu_event(widget, event: QContextMenuEvent) -> None:
    """Open the slot menu on Qt's context-menu request (not RMB press).

    Match Image Compare: manager defaults (``popup`` on Linux) with the QRhi
    canvas as the toolkit parent. Do **not** set ``menu_parent`` to MainWindow —
    that caused a one-frame clear wipe on Wayland while IC (canvas parent)
    stays stable.

    Optional A/B: ``IMGSLI_MC_RMB_SURFACE=in_window|popup``.
    """
    pos = event.pos()
    if _swallow_focus_dim_click(widget, pos):
        event.accept()
        return
    picked = leaf_at(pos, widget._leaf_rects())
    if picked is None:
        event.ignore()
        return
    leaf, rect = picked
    slot = next((s for s in widget.state.slots if s.id == leaf.slot_id), None)

    from ui.canvas_infra.rhi.rhi_present_sync import ensure_window_active_for_qrhi

    ensure_window_active_for_qrhi(widget)

    menu = open_context_menu(
        ContextMenuRequest(
            source_widget=widget,
            global_pos=event.globalPos(),
            local_pos=pos,
            session_type="multi_compare",
            target=ContextMenuTarget(
                kind="multi_compare_slot",
                id=leaf.slot_id,
                payload={
                    "rect": rect,
                    "path": slot.path if slot is not None else None,
                    "label": slot.label if slot is not None else "",
                },
            ),
            surface=_rmb_surface_override(),
        )
    )
    if menu is None:
        event.ignore()
        return

    event.accept()


def handle_mouse_press_event(widget, event: QMouseEvent) -> None:
    pos = event.position().toPoint()

    if _swallow_focus_dim_click(widget, pos):
        event.accept()
        return

    if event.button() == Qt.MouseButton.LeftButton:
        ctx = GesturePressContext(
            handler=widget,
            local_pos=event.position(),
            button=event.button().value,
            modifiers=int(event.modifiers().value),
        )
        binding = resolve_press(ctx)
        if binding is not None:
            if binding.begin is not None:
                binding.begin(widget, event.position())
            event.accept()
            return

    if event.button() == Qt.MouseButton.MiddleButton:
        if _ctrl_held(event):
            # Ctrl+middle-drag moves the scene camera instead of the content
            # pan. State is seeded here (start pan + cursor in widget px), the
            # delta math runs in ``handle_mouse_move_event``.
            widget._scene_panning = True
            widget._scene_pan_start_pos = event.position()
            _zoom, scene_pan_x, scene_pan_y = scene_camera(widget.state)
            widget._scene_pan_start_state = (scene_pan_x, scene_pan_y)
            widget.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        widget._panning = True
        widget._pan_start_pos = event.position()
        widget._pan_start_state = (widget.state.pan_x, widget.state.pan_y)
        leaf_rects = widget._leaf_rects()
        picked = leaf_at(pos, leaf_rects)
        if picked is not None:
            leaf, rect = picked
            slot = next((s for s in widget.state.slots if s.id == leaf.slot_id), None)
            widget._pan_ref_rect = rect
            widget._pan_ref_fit = (
                fit_scale_for(slot, rect, _source_for(widget, slot))
                if slot is not None
                else (1.0, 1.0)
            )
        else:
            widget._pan_ref_rect = widget.rect()
            widget._pan_ref_fit = (1.0, 1.0)
        widget.setCursor(Qt.CursorShape.ClosedHandCursor)
        event.accept()
        return


def handle_mouse_move_event(widget, event: QMouseEvent) -> None:
    active = iter_active(widget)
    if active:
        buttons = event.buttons()
        consumed = False
        for binding in active:
            if not (buttons & Qt.MouseButton(binding.button)):
                continue
            if binding.update is not None:
                binding.update(widget, event.position())
                consumed = True
        if consumed:
            event.accept()
            return

    if getattr(widget, "_scene_panning", False):
        zoom, _pan_x, _pan_y = scene_camera(widget.state)
        layout = hit_projection.canvas_layout(widget)
        if layout is None:
            event.accept()
            return
        # Composition size in widget px at this camera (``sr`` already carries
        # the scene zoom), so a pixel drag converts straight into scene-pan
        # units: dragging right moves the composition right under the cursor.
        comp_w = max(1e-6, float(layout[0]) * float(layout[2]))
        comp_h = max(1e-6, float(layout[1]) * float(layout[2]))
        delta = event.position() - widget._scene_pan_start_pos
        new_pan_x = widget._scene_pan_start_state[0] + delta.x() / comp_w
        new_pan_y = widget._scene_pan_start_state[1] + delta.y() / comp_h
        new_pan_x, new_pan_y = clamp_scene_pan(new_pan_x, new_pan_y, zoom)
        if (
            new_pan_x != widget._scene_pan_start_state[0]
            or new_pan_y != widget._scene_pan_start_state[1]
        ):
            widget._do_dispatch(actions.set_scene_pan(new_pan_x, new_pan_y))
        event.accept()
        return

    if widget._panning:
        ref = widget._pan_ref_rect
        if ref.width() <= 0 or ref.height() <= 0:
            return
        fit_x, fit_y = widget._pan_ref_fit
        z = max(widget.state.zoom, 1e-6)
        delta = event.position() - widget._pan_start_pos
        new_pan_x = widget._pan_start_state[0] + (delta.x() / ref.width()) / (
            max(fit_x, 1e-6) * z
        )
        new_pan_y = widget._pan_start_state[1] + (delta.y() / ref.height()) / (
            max(fit_y, 1e-6) * z
        )
        new_pan_x, new_pan_y = clamp_pan_values(
            new_pan_x, new_pan_y, widget.state.zoom,
            fit_x, fit_y,
        )
        widget._do_dispatch(actions.set_pan(new_pan_x, new_pan_y))
        event.accept()
        return

    div = divider_at(widget, event.position())
    if div is not None and not _dividers_locked(widget.state):
        _path, _idx, _rect, direction, _ws = div
        widget.setCursor(
            Qt.CursorShape.SplitHCursor
            if direction == "h"
            else Qt.CursorShape.SplitVCursor
        )
    else:
        widget.setCursor(Qt.CursorShape.ArrowCursor)


def handle_mouse_release_event(widget, event: QMouseEvent) -> None:
    released_button = event.button().value
    ended_any = False
    for binding in iter_active(widget):
        if binding.button != released_button:
            continue
        if binding.end is not None:
            binding.end(widget)
        ended_any = True
    if ended_any:
        event.accept()
        return
    if event.button() == Qt.MouseButton.MiddleButton and getattr(
        widget, "_scene_panning", False
    ):
        widget._scene_panning = False
        widget.setCursor(Qt.CursorShape.ArrowCursor)
        event.accept()
        return
    if event.button() == Qt.MouseButton.MiddleButton and widget._panning:
        widget._panning = False
        widget.setCursor(Qt.CursorShape.ArrowCursor)
        event.accept()


def handle_mouse_double_click_event(widget, event: QMouseEvent) -> None:
    if event.button() == Qt.MouseButton.LeftButton:
        pos = event.position().toPoint()
        if _swallow_focus_dim_click(widget, pos):
            event.accept()
            return
        div = divider_at(widget, event.position())
        if div is not None:
            split_path, _idx, _drect, _direction, weights = div
            n = len(weights)
            if n > 0:
                from tabs.multi_compare.pipeline.cache import sizes_for_slots
                from tabs.multi_compare.scene import actions as _mc_actions

                sizes = None
                try:
                    sizes = sizes_for_slots(
                        getattr(widget, "pixel_cache", None),
                        getattr(getattr(widget, "state", None), "slots", None),
                    )
                except Exception:
                    sizes = None
                widget._do_dispatch(
                    _mc_actions.set_split_weights(split_path, [1.0] * n, sizes=sizes)
                )
            event.accept()


from shared.canvas.keyboard_constants import (
    KEY_PAN as _KEY_PAN,
    KEY_PAN_NUDGE as _KEY_PAN_NUDGE,
    KEY_ZOOM_IN as _KEY_ZOOM_IN,
    KEY_ZOOM_OUT as _KEY_ZOOM_OUT,
)


def _keyboard_pan_reference(widget) -> tuple[QRect, tuple[float, float]]:
    """Reference rect + fit used to convert a screen nudge into pan units.

    Mirrors the middle-button pan reference: the focused slot's leaf when a
    slot is focused, otherwise the whole widget rect with fit ``(1, 1)``.
    """
    if widget.state.is_focused:
        for leaf, rect in widget._leaf_rects():
            slot = next(
                (s for s in widget.state.slots if s.id == leaf.slot_id), None
            )
            if slot is not None:
                return rect, fit_scale_for(slot, rect, _source_for(widget, slot))
    return widget.rect(), (1.0, 1.0)


def _apply_keyboard_pan(widget, key) -> None:
    _rect, (fit_x, fit_y) = _keyboard_pan_reference(widget)
    z = max(widget.state.zoom, 1e-6)
    dx = -1 if key == Qt.Key.Key_Left else (1 if key == Qt.Key.Key_Right else 0)
    dy = -1 if key == Qt.Key.Key_Up else (1 if key == Qt.Key.Key_Down else 0)
    dpan_x = dx * _KEY_PAN_NUDGE / (max(fit_x, 1e-6) * z)
    dpan_y = dy * _KEY_PAN_NUDGE / (max(fit_y, 1e-6) * z)
    new_x, new_y = clamp_pan_values(
        widget.state.pan_x + dpan_x,
        widget.state.pan_y + dpan_y,
        z,
        fit_x,
        fit_y,
    )
    if new_x != widget.state.pan_x or new_y != widget.state.pan_y:
        widget._do_dispatch(actions.set_pan(new_x, new_y))


def _apply_keyboard_zoom(widget, key) -> None:
    factor = widget.ZOOM_STEP if key in _KEY_ZOOM_IN else 1.0 / widget.ZOOM_STEP
    z1 = widget.state.zoom
    z2 = max(widget.ZOOM_MIN, min(widget.ZOOM_MAX, z1 * factor))
    if z2 == z1:
        return
    if z2 <= widget.ZOOM_MIN:
        new_pan_x, new_pan_y = 0.0, 0.0
    else:
        # Anchor at the reference cell's center (cell_u == cell_v == 0.5), so
        # the center stays fixed and pan is unchanged — same result as wheel
        # zoom over the middle of the cell. Zooming out can still leave the
        # carried pan outside the new cover range, so clamp it (same
        # reference fit as the keyboard-pan path).
        _rect, (_fit_x, _fit_y) = _keyboard_pan_reference(widget)
        new_pan_x, new_pan_y = clamp_pan_values(
            widget.state.pan_x, widget.state.pan_y, z2, _fit_x, _fit_y
        )
    widget._do_dispatch(actions.set_zoom(z2, new_pan_x, new_pan_y))
    # MC demand pyramid (keyboard zoom has no picked slot → fan out over
    # every slot holding a pixel store; same fail-open single call as wheel).
    try:
        from tabs.multi_compare.use_cases import loading as _mc_loading

        _mc_loading.demand_for_zoom_widget(widget, None, z2)
    except Exception:
        pass


def _apply_keyboard_scene_zoom(widget, key) -> None:
    """Ctrl+``+``/``-``: scene zoom around the viewport center.

    Centered means the pan is carried unchanged (the point at the viewport
    center stays fixed by construction of ``s = 0.5``), then clamped to the
    new legal window; the floor snaps it to the origin, mirroring the content
    path's ``_apply_keyboard_zoom``.
    """
    factor = widget.ZOOM_STEP if key in _KEY_ZOOM_IN else 1.0 / widget.ZOOM_STEP
    zoom1, pan_x, pan_y = scene_camera(widget.state)
    zoom2 = max(1.0, min(float(widget.ZOOM_MAX), zoom1 * factor))
    if zoom2 == zoom1:
        return
    if zoom2 <= 1.0:
        new_pan_x, new_pan_y = 0.0, 0.0
    else:
        new_pan_x, new_pan_y = clamp_scene_pan(pan_x, pan_y, zoom2)
    widget._do_dispatch(actions.set_scene_zoom(zoom2, new_pan_x, new_pan_y))


def _apply_keyboard_scene_pan(widget, key) -> None:
    """Ctrl+arrows: scene pan by ``KEY_PAN_NUDGE`` of the *visible* window.

    ``1/zoom`` is the visible window's width in composition units, so the
    nudge keeps a fixed on-screen distance at any scene zoom (same intent as
    the content pan's ``nudge / (fit * zoom)``).
    """
    zoom, pan_x, pan_y = scene_camera(widget.state)
    if zoom <= 1.0:
        return
    dx = -1 if key == Qt.Key.Key_Left else (1 if key == Qt.Key.Key_Right else 0)
    dy = -1 if key == Qt.Key.Key_Up else (1 if key == Qt.Key.Key_Down else 0)
    step = _KEY_PAN_NUDGE / zoom
    new_x, new_y = clamp_scene_pan(pan_x + dx * step, pan_y + dy * step, zoom)
    if new_x != pan_x or new_y != pan_y:
        widget._do_dispatch(actions.set_scene_pan(new_x, new_y))


def handle_key_press_event(widget, event) -> None:
    key = event.key()
    if _ctrl_held(event):
        # Ctrl chords are the scene camera: zoom the whole composition,
        # pan it, or reset BOTH cameras. Plain keys keep their meaning.
        if key in _KEY_ZOOM_IN or key in _KEY_ZOOM_OUT:
            _apply_keyboard_scene_zoom(widget, key)
            event.accept()
            return
        if key in _KEY_PAN:
            _apply_keyboard_scene_pan(widget, key)
            event.accept()
            return
        if key == Qt.Key.Key_0:
            widget._do_dispatch(actions.reset_view())
            event.accept()
            return
    if key == Qt.Key.Key_Escape and widget.state.is_focused:
        widget._do_dispatch(actions.set_focus(None))
        event.accept()
    elif key == Qt.Key.Key_0:
        widget._do_dispatch(actions.reset_view())
        event.accept()
    elif key in _KEY_PAN:
        _apply_keyboard_pan(widget, key)
        event.accept()
    elif key in _KEY_ZOOM_IN or key in _KEY_ZOOM_OUT:
        _apply_keyboard_zoom(widget, key)
        event.accept()
    else:
        QWidget.keyPressEvent(widget, event)


def build_internal_drag_mime(slot_id: int, path=None) -> QMimeData:
    """Mime for a slot drag — intra-tab reorder *and* cross-tab delivery.

    Two payloads on one gesture:

    * ``INTERNAL_SLOT_MIME`` (slot id) — this tab's own drop path reorders /
      swaps the slot (``ui/drag_drop.py``); the id only means something inside
      the session it came from (foreign-session ids are ignored there).
    * ``WORKSPACE_TAB_PATHS_MIME`` + ``setUrls`` — the same wire a gallery-card
      drag speaks, so the host tab-strip router delivers the image to the tab
      under the cursor exactly like gallery DnD. Copy semantics: the file is
      not touched, the source slot keeps its image.

    A slot without a path (imageless placeholder) carries only the internal
    half — there is nothing to hand over.
    """
    mime = QMimeData()
    try:
        mime.setData(INTERNAL_SLOT_MIME, str(slot_id).encode("utf-8"))
    except Exception:
        pass
    if path:
        try:
            from PySide6.QtCore import QUrl

            from shared.drag_mime import WORKSPACE_TAB_PATHS_MIME

            url = QUrl.fromLocalFile(str(path))
            mime.setData(WORKSPACE_TAB_PATHS_MIME, url.toString().encode("utf-8"))
            mime.setUrls([url])
        except Exception:
            pass
    return mime


def start_internal_drag(widget, slot_id: int) -> None:
    slot = next((s for s in widget.state.slots if s.id == slot_id), None)
    mime = build_internal_drag_mime(slot_id, getattr(slot, "path", None))
    drag = QDrag(widget)
    drag.setMimeData(mime)

    rects = widget._leaf_rects()
    rect = next((r for l, r in rects if l.slot_id == slot_id), None)
    if rect is not None and slot is not None:
        from PySide6.QtGui import QPixmap

        from engine.store.tiled_pixel_store import qimage_from_pixel_source

        source = _source_for(widget, slot)
        if source is not None:
            qimg = qimage_from_pixel_source(source)
            preview = QPixmap.fromImage(qimg).scaledToWidth(
                160, Qt.TransformationMode.SmoothTransformation
            )
            drag.setPixmap(preview)
            drag.setHotSpot(QPoint(preview.width() // 2, preview.height() // 2))
    drag.exec(Qt.DropAction.MoveAction)