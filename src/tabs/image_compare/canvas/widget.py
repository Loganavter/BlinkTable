# Audit-Meta: pattern=thin-owner reason="Canvas widget thin owner — delegates to interaction/render_context/features"
from PySide6.QtCore import QCoreApplication, QPoint, QPointF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QContextMenuEvent, QImage, QPixmap, QResizeEvent
from PySide6.QtWidgets import QRhiWidget

# QTimer stays a module-level name: the first-frame tests patch
# ``widget_mod.QTimer.singleShot``; the code that fires it lives in
# canvas/first_frame.py after the Part D thin-owner split.
from PySide6.QtCore import QTimer  # noqa: F401

from tabs.image_compare.first_frame_debug import (
    ic_first_frame_debug,
)

from .feature_overlay_gpu import (
    clear_feature_overlay_gpu,
    set_feature_overlay_content,
    set_feature_overlay_gpu_params,
    upload_feature_overlay_crop,
)
from .interaction import (
    handle_key_press_event,
    handle_key_release_event,
    handle_leave_event,
    handle_mouse_move_event,
    handle_mouse_press_event,
    handle_mouse_release_event,
    handle_wheel_event,
)
from .interaction import reset_view as reset_view_impl
from .interaction import set_capture_area as set_capture_area_impl
from .interaction import set_capture_color as set_capture_color_impl
from .interaction import set_guides_params as set_guides_params_impl
from .interaction import set_overlay_coords as set_overlay_coords_impl
from .interaction import set_pan as set_pan_impl
from .interaction import set_zoom as set_zoom_impl
from .render_context import (
    begin_update_batch,
    end_update_batch,
    resize_canvas,
    schedule_source_preload,
)
from ui.canvas_infra.rhi.rhi_backend import configure_rhi_widget
from ui.canvas_infra.rhi.rhi_render import render_clear_frame
from ui.canvas_presentation.plan import plan_owns_overlay_state
from engine.gpu.coalesced_flush import CoalescedFlush
from engine.gpu.glass_panel import GlassPanelRegistry
from .rhi_renderer import RhiCanvasRenderer
from .scene import build_render_scene
from .state import CanvasRuntimeState, init_widget_state
from .texture_parts.base_images import (
    configure_offscreen_render,
    get_letterbox_params,
    upload_diff_source_pil_image,
    upload_image,
    upload_pil_images,
)
from .texture_parts.layers import clear as clear_textures_and_layers
from .texture_parts.layers import (
    set_background,
    set_layers,
    set_pil_layers,
    set_pixmap,
)

from engine.cache.first_frame_gate import (
    first_visual_present_count as _first_visual_present_count,
)
from engine.frame.present import PresentDebt, begin_source

# Cluster bodies live in sibling modules (Part D thin-owner split).
# Names that were module-level here stay importable off this module: the
# first-frame tests (`tests/render/test_ic_first_frame_gate.py`) monkeypatch
# ``widget_mod.render_clear_frame`` / ``widget_mod._first_visual_present_count``
# / ``widget_mod.QTimer``, and the drag-restack tests call
# ``wmod._schedule_drag_restack`` and read ``wmod._DND_RESTACK_DELAY_MS``.
from . import context_menu_slots, first_frame
from .features.drag_drop_overlay import keepwarm
from .features.drag_drop_overlay.keepwarm import (  # noqa: F401 — test surface
    _DND_RESTACK_DELAY_MS,
    _schedule_drag_restack,
)


