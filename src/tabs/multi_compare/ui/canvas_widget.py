"""Canvas host widget for the multi-compare scene.

Input chrome lives in ``canvas/interaction.py``; drop/hit projection in
``ui/drop_targets.py`` and ``ui/hit_projection.py``. Feature gestures stay
under ``canvas/features/*/input/``.
Audit-Meta: pattern=thin-owner reason="Canvas widget thin owner — delegates to interaction/render_context/features"
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QPoint, QPointF, QRect, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QContextMenuEvent, QMouseEvent, QPalette, QWheelEvent
from PySide6.QtWidgets import QRhiWidget, QWidget

from ui.canvas_infra.rhi.rhi_backend import configure_rhi_widget
from ui.canvas_infra.rhi.rhi_render import render_or_defer
from engine.gpu.coalesced_flush import CoalescedFlush
from engine.gpu.glass_panel import GlassPanelRegistry

from tabs.multi_compare.canvas import interaction as canvas_interaction
from tabs.multi_compare.first_frame_debug import (
    mc_first_frame_debug,
    mc_first_frame_debug_enabled,
    mc_first_frame_readiness_repr,
)
from tabs.multi_compare.models import (
    CompareSlot,
    LeafNode,
    MultiCompareState,
)
from tabs.multi_compare.scene import MultiCompareAction
from tabs.multi_compare.scene.renderer import MultiCompareRhiRenderer
from tabs.multi_compare.ui import drop_targets, hit_projection
from tabs.multi_compare.ui.canvas_helpers import (
    INTERNAL_SLOT_MIME,
    _dividers_locked,
    _layout_is_symmetric,
)
from core.log_names import APP_LOGGER_NAME

if TYPE_CHECKING:
    pass

logger = logging.getLogger(APP_LOGGER_NAME)

# Re-exports for feature modules / tests that import from this module.
__all__ = [
    "INTERNAL_SLOT_MIME",
    "MultiCompareCanvasWidget",
    "_dividers_locked",
    "_layout_is_symmetric",
]

# How many completed presents get a compositor settle kick. Shared with
# image_compare so both canvas tabs wait for the first frame that actually
# reaches the display (see shared/rendering/first_frame_gate.py).
from engine.cache.first_frame_gate import (
    FIRST_PRESENT_SETTLE_COUNT as _FIRST_PRESENT_SETTLE_COUNT,
    first_visual_present_count as _first_visual_present_count,
)


def _is_canvas_visible(widget):
    try:
        window = widget.window()
        ui = getattr(window, "ui", None) if window is not None else None
        if ui is None:
            presenter = getattr(window, "presenter", None) if window is not None else None
            ui = getattr(presenter, "ui", None) if presenter is not None else None
        stack = getattr(ui, "workspace_stack", None) if ui is not None else None
        if stack is None:
            from PySide6.QtWidgets import QStackedWidget
            p2 = widget.parentWidget()
            while p2 is not None:
                if isinstance(p2, QStackedWidget):
                    stack = p2
                    break
                p2 = p2.parentWidget()
        if stack is not None:
            cur = stack.currentWidget()
            if cur is None:
                return bool(widget.isVisible())
            if cur is widget:
                return True
            try:
                if hasattr(cur, "isAncestorOf") and cur.isAncestorOf(widget):
                    return True
            except Exception:
                pass
            p2 = widget.parentWidget()
            while p2 is not None:
                if p2 is cur:
                    return True
                p2 = p2.parentWidget()
            return False
        return bool(widget.isVisible())
    except Exception:
        try:
            return bool(widget.isVisible())
        except Exception:
            return True


class MultiCompareCanvasWidget(QRhiWidget):
    """QRhi canvas host for multi-compare rendering and input dispatch."""

    ZOOM_MIN = 1.0
    ZOOM_MAX = 50.0
    ZOOM_STEP = 1.1

    firstFrameRendered = Signal()

    dropTargetChanged = None

    def __init__(self, parent: QWidget | None = None, *, translate=None):
        super().__init__(parent)
        configure_rhi_widget(self)
        # Match image_compare's CanvasWidget: never enable QWidget autofill on a
        # QRhiWidget. Autofill + palette clear can fight RHI texture compositing
        # (stale zoom after reset while render() already drew z=1). Clear color
        # stays in the RHI pass via ``_theme_or_palette_bg()``.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self._allow_transparent_clear = False
        self.apply_theme_background()
        self._translate = translate or (lambda _key, default=None: default or _key)

        self.state = MultiCompareState()

        # B1: session pixel cache (owned by the controller, shared across
        # this page's MC sessions — keys are content-addressed). ``None``
        # until the controller wires it (headless canvases read imageless).
        self.pixel_cache = None

        # Parametric-crop clip views (lane mc-crop-render): sid ->
        # (real_source, box, view), owned here and shared with
        # canvas/interaction.py's fit path. Lets the pass resolve each
        # slot's effective box (``scene.passes.residency`` helpers) while
        # keeping swap duplicate-detection (``is``) stable across frames.
        self._crop_clip_views: dict[int, tuple[object, tuple[int, int, int, int], object]] = {}

        self._dispatch: Callable | None = None

        self._active_composition = None
        self._export_canvas_viewport: tuple[int, int, int, int] | None = None

        self._renderer = MultiCompareRhiRenderer(self)
        # Registered/unregistered by GlassHUD instances anchored to this
        # canvas (see ui/widgets/glass_hud/hud.py) -- consumed by
        # MultiCompareRhiRenderer.render(), which populates
        # self._glass_panel_sprites/_glass_panel_images for each GlassHUD's
        # own GlassPanelDisplayWidget to read. Mirrors image_compare's
        # CanvasWidget.glass_panels -- without this attribute existing at
        # all, GlassHUD._refresh_backdrop() silently no-ops (see its own
        # `registry is None` guard), so this canvas's ZoomIndicator never
        # got a blurred backdrop or its zoom-percent text rendered, only
        # the plain Qt-painted panel shape and the (real child widget)
        # reset button -- reported live as "рендерится только кнопка".
        self.glass_panels = GlassPanelRegistry()
        self._first_frame_emitted = False
        self._rhi_presents_completed = 0
        self._composition_flush = CoalescedFlush(self._flush_composition)

        self._panning = False
        self._pan_start_pos = QPointF()
        self._pan_start_state = (0.0, 0.0)
        self._pan_ref_rect = QRect()
        self._pan_ref_fit = (1.0, 1.0)
        # Scene-camera drag (Ctrl+middle drag): seeded on press, the delta math
        # lives in canvas/interaction.py's move handler.
        self._scene_panning = False
        self._scene_pan_start_pos = QPointF()
        self._scene_pan_start_state = (0.0, 0.0)

        self._divider_drag: tuple[tuple[int, ...], int, str, list[float]] | None = None
        self._divider_start_cursor = QPointF()

        self._lmb_press_pos: QPointF | None = None
        self._lmb_press_slot_id: int | None = None
        self._view_update_pending = False
        self._color_buffer_frozen = False
        self._composition_stale: bool = False

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        mc_first_frame_debug(self, "canvas constructed")

    def set_state(self, state: MultiCompareState) -> None:
        """Assign new state immediately (cheap); defer the expensive
        texture-sync/composition-rebuild to a single coalesced flush per
        event-loop tick, so N dispatches (zoom ticks, drag deltas, ...)
        arriving before the next tick cost one rebuild, not N.
        """
        self.state = state
        mc_first_frame_debug(
            self, "set_state slots=%s composition=%s", len(state.slots),
            self._active_composition is not None,
        )
        if not _is_canvas_visible(self):
            self._composition_stale = True
            try:
                from core.tracing.tracer import Tracer
                if Tracer.enabled():
                    Tracer.instance().record("render.mc.deferred", "MC composition deferred - background tab", {"slots": len(state.slots)})
            except Exception:
                pass
            return
        self._composition_flush.request()
        self.update()

    def _flush_composition(self) -> None:
        if not _is_canvas_visible(self):
            self._composition_stale = True
            try:
                from core.tracing.tracer import Tracer
                if Tracer.enabled():
                    Tracer.instance().record("render.mc.deferred", "MC _flush_composition deferred - background tab", {})
            except Exception:
                pass
            return
        self._sync_textures()
        self._rebuild_composition()
        self.request_view_update()

    def is_current_stack_page(self) -> bool:
        return _is_canvas_visible(self)

    def flush_stale_composition(self) -> bool:
        if not getattr(self, "_composition_stale", False):
            return False
        if not _is_canvas_visible(self):
            return False
        self._composition_stale = False
        try:
            from core.tracing.tracer import Tracer
            if Tracer.enabled():
                Tracer.instance().record("render.mc.flush", "MC stale composition flushed on show", {})
        except Exception:
            pass
        self._composition_flush.request()
        self.update()
        return True

    def request_view_update(self) -> None:
        """Ensure the QRhi backing store re-composites after view changes.

        Immediate ``update()`` plus a next-tick pass so overlay hide/show in
        the same stack frame (zoom-reset chip) cannot leave a stale frame.

        Perf spans (Wave D, plan-engine-perf-debugger-2026-09-20): the
        enqueue hop of the wheel-to-photon chain (mirrors IC's
        ``ic.enqueue`` in ``render_context.request_update``) — span/record
        only, the ``update()`` call below is unchanged. None-safe: fakes
        without a renderer-owned context skip the span, not the update.
        """
        try:
            from contextlib import nullcontext as _nullcontext

            _renderer = getattr(self, "_renderer", None)
            _fc = getattr(_renderer, "frame_context", None)
            _enqueue_cm = (
                _fc.span("mc.enqueue", detail=lambda: {"source": "request_view_update"})
                if _fc is not None
                else _nullcontext()
            )
        except Exception:
            from contextlib import nullcontext as _nullcontext

            _enqueue_cm = _nullcontext()
        with _enqueue_cm:
            self.update()
        if self._view_update_pending:
            return
        self._view_update_pending = True
        QTimer.singleShot(0, self._flush_view_update)

    def _flush_view_update(self) -> None:
        self._view_update_pending = False
        if not self.isVisible():
            return
        self.update()

    def is_color_buffer_frozen(self) -> bool:
        fixed = self.fixedColorBufferSize()
        return bool(fixed.isValid() and fixed.width() > 0 and fixed.height() > 0)

    def freeze_color_buffer(self) -> bool:
        """Pin the GPU color buffer (same idea as main-window CSD resize freeze).

        Popup / CSD micro-geometry must not recreate the swapchain or
        re-letterbox ``ox/oy/sr`` while a zoomed frame is on screen — that
        looks like a zoom nudge even when ``zoom``/``pan`` are unchanged.
        """
        try:
            if self.is_color_buffer_frozen():
                self._color_buffer_frozen = True
                return True
            size = self.size()
            if size.width() <= 0 or size.height() <= 0:
                return False
            self.setFixedColorBufferSize(size)
            self._color_buffer_frozen = True
            return True
        except Exception:
            logger.exception("freeze_color_buffer failed")
            return False

    def unfreeze_color_buffer(self) -> None:
        """Clear a fixed color buffer pin and request one present."""
        try:
            self.setFixedColorBufferSize(QSize())
        except Exception:
            logger.exception("unfreeze_color_buffer failed")
        self._color_buffer_frozen = False
        if self.isVisible():
            self.update()

    def render(self, command_buffer) -> None:  # type: ignore[override]  # RHI entrypoint, shadows QWidget.render
        # Match image_compare's CanvasWidget: only a completed beginPass/
        # endPass counts as a real present, and the *first* presented buffer
        # on D3D is often still an alpha hole through the translucent CSD
        # shell -- wait for a settle present + compositor flush before
        # trusting it enough to emit firstFrameRendered.
        #
        # Multi Compare additionally emits *after* the settle flush, not on
        # the present itself: on Wayland/Vulkan the first beginPass/endPass
        # is recorded while the compositor still shows the untouched
        # (transparent) subsurface, and only flush_qrhi_compositor()'s
        # requestUpdate/restack makes that frame visible. Emitting on
        # present #1 hides the startup placeholder a frame too early — the
        # user sees the transparent subsurface until the flush lands.
        painted = render_or_defer(
            self, "mc-canvas", lambda: self._renderer.render(command_buffer)
        )
        if not painted:
            mc_first_frame_debug(
                self, "render() returned NOT-painted (engine not ready yet)"
            )
            # Cold canvas would otherwise sit at presents==0 until the first
            # DnD drives a frame — and that frame then pays the full cold-init
            # cost as visible lag. Keep nudging until frames flow (bounded;
            # the drag path still initializes as fallback).
            self._nudge_init_retry()
            return

        self._rhi_presents_completed += 1
        mc_first_frame_debug(
            self, "render() painted present #%s [%s]",
            self._rhi_presents_completed, mc_first_frame_readiness_repr(self),
        )
        if self._rhi_presents_completed <= _FIRST_PRESENT_SETTLE_COUNT:
            # P4 settle-pump collapse (plan-mc-pipeline-refactor A4): the
            # count invariant is untouched -- every painted present still
            # counts toward the ``presents >= 10`` visual gate -- but only
            # the boundary presents pay the full compositor restack. The
            # first present needs it (makes the frame compositor-visible on
            # Wayland/Vulkan) and the tenth needs it (pre-emit restack, so
            # firstFrameRendered fires onto a genuinely visible frame);
            # intermediate presents just pump one canvas repaint to keep the
            # chain alive until the gate, without the raise_/activate +
            # win/parent/overlay update churn of flush_qrhi_compositor.
            if (
                self._rhi_presents_completed == 1
                or self._rhi_presents_completed >= _FIRST_PRESENT_SETTLE_COUNT
            ):
                self._settle_first_presents()
            else:
                self._pump_intermediate_present()

    def _pump_intermediate_present(self) -> None:
        """Keep the settle chain alive with a single canvas repaint.

        A direct ``update()`` only marks the widget dirty for the next
        frame (safe to call from inside ``render()``'s own paint), so the
        next present still happens without the raise_/activate +
        win/parent/overlay update churn of ``flush_qrhi_compositor``.
        Guarded: the pump must never break a frame on a half-torn-down
        widget (teardown races, ``__new__``-only unit fakes). Full
        ``flush_qrhi_compositor`` stays reserved for the first/tenth
        present (see ``render()``).
        """
        try:
            self.update()
        except (RuntimeError, AttributeError):
            pass

    def _settle_first_presents(self) -> None:
        """Boundary-present restack (first + tenth) + pre-emit gate check.

        Runs only where restack matters (see ``render()``'s P4 split):
        intermediate presents pump via ``_pump_intermediate_present``
        instead, so D3D still never shows a see-through hole while the
        per-present flush churn collapses to two full flushes total."""

        def _flush() -> None:
            try:
                from ui.canvas_infra.rhi.rhi_present_sync import flush_qrhi_compositor

                flush_qrhi_compositor(self, reason="mc-first-present")
                mc_first_frame_debug(self, "compositor settle flush ran")
            except Exception:
                self.update()
            self._emit_first_frame_if_ready()

        QTimer.singleShot(0, _flush)

    def _emit_first_frame_if_ready(self) -> None:
        """Emit firstFrameRendered only once the frame is compositor-visible."""
        if self._rhi_presents_completed < _first_visual_present_count():
            return
        if not self._first_frame_emitted:
            self._first_frame_emitted = True
            mc_first_frame_debug(
                self, "EMIT firstFrameRendered (presents=%s) [%s]",
                self._rhi_presents_completed, mc_first_frame_readiness_repr(self),
            )
            self.firstFrameRendered.emit()

    def setAutoFillBackground(self, enabled) -> None:  # noqa: N802 — Qt API
        # Intentionally ignore — see __init__ comment. Image compare does the same.
        return

    def resizeEvent(self, event) -> None:  # noqa: D401 — Qt signature
        super().resizeEvent(event)
        # Skip no-op geometry churn and any resize while the color buffer is
        # pinned for a popup/CSD interaction (context menu, window drag).
        if event.size() == event.oldSize():
            return
        if self._color_buffer_frozen or self.is_color_buffer_frozen():
            return
        mc_first_frame_debug(self, "resizeEvent -> %s", event.size())
        self.request_view_update()

    def showEvent(self, event) -> None:  # noqa: N802 — Qt signature
        super().showEvent(event)
        mc_first_frame_debug(self, "showEvent (canvas visible)")
        self._start_first_frame_sampler()
        try:
            self.flush_stale_composition()
        except Exception:
            pass
        # Mirrors image_compare's CanvasWidget.showEvent: hidden stack pages
        # never present, so on the first show force a repaint immediately and
        # schedule the compositor settle flush before the first present. The
        # flush is what makes the frame compositor-visible on Wayland/Vulkan,
        # and firstFrameRendered is only emitted after it runs (see render()),
        # so kicking it from showEvent as well keeps the startup placeholder
        # up until the first genuinely visible frame.
        self.request_view_update()
        if self._rhi_presents_completed < _FIRST_PRESENT_SETTLE_COUNT:
            QTimer.singleShot(0, self._settle_first_presents)
        if self._rhi_presents_completed == 0:
            # Cold canvas: this single update() can vanish pre-exposure
            # without ever reaching render()/initialize(). Nudge until
            # frames flow so the first DnD doesn't pay cold-init as lag.
            self._nudge_init_retry()

    def _start_first_frame_sampler(self) -> None:
        """Periodically log what the canvas region actually shows on screen.

        Catches the "transparent first frame" that the present timeline can't:
        the compositor may keep showing the untouched (transparent) subsurface
        even while RHI presents are being recorded. Samples every ~50 ms for
        the first ~1.5 s after show, reporting Qt-side visibility/exposure and
        the opaque fraction of a corner grab.
        """
        if not mc_first_frame_debug_enabled():
            return
        if getattr(self, "_ffd_sampler_started", False):
            return
        self._ffd_sampler_started = True
        self._ffd_sampler_ticks = 0
        from tabs.multi_compare.first_frame_debug import (
            mc_first_frame_surface_repr,
        )

        def _probe_top_level_rhi_windows():
            """List every top-level QRhiWidget window + their translucency.

            "Прозрачное qrhi окно" that appears independently of the present
            gate is a separate top-level widget (e.g. an offscreen export
            canvas whose WA_DontShowOnScreen isn't honored by the compositor),
            not the live canvas — this pinpoints which one it is.
            """
            try:
                from PySide6.QtWidgets import QApplication, QRhiWidget

                app = QApplication.instance()
                if app is None:
                    return
                for top in app.topLevelWidgets():  # ALLOWED: debug probe — enumerates top-level QRhiWidgets generically, not tab-specific
                    if not isinstance(top, QRhiWidget):
                        continue
                    mc_first_frame_debug(
                        self,
                        "top-level QRhiWidget name=%r visible=%s translucent=%s "
                        "dont_show=%s geom=%s",
                        getattr(top, "objectName", lambda: "")() or "<anon>",
                        top.isVisible(),
                        bool(
                            top.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
                        ),
                        bool(top.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)),
                        top.geometry(),
                    )
            except Exception:
                pass

        def _probe_placeholder():
            """Whether the startup placeholder fully covers the canvas and is
            actually opaque — if it's transparent or smaller than the canvas,
            the user sees the unrendered (transparent) surface through it."""
            try:
                owner = None
                node = self
                for _ in range(6):
                    node = node.parentWidget()
                    if node is None:
                        break
                    if hasattr(node, "_startup_placeholder"):
                        owner = node
                        break
                if owner is None:
                    mc_first_frame_debug(self, "placeholder probe: owner not found")
                    return
                placeholder = owner._startup_placeholder
                bg = getattr(placeholder, "_bg_color", None)
                bg_desc = (
                    f"{bg.name()} a={bg.alpha()}"
                    if bg is not None and bg.isValid()
                    else "invalid"
                )
                covers = (
                    placeholder.isVisible()
                    and placeholder.geometry().contains(self.geometry())
                )
                mc_first_frame_debug(
                    self,
                    "placeholder probe vis=%s covers_full=%s bg=%s "
                    "placeholder_geom=%s canvas_geom=%s",
                    placeholder.isVisible(),
                    covers,
                    bg_desc,
                    placeholder.geometry(),
                    self.geometry(),
                )
            except Exception:
                pass

        _probe_top_level_rhi_windows()
        _probe_placeholder()

        def _tick():
            self._ffd_sampler_ticks += 1
            mc_first_frame_debug(
                self,
                "sampler presents=%s [%s]",
                self._rhi_presents_completed,
                mc_first_frame_surface_repr(self),
            )
            if self._ffd_sampler_ticks < 30:
                QTimer.singleShot(50, _tick)

        QTimer.singleShot(50, _tick)

    def set_dispatch(self, dispatch) -> None:
        """Install the redux dispatch callable used for interaction-driven changes."""
        self._dispatch = dispatch

    def _do_dispatch(self, action: MultiCompareAction) -> None:
        if self._dispatch is not None:
            self._dispatch(action)

    def _slot_sources(self) -> dict[int, object]:
        """Resolved pixel sources for the current state (B1).

        One ``cache.resolve`` per slot with a path — pixel tier first, then
        preview, imageless slots omitted. Closed/evicted stores never
        surface here (the cache validates before returning).

        Parametric-crop clip (lane mc-crop-render): each slot is passed
        through ``residency.cropped_source_for_slot`` with this canvas's
        controller, so cropped slots resolve to a translating crop view
        (cropped size into grid/LOD/fit, clipped reads) while uncropped
        slots keep their real source byte-identical. An empty crop
        intersection resolves to ``None`` and is omitted — the slot
        renders gap (fail-closed, never neighbor bytes).
        """
        from tabs.multi_compare.pipeline.cache import resolve_slot_source
        from tabs.multi_compare.scene.passes import residency as _residency

        controller = _residency.controller_for_canvas(self)
        sources: dict[int, object] = {}
        for slot in self.state.slots:
            sid = int(slot.id)
            source = resolve_slot_source(self.pixel_cache, slot)
            if source is None:
                self._crop_clip_views.pop(sid, None)
                continue
            clipped = _residency.cropped_source_for_slot(
                source, controller, slot, cache=self._crop_clip_views
            )
            if clipped is None or clipped is source:
                # Gap (empty intersection, omitted) or no crop: no live
                # view for this sid -- drop any stale cached view so a
                # later toggle re-resolves instead of reusing it.
                self._crop_clip_views.pop(sid, None)
                if clipped is None:
                    continue
            sources[sid] = clipped
        if self._crop_clip_views:
            live = set(sources)
            for dead in [sid for sid in self._crop_clip_views if sid not in live]:
                self._crop_clip_views.pop(dead, None)
        return sources

    def _rebuild_composition(self) -> None:
        """Build the CompositionPlan from state and apply it to ``self``.

        ``_active_composition`` ends up holding a ``ResolvedComposition`` —
        the flat list of textured-quad layers in canvas-px that ``render()``
        consumes. Canvas size = native canvas (computed from image extents)
        so live and export share the same canon; ``sr = min(fb/canvas)`` is
        applied once in ``render()`` to project canvas-px into framebuffer-px.
        """
        from tabs.multi_compare.services.composition_builder import (
            build_composition_plan,
        )
        from ui.canvas_presentation.composition import resolve_composition

        plan = build_composition_plan(self.state, sources=self._slot_sources())
        if plan is None:
            self._active_composition = None
            return
        self._active_composition = resolve_composition(plan)

    # --- drop / hit projection (thin wrappers for features + tests) ---

    def compute_drop_target(
        self, pos: QPoint, *, include_center: bool = False
    ) -> tuple[tuple[int, ...] | None, str | None, bool, int | None]:
        return drop_targets.compute_drop_target(
            self, pos, include_center=include_center
        )

    def _drop_target_for_gap(
        self, pos: QPoint
    ) -> tuple[tuple[int, ...], str, bool, None] | None:
        return drop_targets.drop_target_for_gap(self, pos)

    def _canvas_layout(self) -> tuple[int, int, float, float, float] | None:
        return hit_projection.canvas_layout(self)

    def _drop_gaps(self):
        return hit_projection.drop_gaps(self)

    def _leaf_paths_and_rects(self):
        return hit_projection.leaf_paths_and_rects(self)

    def _node_rect_at_path(self, path: tuple[int, ...]) -> QRect | None:
        return hit_projection.node_rect_at_path(self, path)

    def _leaf_rects(self) -> list[tuple[LeafNode, QRect]]:
        return hit_projection.leaf_rects(self)

    # --- textures / RHI ---

    def upload_image(self, slot: CompareSlot) -> None:
        from tabs.multi_compare.pipeline.cache import resolve_slot_source

        source = resolve_slot_source(self.pixel_cache, slot)
        if source is None:
            return
        self.upload_pixel_source(slot.id, source)

    def upload_pixel_source(self, slot_id: int, source) -> None:
        self._renderer.queue_upload(slot_id, source)
        self.update()

    def remove_texture(self, slot_id: int) -> None:
        self._renderer.queue_remove(slot_id)
        self.update()

    def _sync_textures(self) -> None:
        """Reconcile GPU textures with all available image sources.

        Union of ``state.slots`` (live data ownership — includes hidden slots
        during focused mode) and ``_active_composition.layers`` (export path
        when ``state`` is empty). Slot pixels resolve from the session cache
        (B1); texture eviction tracks the union so toggling
        focus never evicts a still-loaded slot.
        """
        sources: dict[int, object] = self._slot_sources()
        try:
            import logging as _dl, time as _dt
            _now = _dt.monotonic()
            if _now - getattr(self, "_dbg_sync", 0.0) > 1.0:
                self._dbg_sync = _now
                _dl.getLogger("mc.tex.debug").warning(
                    "MCSYNC sources=%r" % ({k: type(v).__name__ for k, v in sources.items()},)
                )
        except Exception:
            pass
        if self._active_composition is not None:
            for layer in self._active_composition.layers:
                if layer.image is not None:
                    sources.setdefault(int(layer.layer_id), layer.image)

        for sid, source in sources.items():
            if (
                not self._renderer.has_slot_texture(sid)
                or self._renderer.slot_texture_source(sid) is not source
            ):
                self.upload_pixel_source(sid, source)
        stale = [sid for sid in self._renderer.slot_texture_ids() if sid not in sources]
        for sid in stale:
            self.remove_texture(sid)

    def initialize(self, command_buffer) -> None:
        mc_first_frame_debug(self, "initialize() renderer init starts")
        self._renderer.initialize(command_buffer)
        if getattr(self._renderer, "initialized", False):
            self._init_retry_count = 0
            mc_first_frame_debug(self, "initialize() renderer ready")
            return
        # Init aborted (rhi/renderTarget not realized yet) — retry shortly.
        # Otherwise a cold canvas sits at presents==0 until the first DnD
        # drives a frame, and that first drag-driven frame pays the full
        # cold-init cost (~160ms GUI stall → visible lag + frozen DnD cursor
        # while the event loop is stuck compiling pipelines). Bounded: gives
        # up after ~5s; the drag path still initializes as fallback.
        # (image_compare never hits this: continuous repaints init at startup.)
        mc_first_frame_debug(self, "initialize() deferred (not realized), retry scheduled")
        self._nudge_init_retry()

    def _nudge_init_retry(self) -> None:
        """Schedule one more paint attempt while the renderer is cold.

        Shared budget with ``initialize()`` (~5s): gives up on persistently
        broken surfaces instead of update-spamming forever. Hidden widgets
        never reach ``render()``/``initialize()``, so the chain self-stops
        off-screen by construction.
        """
        if getattr(self._renderer, "initialized", False):
            self._init_retry_count = 0
            return
        retries = getattr(self, "_init_retry_count", 0)
        if retries < 50:
            self._init_retry_count = retries + 1
            QTimer.singleShot(100, self._schedule_init_retry)

    def _schedule_init_retry(self) -> None:
        try:
            if not getattr(self._renderer, "initialized", False):
                self.update()
        except Exception:
            pass

    def releaseResources(self) -> None:
        self._renderer.release()
        self._composition_flush.cancel()

    def apply_theme_background(self, color: QColor | None = None) -> None:
        bg = QColor(color) if isinstance(color, QColor) and color.isValid() else None
        if bg is None:
            bg = self.palette().color(QPalette.ColorRole.Window)
            if not bg.isValid():
                bg = self.palette().color(QPalette.ColorRole.Base)
            if not bg.isValid():
                bg = QColor(30, 30, 30)
        if not getattr(self, "_allow_transparent_clear", False):
            bg.setAlpha(255)
        palette = self.palette()
        palette.setColor(self.backgroundRole(), bg)
        palette.setColor(QPalette.ColorRole.Window, bg)
        palette.setColor(QPalette.ColorRole.Base, bg)
        self.setPalette(palette)
        self._theme_background_color = QColor(bg)

    def _theme_or_palette_bg(self) -> QColor:
        bg = getattr(self, "_theme_background_color", None)
        if not isinstance(bg, QColor) or not bg.isValid():
            bg = self.palette().color(QPalette.ColorRole.Window)
        if not bg.isValid():
            bg = QColor(30, 30, 30)
        if not getattr(self, "_allow_transparent_clear", False):
            bg.setAlpha(255)
        return bg

    # --- chrome input (stubs → canvas/interaction.py) ---

    def wheelEvent(self, event: QWheelEvent) -> None:
        canvas_interaction.handle_wheel_event(self, event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        canvas_interaction.handle_mouse_press_event(self, event)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        canvas_interaction.handle_context_menu_event(self, event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        canvas_interaction.handle_mouse_move_event(self, event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        canvas_interaction.handle_mouse_release_event(self, event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        canvas_interaction.handle_mouse_double_click_event(self, event)

    def keyPressEvent(self, event) -> None:
        canvas_interaction.handle_key_press_event(self, event)

    def _start_internal_drag(self, slot_id: int) -> None:
        canvas_interaction.start_internal_drag(self, slot_id)

    def _leaf_at(self, pos: QPoint, leaf_rects) -> tuple[LeafNode, QRect] | None:
        return canvas_interaction.leaf_at(pos, leaf_rects)