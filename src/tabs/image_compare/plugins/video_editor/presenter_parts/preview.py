# Audit-Meta: pattern=state-machine reason="PreviewCoordinator single pipeline schedule->GPU->worker->apply with render_task_id race-guard"
import logging

from core.tracing import Tracer
from PIL import Image
import shiboken6 as sip
from PySide6.QtCore import QThreadPool, QTimer
from PySide6.QtGui import QImage, QPixmap
from tabs.image_compare.plugins.video_editor.services.keyframing.engine.values import (
    frozen_value,
    viewport_fingerprint,
)
from sli_ui_toolkit.managers import SettleGate
from sli_ui_toolkit.workers import GenericWorker
from ui.canvas_presentation import apply_canvas_render_plan
from core.log_names import APP_LOGGER_NAME

logger = logging.getLogger(APP_LOGGER_NAME)
_vplog = logging.getLogger(f"{APP_LOGGER_NAME}.video_preview")


class PreviewCoordinator:
    def __init__(
        self,
        view,
        export_controller,
        playback_engine,
        model,
        editor_service,
        timer_parent,
        emit_preview_ready,
        emit_fit_content_available=None,
    ):
        self.view = view
        self.export_controller = export_controller
        self.playback_engine = playback_engine
        self.model = model
        self.editor_service = editor_service
        self.emit_preview_ready = emit_preview_ready
        self.emit_fit_content_available = emit_fit_content_available

        self._view_destroyed = False
        self._is_rendering_preview = False
        self._render_task_id = 0
        self._preview_ready_emitted = False
        self._last_render_params = (None, None, None)
        self.fit_content_mode = False
        self.preview_render_scale = 1.0
        # No local bounds mirror: the VideoExporterService owns the global
        # bounds cache (peek/store/invalidate); read via _get_service_bounds().
        self._bounds_request_id: int = 0
        self._stored_crop_resolution = None

        # ``prepare_key`` / ``plan`` / ``store`` / optional ``frame_pil``.
        self._preview_frame_cache = None

        # A cache miss (new render size — the common case mid-resize-drag)
        # needs prepare_snapshot_canvas_frame(), which decodes/resamples the
        # source images — hundreds of ms for 4K sources. Runs here instead of
        # inline on the GUI thread so a resize drag doesn't stall the UI; see
        # thumbnails.py's identical rationale for its own pool.
        self._prepare_pool = QThreadPool(timer_parent)
        self._prepare_pool.setMaxThreadCount(1)

        self._preview_updater = QTimer(timer_parent)
        self._preview_updater.setSingleShot(True)
        self._preview_updater.setInterval(1)
        self._preview_updater.timeout.connect(self.do_update_preview_heavy)

        # Cheap refit on every pulse; full prepare/GPU after quiet period.
        self._resize_settle = SettleGate(
            on_settle=self._on_resize_gpu_settled,
            on_pulse=self._refit_cached_preview_to_widget,
            interval_ms=SettleGate.DEFAULT_INTERVAL_MS,
            parent=timer_parent,
        )

    def detach_view(self):
        self.view = None

    def on_view_destroyed(self, *_args):
        self._view_destroyed = True
        self._preview_updater.stop()
        self._resize_settle.cancel()
        self.view = None

    def has_live_view(self) -> bool:
        return self.view is not None and sip.isValid(self.view)

    def get_preview_size_safe(self) -> tuple[int, int]:
        if not self.has_live_view():
            return (0, 0)
        try:
            return self.view.get_preview_size()
        except RuntimeError:
            return (0, 0)

    def schedule_update(self):
        if self.has_live_view():
            self._preview_updater.start()

    def reset_render_state(self):
        """Single invalidation path for the preview frame cache.

        ``prepare_key``/``plan``/``store``/``request_key``/``frame_pil`` are
        only ever dropped here (and at teardown in ``cleanup``); resize,
        bounds, and scale changes flow through this, never ad-hoc clears.
        """
        self._last_render_params = (None, None, None)
        self._preview_frame_cache = None

    def _get_service_bounds(self):
        """Single-source read of the cached global bounds (service-owned).

        Returns ``None`` when no exporter is wired or the background
        recalculation has not published yet — callers wait/recalculate.
        """
        exporter = getattr(self.export_controller, "video_exporter", None)
        if exporter is None:
            return None
        peek = getattr(exporter, "peek_cached_global_bounds", None)
        if callable(peek):
            try:
                return peek()
            except Exception:
                return None
        return getattr(exporter, "_cached_global_bounds", None)

    @staticmethod
    def _build_preview_keys(
        snapshot_signature,
        render_w: int,
        render_h: int,
        fit_content_mode: bool,
        fill_color_tuple,
        global_bounds,
        preview_w: int,
        preview_h: int,
    ):
        """Single construction site for the preview cache keys.

        ``prepare_key`` is the only cache identity (same render buffer can be
        re-letterboxed into a resized pane without re-prepare); ``request_key``
        extends it with the widget display size for the canvas source/display
        keys downstream — never a second lookup scheme.
        """
        # Prepare key ignores widget display size: same render buffer can be
        # re-letterboxed into a resized preview pane without re-prepare.
        prepare_key = (
            snapshot_signature,
            render_w,
            render_h,
            fit_content_mode,
            fill_color_tuple,
            global_bounds,
        )
        request_key = (
            prepare_key,
            preview_w,
            preview_h,
        )
        return prepare_key, request_key

    def _clip_overlays_for_mode(self) -> bool:
        return not self.fit_content_mode

    def _refit_cached_preview_to_widget(self) -> bool:
        """Cheap path: re-letterbox last plan into the current widget size.

        Textures stay uploaded; ``apply_canvas_render_plan`` takes the scene-only
        branch when display keys match. Same idea as export dialog scaling a
        baked pixmap on resize.
        """
        cache = self._preview_frame_cache or {}
        plan = cache.get("plan")
        if plan is None:
            frame_pil = cache.get("frame_pil")
            request_key = cache.get("request_key")
            if frame_pil is not None and request_key is not None:
                self._apply_preview_frame(frame_pil, request_key)
                return True
            return False
        canvas = getattr(self.view, "preview_label", None)
        if canvas is None:
            return False
        apply_canvas_render_plan(
            canvas,
            plan,
            store=cache.get("store"),
            clip_overlays_to_image_bounds=self._clip_overlays_for_mode(),
        )
        update = getattr(canvas, "update", None)
        if callable(update):
            update()
        return True

    def _on_resize_gpu_settled(self):
        if not self.has_live_view():
            return
        if self.playback_engine.is_playing():
            return
        # Allow size-change detection without dropping the prepare cache.
        frame, _, _ = self._last_render_params
        self._last_render_params = (frame, None, None)
        self.schedule_update()

    def do_update_preview_heavy(self):
        if self._is_rendering_preview or not self.has_live_view():
            _vplog.debug(
                "preview_skip busy=%s live=%s",
                self._is_rendering_preview,
                self.has_live_view(),
            )
            return

        available_size = self.get_preview_size_safe()
        if not available_size or available_size[0] < 10 or available_size[1] < 10:
            _vplog.debug("preview_skip size=%s", available_size)
            return

        if self.fit_content_mode and self._get_service_bounds() is None:
            self.recalculate_global_bounds()
            _vplog.debug(
                "preview_wait_global_bounds request_id=%s",
                self._bounds_request_id,
            )
            return

        current_frame = self.playback_engine.get_current_frame()
        render_w, render_h = available_size
        last_frame, last_w, last_h = self._last_render_params
        if last_frame == current_frame and last_w and last_h:
            if abs(render_w - last_w) < 2 and abs(render_h - last_h) < 2:
                _vplog.debug(
                    "preview_skip_unchanged frame=%s size=%sx%s",
                    current_frame,
                    render_w,
                    render_h,
                )
                return

        self._is_rendering_preview = True
        self._render_task_id += 1
        self._last_render_params = (current_frame, render_w, render_h)
        _vplog.debug(
            "preview_begin task=%s frame=%s preview=%sx%s fit_content=%s",
            self._render_task_id,
            current_frame,
            render_w,
            render_h,
            self.fit_content_mode,
        )

        task_id = self._render_task_id
        snap = self.editor_service.get_snapshot_at(current_frame)
        if not snap:
            # Sparse/evicted frame: drop the cached plan (it is bound to the
            # previous frame's prepare_key) and clear the surface. Returning
            # without invalidating left the neighbour's pixels on screen while
            # the playhead showed this frame, and _last_render_params kept the
            # skip check pinned to the stale render.
            self._is_rendering_preview = False
            self._preview_frame_cache = None
            self._last_render_params = (None, None, None)
            canvas = getattr(self.view, "preview_label", None)
            if canvas is not None:
                clear = getattr(canvas, "clear", None)
                if callable(clear):
                    clear()
                canvas._preview_source_key = None
                update = getattr(canvas, "update", None)
                if callable(update):
                    update()
            return

        # render_preview_gpu returns True when it finished synchronously (or
        # couldn't start) and _finish_preview_render should run now; False
        # means it submitted a background worker that will call
        # _finish_preview_render itself once the prepare completes — do NOT
        # reset _is_rendering_preview here, that would let a new resize tick
        # start rendering while this one is still in flight.
        needs_finish = True
        try:
            if self.can_render_preview_on_gpu(snap):
                needs_finish = self.render_preview_gpu(snap)
        except Exception as exc:
            logger.error(f"GPU preview render failed: {exc}", exc_info=True)
            needs_finish = True
        if needs_finish:
            self._finish_preview_render(task_id)

    def can_render_preview_on_gpu(self, snap) -> bool:
        exporter = getattr(self.export_controller, "video_exporter", None)
        return (
            self.has_live_view()
            and snap is not None
            and exporter is not None
        )

    def _resolve_fill_color_tuple(self):
        fill_color = getattr(self.view, "fit_content_fill_color", None)
        if fill_color is None:
            return None
        return (fill_color.red(), fill_color.green(), fill_color.blue(), fill_color.alpha())

    @staticmethod
    def _trace(kind: str, summary: str, payload: dict) -> None:
        if Tracer.enabled():
            Tracer.instance().record(kind, summary, payload)

    def _resolve_preview_render_size(self, preview_w: int, preview_h: int) -> tuple[int, int]:
        target_w = max(1, int(self.model.width or preview_w or 1))
        target_h = max(1, int(self.model.height or preview_h or 1))
        canvas = getattr(self.view, "preview_label", None)
        dpr = 1.0
        if canvas is not None and hasattr(canvas, "devicePixelRatioF"):
            try:
                dpr = max(1.0, float(canvas.devicePixelRatioF()))
            except Exception:
                dpr = 1.0
        preview_phys_w = max(1, int(round(float(preview_w or 1) * dpr)))
        preview_phys_h = max(1, int(round(float(preview_h or 1) * dpr)))
        scale_factor = max(0.25, min(float(self.preview_render_scale or 1.0), 1.0))

        if self.fit_content_mode:
            base_cap_w = max(1, int(preview_phys_w or target_w))
            base_cap_h = max(1, int(preview_phys_h or target_h))
        else:
            base_cap_w = max(1, max(int(preview_phys_w), min(int(preview_phys_w * 2), 1920)))
            base_cap_h = max(1, max(int(preview_phys_h), min(int(preview_phys_h * 2), 1080)))
        cap_w = max(1, int(round(base_cap_w * scale_factor)))
        cap_h = max(1, int(round(base_cap_h * scale_factor)))
        scale = min(
            1.0,
            float(cap_w) / float(max(1, target_w)),
            float(cap_h) / float(max(1, target_h)),
        )
        if scale >= 0.999:
            return target_w, target_h
        return (
            max(1, int(round(target_w * scale))),
            max(1, int(round(target_h * scale))),
        )

    def _apply_preview_frame(self, frame_pil: Image.Image, request_key) -> None:
        """Fallback: already-composited output frame (pads baked into pixels)."""
        canvas = getattr(self.view, "preview_label", None)
        if canvas is None:
            return

        if hasattr(canvas, "set_pil_layers"):
            from engine.cache.tab_canvas_services import (
                reset_canvas_overlays,
            )
            canvas._preview_source_key = request_key
            reset_canvas_overlays(canvas)
            # Composited frame already includes fill; do not clear with plan fill.
            canvas._use_plan_fill_clear = False
            state = getattr(canvas, "runtime_state", None)
            if state is not None:
                state._store = None
            canvas.set_pil_layers(
                pil_image1=frame_pil,
                pil_image2=frame_pil,
                source_image1=frame_pil,
                source_image2=frame_pil,
                source_key=(request_key, "preview_source"),
                display_cache_key=(request_key, "preview_display"),
                shader_letterbox=False,
            )
            return

        rgba = frame_pil.convert("RGBA")
        qimage = QImage(
            rgba.tobytes("raw", "RGBA"),
            rgba.width,
            rgba.height,
            QImage.Format.Format_RGBA8888,
        ).copy()
        pixmap = QPixmap.fromImage(qimage)
        if hasattr(canvas, "set_pixmap"):
            canvas._preview_source_key = request_key
            canvas.set_pixmap(pixmap)

    def _prepare_preview_frame(
        self,
        exporter,
        snap,
        render_w: int,
        render_h: int,
        global_bounds,
        fill_color_tuple,
    ):
        """Pure CPU/PIL work (image decode, resample, plan building) — no Qt
        widget access, safe to run off the GUI thread. Mirrors
        ``prepare_snapshot_canvas_frame`` — same renderer as export, per
        render/export parity."""
        return exporter.prepare_snapshot_canvas_frame(
            snap,
            render_w,
            render_h,
            auto_crop=False,
            fit_content=self.fit_content_mode,
            global_bounds=global_bounds,
            fill_color=fill_color_tuple or (0, 0, 0, 0),
            thumbnail=False,
        )

    def _apply_prepared_preview_scene(
        self, prepared, request_key, render_w: int, render_h: int
    ) -> bool:
        """Qt-widget-touching half of the old ``_apply_preview_scene`` —
        GUI-thread only. ``prepared`` comes from ``_prepare_preview_frame``,
        possibly via a background worker's result callback."""
        canvas = getattr(self.view, "preview_label", None)
        if canvas is None or prepared is None:
            return False

        # Theme chrome outside the letterboxed canvas frame. Pad fill inside
        # the expanding canvas is painted by base.frag (canvasLetterbox +
        # letterboxFill), not by clearing the whole preview pane.
        canvas._use_plan_fill_clear = False

        # Crop mode: clip overlays to the export frame (inner == outer for
        # non-padded plans). Fit-content / uncrop: the padded canvas extends
        # beyond the image; clipping to the inner image rect would crop the
        # magnifier — export uses clip_overlays=False for the same reason
        # (gpu_export_scene.py).
        apply_canvas_render_plan(
            canvas,
            prepared.plan,
            store=prepared.store,
            clip_overlays_to_image_bounds=self._clip_overlays_for_mode(),
        )
        self._trace(
            "video.preview.apply_scene",
            f"apply preview scene render={render_w}x{render_h}",
            {
                "render_size": (int(render_w), int(render_h)),
                "plan_canvas": (
                    int(getattr(prepared.plan, "canvas_w", 0) or 0),
                    int(getattr(prepared.plan, "canvas_h", 0) or 0),
                ),
                "plan_image1_size": getattr(
                    getattr(prepared.plan, "image1", None),
                    "size",
                    None,
                ),
                "image_is_padded_composite": bool(
                    getattr(prepared.plan, "image_is_padded_composite", False)
                ),
                "overlay_clip_rect": getattr(
                    getattr(prepared.plan, "render_scene", None),
                    "overlay_clip_rect",
                    None,
                ),
                "fill_rgba": getattr(prepared.plan, "fill_rgba", None),
                "output_size": (
                    int(getattr(prepared, "output_width", 0) or 0),
                    int(getattr(prepared, "output_height", 0) or 0),
                ),
                "image_dest": (
                    int(getattr(prepared, "image_dest_x", 0) or 0),
                    int(getattr(prepared, "image_dest_y", 0) or 0),
                ),
                "debug": dict(getattr(prepared, "debug", {}) or {}),
            },
        )

        canvas._preview_source_key = request_key
        return True

    def render_preview_gpu(self, snap) -> bool:
        """Returns True if the caller should call ``_finish_preview_render``
        right away (the render finished synchronously, or couldn't start at
        all); False means a background worker was submitted and will call
        ``_finish_preview_render`` itself once it completes."""
        exporter = getattr(self.export_controller, "video_exporter", None)
        if exporter is None:
            return True
        preview_w, preview_h = self.get_preview_size_safe()
        render_w, render_h = self._resolve_preview_render_size(preview_w, preview_h)
        fill_color_tuple = self._resolve_fill_color_tuple()
        global_bounds = self._get_service_bounds() if self.fit_content_mode else None
        self._trace(
            "video.preview.request",
            f"preview request render={render_w}x{render_h} display={preview_w}x{preview_h}",
            {
                "preview_size": (int(preview_w), int(preview_h)),
                "render_size": (int(render_w), int(render_h)),
                "model_size": (
                    int(getattr(self.model, "width", 0) or 0),
                    int(getattr(self.model, "height", 0) or 0),
                ),
                "fit_content": bool(self.fit_content_mode),
                "preview_render_scale": float(self.preview_render_scale or 1.0),
                "global_bounds": repr(global_bounds),
                "fill_color": fill_color_tuple,
            },
        )
        snapshot_signature = (
            getattr(snap, "image1_path", None),
            getattr(snap, "image2_path", None),
            getattr(snap, "timestamp", None),
            viewport_fingerprint(snap.viewport_state),
            frozen_value(snap.settings_state),
        )
        prepare_key, request_key = self._build_preview_keys(
            snapshot_signature,
            render_w,
            render_h,
            self.fit_content_mode,
            fill_color_tuple,
            global_bounds,
            preview_w,
            preview_h,
        )
        request_id = self._render_task_id
        cache = self._preview_frame_cache or {}
        if (
            cache.get("prepare_key") == prepare_key
            and cache.get("plan") is not None
        ):
            _vplog.debug(
                "preview_cache_hit_refit task=%s render=%sx%s display=%sx%s",
                request_id,
                render_w,
                render_h,
                preview_w,
                preview_h,
            )
            canvas = getattr(self.view, "preview_label", None)
            if canvas is not None:
                canvas._use_plan_fill_clear = False
                apply_canvas_render_plan(
                    canvas,
                    cache["plan"],
                    store=cache.get("store"),
                    clip_overlays_to_image_bounds=self._clip_overlays_for_mode(),
                )
                canvas._preview_source_key = request_key
            return True

        # Cache miss: needs prepare_snapshot_canvas_frame(), which decodes
        # and resamples the source images — hundreds of ms for 4K sources.
        # Submitted to a background thread (like thumbnails.py) so a resize
        # drag doesn't stall the GUI thread; _on_preview_prepared applies the
        # result on the main thread once it's ready.
        _vplog.debug(
            "preview_render task=%s render=%sx%s display=%sx%s",
            request_id,
            render_w,
            render_h,
            preview_w,
            preview_h,
        )
        canvas = getattr(self.view, "preview_label", None)
        if canvas is not None and hasattr(canvas, "set_read_only"):
            canvas.set_read_only(True)

        worker = GenericWorker(
            self._prepare_preview_frame,
            exporter,
            snap,
            render_w,
            render_h,
            global_bounds,
            fill_color_tuple,
        )
        worker.signals.result.connect(
            lambda prepared, rid=request_id, rk=request_key, pk=prepare_key, rw=render_w, rh=render_h: (
                self._on_preview_prepared(prepared, rid, rk, pk, rw, rh)
            )
        )
        worker.signals.error.connect(
            lambda err, rid=request_id, rk=request_key, snap=snap, rw=render_w, rh=render_h, gb=global_bounds, fc=fill_color_tuple: (
                self._on_preview_prepare_error(rid, rk, snap, rw, rh, gb, fc)
            )
        )
        self._prepare_pool.start(worker)
        return False

    def _on_preview_prepared(
        self, prepared, request_id: int, request_key, prepare_key, render_w: int, render_h: int
    ) -> None:
        if request_id == self._render_task_id and self.has_live_view():
            if self._apply_prepared_preview_scene(prepared, request_key, render_w, render_h):
                cache = self._preview_frame_cache or {}
                cache["prepare_key"] = prepare_key
                cache["plan"] = prepared.plan
                cache["store"] = prepared.store
                cache["request_key"] = request_key
                cache["frame_pil"] = None
                self._preview_frame_cache = cache
        self._finish_preview_render(request_id)

    def _fallback_render_frame(self, exporter, snap, render_w, render_h, global_bounds, fill_color_tuple):
        """CPU fallback render for prepare-error path — safe off GUI thread."""
        if exporter is None:
            return None
        return exporter.render_snapshot_to_pil(
            snap,
            render_w,
            render_h,
            auto_crop=False,
            fit_content=self.fit_content_mode,
            global_bounds=global_bounds,
            fill_color=fill_color_tuple or (0, 0, 0, 0),
        )

    def _on_fallback_frame_ready(self, frame_pil, request_id: int, request_key) -> None:
        if request_id == self._render_task_id and self.has_live_view() and frame_pil is not None:
            self._apply_preview_frame(frame_pil, request_key)
            self._preview_frame_cache = {
                "prepare_key": None,
                "plan": None,
                "store": None,
                "request_key": request_key,
                "frame_pil": frame_pil,
            }
        self._finish_preview_render(request_id)

    def _on_fallback_frame_error(self, err, request_id: int) -> None:
        logger.error("Video preview fallback render failed: %s", err)
        self._finish_preview_render(request_id)

    def _on_preview_prepare_error(
        self, request_id: int, request_key, snap, render_w: int, render_h: int, global_bounds, fill_color_tuple
    ) -> None:
        logger.error("Video preview prepare failed on background thread")
        if request_id != self._render_task_id or not self.has_live_view():
            self._finish_preview_render(request_id)
            return
        exporter = getattr(self.export_controller, "video_exporter", None)
        worker = GenericWorker(
            self._fallback_render_frame,
            exporter,
            snap,
            render_w,
            render_h,
            global_bounds,
            fill_color_tuple,
        )
        worker.signals.result.connect(
            lambda pil, rid=request_id, rk=request_key: self._on_fallback_frame_ready(pil, rid, rk)
        )
        worker.signals.error.connect(
            lambda err, rid=request_id: self._on_fallback_frame_error(err, rid)
        )
        try:
            self._prepare_pool.start(worker)
        except Exception:
            try:
                self.export_controller.thread_pool.start(worker)
            except Exception:
                self._finish_preview_render(request_id)

    def _finish_preview_render(self, request_id: int) -> bool:
        _vplog.debug("preview_end task=%s", request_id)
        if request_id != self._render_task_id or not self.has_live_view():
            _vplog.debug(
                "preview_drop_stale task=%s current=%s live=%s",
                request_id,
                self._render_task_id,
                self.has_live_view(),
            )
            self._is_rendering_preview = False
            return False

        if not self._preview_ready_emitted:
            self._preview_ready_emitted = True
            self.emit_preview_ready()
        self._is_rendering_preview = False
        return True

    def on_fit_content_fill_color_changed(self, _color):
        self.reset_render_state()
        self.schedule_update()

    def on_preview_scale_changed(self, scale: float):
        self.preview_render_scale = max(0.25, min(float(scale), 1.0))
        if self.model is not None and hasattr(self.model, "preview_render_scale"):
            self.model.preview_render_scale = self.preview_render_scale
        self.reset_render_state()
        self.schedule_update()

    def on_width_changed(self, width: int):
        self.reset_render_state()
        if self.model.aspect_ratio_locked:
            new_height = self.model.adjust_height_to_aspect_ratio(width)
            self.model.set_resolution(width, new_height)
            if self.has_live_view():
                self.view.set_resolution(width, new_height)
        else:
            self.model.width = width
        self.schedule_update()

    def on_height_changed(self, height: int):
        self.reset_render_state()
        if self.model.aspect_ratio_locked:
            new_width = self.model.adjust_width_to_aspect_ratio(height)
            self.model.set_resolution(new_width, height)
            if self.has_live_view():
                self.view.set_resolution(new_width, height)
        else:
            self.model.height = height
        self.schedule_update()

    def on_fit_content_changed(self, enabled: bool):
        self.reset_render_state()
        self.fit_content_mode = enabled

        if enabled:
            self._stored_crop_resolution = (self.model.width, self.model.height)
            self.recalculate_global_bounds()
        else:
            if self._stored_crop_resolution and self.has_live_view():
                width, height = self._stored_crop_resolution
                self.view.blockSignals(True)
                was_locked = self.model.aspect_ratio_locked
                self.model.aspect_ratio_locked = False
                self.model.set_resolution(width, height)
                self.model.aspect_ratio_locked = was_locked
                self.view.set_resolution(width, height)
                self.view.blockSignals(False)

            # Bounds live in the service: invalidate there (single reset path).
            exporter = getattr(self.export_controller, "video_exporter", None)
            if exporter is not None:
                exporter.invalidate_bounds_cache()

        self.schedule_update()

    def recalculate_global_bounds(self):
        exporter = getattr(self.export_controller, "video_exporter", None)
        if exporter is None:
            return

        snapshots = self.editor_service.get_current_snapshots()
        if not snapshots:
            return

        from .common import VIDEO_EDITOR_AUTO_CROP

        self._bounds_request_id += 1
        request_id = self._bounds_request_id

        def calculate():
            return exporter.calculate_global_canvas_bounds(snapshots, VIDEO_EDITOR_AUTO_CROP)

        worker = GenericWorker(calculate)
        worker.signals.result.connect(
            lambda bounds, rid=request_id: self.on_global_bounds_calculated(bounds, request_id=rid)
        )
        worker.signals.error.connect(
            lambda err, rid=request_id: self.on_bounds_calculation_error(err, request_id=rid)
        )
        self.export_controller.thread_pool.start(worker)

    def on_global_bounds_calculated(self, bounds, request_id: int | None = None):
        if request_id is not None and request_id != self._bounds_request_id:
            return
        # Publish to the service-owned cache (single write path, no mirror).
        exporter = getattr(self.export_controller, "video_exporter", None)
        if exporter is not None:
            try:
                snapshots = self.editor_service.get_current_snapshots()
            except Exception:
                snapshots = None
            try:
                store = getattr(exporter, "store_cached_global_bounds", None)
                if callable(store):
                    store(bounds, snapshots)
                else:  # pragma: no cover — legacy fake exporters in tests
                    exporter._cached_global_bounds = bounds
            except Exception:
                pass

        if self.emit_fit_content_available is not None:
            # Nothing to uncrop when the virtual canvas never leaves 0..1
            # (no snapshot ever padded/overflowed the frame) — toggling
            # fit-content would be a visual no-op, so disable the button.
            self.emit_fit_content_available(bounds is None or bounds.extends_beyond_unit())

        if bounds and self.fit_content_mode and self.has_live_view():
            g_pad_left = int(bounds.pad_left)
            g_pad_right = int(bounds.pad_right)
            g_pad_top = int(bounds.pad_top)
            g_pad_bottom = int(bounds.pad_bottom)
            g_base_w = int(bounds.base_width)
            g_base_h = int(bounds.base_height)
            canvas_w = g_base_w + g_pad_left + g_pad_right
            canvas_h = g_base_h + g_pad_top + g_pad_bottom

            self.view.blockSignals(True)
            was_locked = self.model.aspect_ratio_locked
            self.model.aspect_ratio_locked = False
            self.model.set_resolution(canvas_w, canvas_h)
            self.model.aspect_ratio_locked = was_locked
            self.view.set_resolution(canvas_w, canvas_h)
            self.view.blockSignals(False)

        self.schedule_update()

    def on_bounds_calculation_error(self, err, request_id: int | None = None):
        if request_id is not None and request_id != self._bounds_request_id:
            return
        logger.error(f"Error calculating global bounds: {err}")
        if self.emit_fit_content_available is not None:
            # Unknown, not confirmed unit — don't leave the button stuck
            # disabled over a transient failure.
            self.emit_fit_content_available(True)

    def on_window_resized(self):
        if not self.has_live_view():
            return
        if self.playback_engine.is_playing():
            return
        self._resize_settle.ping()

    def cleanup(self):
        self._view_destroyed = True
        self._preview_updater.stop()
        self._resize_settle.cancel()
        self._preview_frame_cache = None
        if self.has_live_view() and hasattr(self.view, "preview_label"):
            self.view.preview_label._preview_source_key = None
        self.view = None