class CanvasWidget(QRhiWidget):
    mousePressed = Signal(object)
    mouseMoved = Signal(object)
    mouseReleased = Signal(object)
    wheelScrolled = Signal(object)
    zoomChanged = Signal(float)
    keyPressed = Signal(object)
    keyReleased = Signal(object)
    firstFrameRendered = Signal()
    firstVisualFrameReady = Signal()

    _alignment = Qt.AlignmentFlag.AlignCenter

    def __init__(self, parent=None):
        super().__init__(parent)
        configure_rhi_widget(self)
        self._first_frame_rendered_emitted = False
        self._rhi_presents_completed = 0
        self._present_debt: PresentDebt = begin_source(None)
        # DnD show→visible latency probes (env-gated logs only, see
        # set_drag_overlay_state/render — prod-silent without IMGSLI_IC_DEBUG).
        self._dnd_show_t0: float | None = None
        self._dnd_first_present_t: float | None = None
        self._rhi_renderer = RhiCanvasRenderer()
        # Registered/unregistered by GlassHUD instances anchored to this
        # canvas (see ui/widgets/glass_hud/hud.py) -- consumed by
        # RhiCanvasRenderer.render(), which populates
        # self._glass_panel_sprites for each GlassHUD's own
        # GlassPanelDisplayWidget to read.
        self.glass_panels = GlassPanelRegistry()
        self.runtime_state: CanvasRuntimeState
        self._context_menu_provider = None
        self._render_scene_flush = CoalescedFlush(self._flush_render_scene)
        init_widget_state(self)
        # Tracks the device size (widget size * DPR) resizeEvent last
        # committed to the GPU-rendering machinery -- see paintEvent's
        # stale-device-size self-heal below (docs/dev/investigations/
        # image-compare-first-activation-flicker-and-qrhi.md Bug 3
        # follow-up, ported from Telegram Desktop's lib_ui gl_surface.cpp).
        self._device_size = QSize()
        ic_first_frame_debug(self, "canvas constructed")

    def set_store(self, store):
        state = self.runtime_state
        state._store = store
        state._render_scene = build_render_scene(
            store, apply_channel_mode_in_shader=state._apply_channel_mode_in_shader
        )
        ic_first_frame_debug(self, "set_store plan=%s", state._render_scene is not None)
        if hasattr(store, "on_change"):
            store.on_change(lambda scope: self._refresh_render_scene())

    def set_context_menu_provider(self, provider) -> None:
        self._context_menu_provider = provider
        pending_ctrl = getattr(self, "_pending_session_controller", None)
        if pending_ctrl is not None and hasattr(provider, "attach_session_controller"):
            provider.attach_session_controller(pending_ctrl)
            self._pending_session_controller = None

    def _refresh_render_scene(self):
        self._render_scene_flush.request()
        self.update()

    def _flush_render_scene(self):
        state = self.runtime_state
        if state._store is None:
            return
        state._render_scene = build_render_scene(
            state._store,
            apply_channel_mode_in_shader=state._apply_channel_mode_in_shader,
        )
        plan = getattr(self, "_active_render_plan", None)
        # A plan that carries its overlay_layout owns that overlay state
        # (recorded snapshot / video-preview plan). This flush must not
        # re-derive it from the live store: that path replaces the plan and,
        # when it cannot build a layout, nulls the overlay and resets the
        # canvas. Same rule as the engine plan applicator and the magnifier
        # live rebuild (PR #83, ``plan_owns_overlay_state``); a plan WITHOUT an
        # overlay layout keeps the live recompute exactly as before.
        if plan is not None and not plan_owns_overlay_state(plan):
            from tabs.image_compare.canvas.registry import registry

            registry().apply_feature_live_runtime_overlays(state._store, self)
        self.update()

    def set_apply_channel_mode_in_shader(self, enabled: bool):
        state = self.runtime_state
        state._apply_channel_mode_in_shader = enabled
        if state._store is not None:
            state._render_scene = build_render_scene(
                state._store,
                apply_channel_mode_in_shader=state._apply_channel_mode_in_shader,
            )
        self.update()

    def set_render_scene(self, scene):
        self.runtime_state._render_scene = scene
        self.update()

    def set_split_position_sync(self, sync_callback):
        self.runtime_state._split_position_sync = sync_callback

    def set_drag_overlay_state(
        self,
        visible: bool,
        horizontal: bool = False,
        text1: str = "",
        text2: str = "",
    ):
        keepwarm.set_drag_overlay_state(self, visible, horizontal, text1, text2)

    def is_drag_overlay_visible(self) -> bool:
        return bool(self.runtime_state._drag_overlay_visible)

    def showEvent(self, event):
        super().showEvent(event)
        first_frame.show_event(self)

    def _start_first_frame_sampler(self) -> None:
        first_frame.start_first_frame_sampler(self)

    def _ensure_dnd_keepwarm(self) -> None:
        keepwarm._ensure_dnd_keepwarm(self)

    def resizeEvent(self, event):
        # lib_ui's gl_surface.cpp guard: refuse to forward a resize into
        # GPU-rendering machinery before the top-level's native window
        # exists at all (see docs/dev/investigations/
        # image-compare-first-activation-flicker-and-qrhi.md Bug 3
        # follow-up). Qt's own geometry bookkeeping for this widget is
        # already done by the time the event is dispatched, so skipping
        # here loses nothing -- the next resizeEvent once the window handle
        # exists will pick up the current size.
        top = self.window()
        if top is None or top.windowHandle() is None:
            return
        state = self.runtime_state
        state._drag_overlay_cache_key = None
        state._drag_overlay_cached_image = None
        super().resizeEvent(event)
        size = event.size()
        ic_first_frame_debug(self, "resizeEvent -> %s", size)
        self._device_size = size * self.devicePixelRatio()
        resize_canvas(self, size.width(), size.height())

    def paintEvent(self, event):
        # lib_ui's gl_surface.cpp guard: self-heal the "stuck at old size"
        # symptom directly -- if the widget's current size*DPR has drifted
        # from what resizeEvent last committed to the GPU-rendering
        # machinery (e.g. a resize was skipped above, or landed before the
        # RHI surface was ready), re-post a synthetic resize instead of
        # painting a frame at the stale size. See docs/dev/investigations/
        # image-compare-first-activation-flicker-and-qrhi.md Bug 3
        # follow-up.
        expected = self.size() * self.devicePixelRatio()
        if self._device_size != expected and expected.width() > 0 and expected.height() > 0:
            self._device_size = expected
            QCoreApplication.postEvent(
                self, QResizeEvent(self.size(), self.size())
            )
            self.update()
            return
        super().paintEvent(event)

    def set_session_controller(self, session_controller) -> None:
        if self._context_menu_provider is not None:
            self._context_menu_provider.attach_session_controller(session_controller)
        else:
            self._pending_session_controller = session_controller

    def initialize(self, command_buffer):
        ic_first_frame_debug(self, "initialize() renderer init starts")
        self._rhi_renderer.initialize(self, command_buffer)
        ic_first_frame_debug(self, "initialize() renderer ready")

    def releaseResources(self):
        self._rhi_renderer.release()

    def upload_image(self, qimage: QImage, slot_index: int):
        return upload_image(self, qimage, slot_index)

    def upload_pil_images(
        self,
        pil_image1,
        pil_image2,
        source_image1=None,
        source_image2=None,
        source_key=None,
        display_cache_key=None,
        shader_letterbox: bool = False,
    ):
        return upload_pil_images(
            self,
            pil_image1,
            pil_image2,
            source_image1,
            source_image2,
            source_key,
            display_cache_key,
            shader_letterbox,
        )

    def render(self, command_buffer):
        # Match Multi Compare: only treat a completed beginPass/endPass as a
        # real present (see first_frame.note_present for the full gating
        # rationale). Emitting on a no-op (target/rhi still None) made the
        # startup cover drop onto an empty D3D swapchain buffer on Windows.
        painted = render_clear_frame(self, command_buffer)
        if not painted:
            ic_first_frame_debug(
                self, "render() returned NOT-painted (engine not ready yet)"
            )
            return
        first_frame.note_present(self)

    def _settle_first_presents(self) -> None:
        first_frame.settle_first_presents(self)

    def _emit_first_frame_if_ready(self) -> None:
        first_frame.emit_first_frame_if_ready(self, _first_visual_present_count())

    def _request_update(self):
        first_frame.request_update_proxy(self)

    def _schedule_source_preload(self):
        schedule_source_preload(self)

    def begin_update_batch(self):
        begin_update_batch(self)

    def end_update_batch(self):
        end_update_batch(self)

    def set_split_pos(self, pos: float):
        self.split_position = pos
        self._request_update()

    def set_background(self, pixmap: QPixmap | None):
        return set_background(self, pixmap)

    def set_pixmap(self, pixmap: QPixmap | None):
        return set_pixmap(self, pixmap)

    def set_feature_overlay_content(
        self, pixmap: QPixmap | None, top_left: QPoint | None
    ):
        return set_feature_overlay_content(self, pixmap, top_left)

    def get_letterbox_params(self, slot: int = 0) -> tuple:
        return get_letterbox_params(self, slot)

    def set_feature_overlay_gpu_params(
        self,
        slots: list[dict | None],
        channel_mode: int = 0,
        diff_mode: int = 0,
        diff_threshold: float = 20.0 / 255.0,
        border_color: QColor | None = None,
        border_width: float = 2.0,
        interp_mode: int = 1,
    ):
        return set_feature_overlay_gpu_params(
            self,
            slots,
            channel_mode,
            diff_mode,
            diff_threshold,
            border_color,
            border_width,
            interp_mode,
        )

    def clear_feature_overlay_gpu(self):
        return clear_feature_overlay_gpu(self)

    def upload_feature_overlay_crop(
        self,
        pil_image,
        center: QPointF,
        radius: float,
        border_color: QColor | None = None,
        border_width: float = 2.0,
        index: int = 0,
        canvas_filter: int | None = None,
    ):
        return upload_feature_overlay_crop(
            self,
            pil_image,
            center,
            radius,
            border_color,
            border_width,
            index,
            canvas_filter,
        )

    def upload_diff_source_pil_image(self, pil_image):
        return upload_diff_source_pil_image(self, pil_image)

    def configure_offscreen_render(
        self,
        *,
        stored_images,
        source_images,
        content_rect: tuple[int, int, int, int],
        shader_letterbox: bool = False,
    ):
        return configure_offscreen_render(
            self,
            stored_images=stored_images,
            source_images=source_images,
            content_rect=content_rect,
            shader_letterbox=shader_letterbox,
        )

    def set_overlay_coords(
        self,
        capture_center: QPointF | None,
        capture_radius: float,
        overlay_centers: list[QPointF],
        overlay_radius: float,
    ):
        set_overlay_coords_impl(
            self, capture_center, capture_radius, overlay_centers, overlay_radius
        )

    def set_guides_params(self, visible: bool, color: QColor, thickness: int):
        set_guides_params_impl(self, visible, color, thickness)

    def set_capture_color(self, color: QColor):
        set_capture_color_impl(self, color)

    def set_capture_area(
        self, center: QPoint | None, size: int, color: QColor | None = None
    ):
        set_capture_area_impl(self, center, size, color)

    def set_layers(
        self,
        background: QPixmap | None,
        overlay: QPixmap | None,
        overlay_pos: QPoint | None,
        coords_snapshot: tuple | None = None,
    ):
        return set_layers(self, background, overlay, overlay_pos, coords_snapshot)

    def set_pil_layers(
        self,
        pil_image1=None,
        pil_image2=None,
        overlay=None,
        overlay_pos=None,
        source_image1=None,
        source_image2=None,
        source_key=None,
        display_cache_key=None,
        shader_letterbox: bool = False,
    ):
        return set_pil_layers(
            self,
            pil_image1,
            pil_image2,
            overlay,
            overlay_pos,
            source_image1,
            source_image2,
            source_key,
            display_cache_key,
            shader_letterbox,
        )

    def setPixmap(self, pixmap: QPixmap | None):
        return set_pixmap(self, pixmap)

    def clear(self):
        return clear_textures_and_layers(self)

    def setAlignment(self, alignment):
        self._alignment = alignment

    def alignment(self):
        return self._alignment

    def set_read_only(self, enabled: bool):
        self.runtime_state._read_only = bool(enabled)
        if enabled:
            from ui.canvas_infra.viewport.state import set_pan_offsets, set_zoom_level

            set_zoom_level(self, 1.0)
            set_pan_offsets(self, 0.0, 0.0)
            self.update()

    def is_read_only(self) -> bool:
        return bool(getattr(self.runtime_state, "_read_only", False))

    def setAutoFillBackground(self, enabled):

        pass

    def setFocusPolicy(self, policy):
        super().setFocusPolicy(policy)

    def setAttribute(self, attribute, on=True):
        super().setAttribute(attribute, on)

    def contentsRect(self):
        return self.rect()

    def set_zoom(self, zoom: float):
        set_zoom_impl(self, zoom)

    def set_pan(self, x: float, y: float):
        set_pan_impl(self, x, y)

    def reset_view(self):
        reset_view_impl(self)

    def wheelEvent(self, event):
        handle_wheel_event(self, event)

    def mousePressEvent(self, event):
        handle_mouse_press_event(self, event)

    def contextMenuEvent(self, event: QContextMenuEvent):
        context_menu_slots.context_menu_event(self, event)

    def _active_session_type(self) -> str | None:
        return context_menu_slots.active_session_type(self)

    def _context_menu_slot_at(self, pos):
        return context_menu_slots.context_menu_slot_at(self, pos)

    def mouseReleaseEvent(self, event):
        handle_mouse_release_event(self, event)

    def mouseMoveEvent(self, event):
        handle_mouse_move_event(self, event)

    def keyPressEvent(self, event):
        # The tab shell (``ImageCompareWidget``) is the focus owner and
        # forwards keys here; this widget itself stays ``NoFocus`` (see
        # ``ui/canvas_infra/rhi/rhi_focus.py``). Pan/zoom chords are accepted
        # here; anything else is emitted on ``keyPressed`` so the shell can
        # hand it back to the global handler.
        handle_key_press_event(self, event)

    def keyReleaseEvent(self, event):
        handle_key_release_event(self, event)

    def leaveEvent(self, event):
        handle_leave_event(self, event)


def get_canvas_widget_class():
    return CanvasWidget