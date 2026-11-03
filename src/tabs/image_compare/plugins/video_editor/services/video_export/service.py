# Audit-Meta: pattern=state-machine reason="single video export service lifecycle — job building, ffmpeg, cancel, GPU warm-up"
from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Sequence

from PySide6.QtGui import QColor

from engine.cache import TargetSurfaceSpec
from engine.store.pyramid_registry import pyramid_for
from tabs.image_compare.plugins.video_editor.services.keyframing import KeyframedRecording
from tabs.image_compare.plugins.video_editor.services.editor import (
    frame_count_for_duration,
)
from tabs.image_compare.services.video_snapshot_rendering.geometry import (
    THUMBNAIL_TARGET_SIZE,
    thumbnail_proxy_dest_scale,
)
from tabs.image_compare.plugins.video_editor.services.video_export.bounds import (
    CanvasBoundsAnalyzer,
)
from tabs.image_compare.plugins.video_editor.services.video_export.encoding import (
    FFmpegCommandBuilder,
    FFmpegProcessManager,
)
from tabs.image_compare.plugins.video_editor.services.video_export.images import (
    VideoExportImageRepository,
)
from tabs.image_compare.plugins.video_editor.services.video_export.models import (
    GlobalCanvasBounds,
    VideoExportJob,
    VideoRenderRequest,
    unique_video_path,
)
from tabs.image_compare.plugins.video_editor.services.video_export.render_loop import (
    VideoRenderLoop,
)
from tabs.image_compare.plugins.video_editor.services.video_snapshot_rendering import (
    SnapshotFrameRenderer,
)
from shared.video_policy import CrfPolicy
from core.log_names import APP_LOGGER_NAME

logger = logging.getLogger(APP_LOGGER_NAME)
_vrlog = logging.getLogger(f"{APP_LOGGER_NAME}.video_render")

VIDEO_EDITOR_AUTO_CROP = False

#: Pixel-source kinds for one thumbnail side, recorded on
#: ``VideoExporterService.last_thumbnail_source`` for structure asserts:
#: ``"pyramid"`` (already-built coarse level, no decode), ``"cache"``
#: (already-decoded base store reused, no new decode), ``"miss"`` (nothing
#: warm — the caller falls back to the full-res path, degrading, never
#: stalling the strip).
THUMBNAIL_SOURCE_PYRAMID = "pyramid"
THUMBNAIL_SOURCE_CACHE = "cache"
THUMBNAIL_SOURCE_MISS = "miss"


def _image_uid_for_log(image):
    """Best-effort store uid for thumbnail debug lines (never raises)."""
    try:
        from engine.cache.image_identity import image_uid

        return image_uid(image)
    except Exception:
        return None


def _cached_thumbnail_base(image_repository, path, auto_crop):
    """Already-decoded base store for ``(path, auto_crop)``, or None.

    Read-only peek at the repository cache — never decodes, never blocks.
    """
    try:
        cache = getattr(image_repository, "_images_cache", None)
        if not isinstance(cache, dict):
            return None
        img = cache.get((path, auto_crop))
    except Exception:
        return None
    if img is None:
        return None
    try:
        if not getattr(img, "is_open", True):
            return None
    except Exception:
        return None
    return img


def _pick_thumbnail_proxy_from_base(base, target_size=None):
    """Pyramid pick over an already-resolved base store — never decodes.

    Returns ``(store, kind)`` with kind in ``{"pyramid", "cache", "miss"}``.
    Shared by the built-in repository lookup and the tab-side PipelineCache
    resolver so both pick the same coarse level for the same target.
    """
    if target_size is None:
        target_size = THUMBNAIL_TARGET_SIZE
    try:
        target_w, target_h = int(target_size[0]), int(target_size[1])
    except Exception:
        target_w, target_h = THUMBNAIL_TARGET_SIZE
    if base is None:
        return None, THUMBNAIL_SOURCE_MISS
    try:
        src_w, src_h = int(base.width), int(base.height)
    except Exception:
        return None, THUMBNAIL_SOURCE_MISS
    try:
        pyramid = pyramid_for(base)
    except Exception:
        pyramid = None
    if pyramid is not None:
        try:
            depth = int(pyramid.level_count)
            dest_scale = thumbnail_proxy_dest_scale(src_w, src_h, target_w, target_h)
            from engine.cache.lod import demand_level

            picked = int(demand_level(dest_scale, int(pyramid.level_count)))
        except Exception:
            depth, picked = 1, 0
        if depth > 1 and picked >= 1:
            try:
                return pyramid.level(picked), THUMBNAIL_SOURCE_PYRAMID
            except Exception:
                pass
    return base, THUMBNAIL_SOURCE_CACHE


def make_pipeline_cache_thumbnail_resolver(pipeline_cache):
    """Thumbnail proxy resolver peeking the warm tab-side PipelineCache.

    ``resolver(path, auto_crop, target_size) -> (image, kind) | None``.
    Peeks ``pipeline_cache.get_pixel(path, auto_crop=auto_crop)`` boxlessly
    (the pixel key ignores the crop arg) — returning the SAME store object
    the canvas loaded, so ``pyramid_for`` hits the already-built pyramid.
    ``None`` (store absent, cache without ``get_pixel``, any error) means
    "fall through to the built-in miss path". Never decodes, never raises.
    """
    def _resolver(path, auto_crop=False, target_size=None):
        try:
            get_pixel = getattr(pipeline_cache, "get_pixel", None)
            if not callable(get_pixel):
                _vrlog.debug("thumbnail_resolver peek n/a (no get_pixel)")
                return None
            base = get_pixel(path, auto_crop=auto_crop)
        except Exception:
            _vrlog.debug("thumbnail_resolver peek error", exc_info=True)
            return None
        if base is None:
            try:
                pixel = getattr(pipeline_cache, "_pixel", None)
                entries = list(pixel.keys()) if hasattr(pixel, "keys") else None
                sample = (
                    [os.path.basename(k[0]) if isinstance(k, tuple) and k else k for k in entries[:5]]
                    if entries
                    else entries
                )
                n = len(entries) if entries is not None else -1
            except Exception:
                n, sample = -2, None
            _vrlog.debug(
                "thumbnail_resolver peek miss path=%s auto_crop=%s cache_entries=%s sample=%s",
                os.path.basename(path) if path else None,
                auto_crop,
                n,
                sample,
            )
            return None
        _vrlog.debug(
            "thumbnail_resolver peek hit path=%s uid=%s open=%s",
            os.path.basename(path) if path else None,
            getattr(base, "uid", None),
            getattr(base, "is_open", None),
        )
        try:
            image, kind = _pick_thumbnail_proxy_from_base(base, target_size)
        except Exception:
            return None
        if image is None:
            return None
        return image, kind

    return _resolver


def make_session_state_thumbnail_resolver(peek_session_images):
    """Thumbnail proxy resolver reading the live Redux image session.

    ``peek_session_images() -> (image1, image2, paths1, paths2) | None``
    (all getattr-guarded by the caller). ``image1/image2`` are the SAME
    unified store objects the canvas renders AND the pyramids were built
    for, so ``pyramid_for`` hits the warm coarse level. Paths match by
    normpath against ``loaded_image*_paths``; unmatched path → ``None``
    (fall through). Never decodes, never raises.
    """
    def _resolver(path, auto_crop=False, target_size=None):
        try:
            state = peek_session_images()
        except Exception:
            return None
        if not state:
            return None
        try:
            img1, img2, lp1, lp2 = state
        except Exception:
            return None
        try:
            norm = os.path.normpath(path) if path else None
        except Exception:
            return None
        if not norm:
            return None
        base = None
        for cand_paths, img in ((lp1, img1), (lp2, img2)):
            try:
                norms = {os.path.normpath(p) for p in (cand_paths or [])}
            except Exception:
                continue
            if norm in norms:
                base = img
                break
        if base is None:
            return None
        _vrlog.debug(
            "thumbnail_session_state peek hit path=%s uid=%s",
            os.path.basename(path),
            _image_uid_for_log(base),
        )
        try:
            image, kind = _pick_thumbnail_proxy_from_base(base, target_size)
        except Exception:
            return None
        if image is None:
            return None
        return image, kind

    return _resolver


def resolve_thumbnail_proxy_source(
    image_repository,
    path,
    auto_crop=False,
    target_size=None,
):
    """Proxy-resolution pixel source for one thumbnail side — never decodes.

    Resolution order: already-built pyramid coarse level (covers the
    thumbnail target) → already-decoded base store → ``(None, "miss")``.
    On ``"miss"`` the caller must use the full-res path (degrade, never
    stall). Returns ``(store_or_None, kind)`` with kind in
    ``{"pyramid", "cache", "miss"}``.
    """
    if target_size is None:
        target_size = THUMBNAIL_TARGET_SIZE
    base = _cached_thumbnail_base(image_repository, path, auto_crop)
    if base is None:
        return None, THUMBNAIL_SOURCE_MISS
    return _pick_thumbnail_proxy_from_base(base, target_size)


class VideoExporterService:
    def __init__(self, recorder, store, main_controller=None, gpu_export_service=None):
        import threading

        self.recorder = recorder
        self.main_store = store
        self.main_controller = main_controller
        self.gpu_export_service = gpu_export_service

        self._cached_global_bounds: GlobalCanvasBounds | None = None
        self._cached_bounds_snapshots_hash = None
        self._active_processes = []
        self._cancel_requested = False
        self._cancel_lock = threading.Lock()
        self._last_render_backend = "gpu"
        # P1 thumbnail proxy seam: optional ``resolver(path, auto_crop,
        # target_size) -> (image_or_None, kind) | None``. Unset → the
        # built-in repository/pyramid lookup. Session wiring (P2/P3) can
        # inject display-cache images here without this service knowing
        # tab internals.
        self._thumbnail_proxy_resolver = None
        # Last per-side source kinds ``(kind1, kind2)`` from the thumbnail
        # path only (structure asserts; export never touches this).
        self._last_thumbnail_source = None

        self._image_repository = VideoExportImageRepository()
        self._bounds_analyzer = CanvasBoundsAnalyzer(self._image_repository.get_image)
        self._frame_renderer = SnapshotFrameRenderer(
            image_loader=self._image_repository.get_image,
            gpu_export_service=self.gpu_export_service,
        )
        self._ffmpeg_command_builder = FFmpegCommandBuilder()
        self._process_manager = FFmpegProcessManager(self._active_processes)
        self._render_loop = VideoRenderLoop(self)

    def warm_up_gpu_widgets(self) -> bool:
        """Pre-create the offscreen GPU render widget used for preview/export,
        well ahead of the video editor dialog being opened.

        Returns True only when the offscreen service this exporter owns is
        ready, so the caller can stop retrying (``GpuExportService.warm_up``
        re-enters its already-created widget).
        """
        # The widget pays a full D3D shader/pipeline compile (~4 s on the
        # Windows build, measured 2026-10-24). This call runs on the startup
        # timer, before the editor is usually opened, so the compile lands
        # where the user is not waiting on a frame.
        service = self.gpu_export_service
        if service is None or not hasattr(service, "warm_up"):
            return False
        try:
            return bool(service.warm_up())
        except Exception:
            logger.debug(
                "Offscreen GPU warm-up failed for %s",
                type(service).__name__,
                exc_info=True,
            )
            return False

    def _drain_last_render_debug(self) -> dict:
        return self._frame_renderer.drain_last_debug()

    def _clear_frame_caches(self) -> None:
        self._image_repository.clear()
        self._frame_renderer.reset_backend_state()

    def request_cancel(self):
        with self._cancel_lock:
            self._cancel_requested = True
        self.cleanup()

    def is_cancel_requested(self) -> bool:
        with self._cancel_lock:
            return bool(self._cancel_requested)

    def _coerce_recording(self, snapshots_or_recording):
        if isinstance(snapshots_or_recording, KeyframedRecording):
            return snapshots_or_recording
        if hasattr(snapshots_or_recording, "evaluate_at") and hasattr(
            snapshots_or_recording, "get_duration"
        ):
            return snapshots_or_recording
        extra_adapters = tuple(getattr(snapshots_or_recording, "extra_adapters", ()))
        return KeyframedRecording.from_snapshots(
            list(snapshots_or_recording or []),
            extra_adapters=extra_adapters,
        )

    def _materialize_snapshots(self, snapshots_or_recording):
        if isinstance(snapshots_or_recording, Sequence):
            return list(snapshots_or_recording)
        if hasattr(snapshots_or_recording, "materialize_snapshots"):
            return snapshots_or_recording.materialize_snapshots()
        return []

    def calculate_global_canvas_bounds(self, snapshots, auto_crop=False):
        materialized = self._materialize_snapshots(snapshots)
        return self._bounds_analyzer.calculate(materialized, auto_crop)

    def _snapshots_hash(self, materialized) -> tuple | None:
        """Bounds cache key: length + tail instant + content fingerprint.

        Length-plus-tail alone aliases recordings that swap a middle
        snapshot's images (same count, same tail instant) and would reuse
        stale bounds. The fingerprint covers every snapshot's
        ``(image1_path, image2_path, timestamp)`` so any content change
        recomputes. Never raises: fingerprint failure degrades to the old
        length-plus-tail key rather than breaking the export.
        """
        if not materialized:
            return None
        try:
            tail_ts = float(materialized[-1].timestamp)
        except (AttributeError, TypeError, ValueError):
            tail_ts = 0.0
        try:
            digest = hashlib.blake2b(digest_size=16)
            for snap in materialized:
                digest.update(
                    repr(
                        (
                            getattr(snap, "image1_path", None),
                            getattr(snap, "image2_path", None),
                            float(getattr(snap, "timestamp", 0.0)),
                        )
                    ).encode("utf-8", "ignore")
                )
            fingerprint = digest.hexdigest()
        except Exception:
            fingerprint = None
        return (len(materialized), tail_ts, fingerprint)

    def get_cached_global_bounds(self, snapshots, auto_crop=False):
        materialized = self._materialize_snapshots(snapshots)
        current_hash = (self._snapshots_hash(materialized), bool(auto_crop))

        if (
            self._cached_global_bounds is None
            or self._cached_bounds_snapshots_hash != current_hash
        ):
            self._cached_global_bounds = self._bounds_analyzer.calculate(
                materialized,
                auto_crop,
            )
            self._cached_bounds_snapshots_hash = current_hash

        return self._cached_global_bounds

    def peek_cached_global_bounds(self) -> GlobalCanvasBounds | None:
        """GUI-thread-safe read of the cached bounds — never computes.

        ``get_cached_global_bounds`` recomputes synchronously on a hash miss
        (decode/measure work); the preview coordinator must not call that on
        the GUI thread. It reads here instead and waits for the background
        ``recalculate_global_bounds`` worker to :meth:`store_cached_global_bounds`.
        """
        return self._cached_global_bounds

    def store_cached_global_bounds(
        self, bounds: GlobalCanvasBounds | None, snapshots, auto_crop: bool = False
    ) -> GlobalCanvasBounds | None:
        """Single write path for the bounds cache (service-owned single source).

        Background ``recalculate_global_bounds`` workers publish here; the
        coordinator mirror is gone, so every reader goes through the service.
        ``auto_crop`` defaults to ``False`` (``VIDEO_EDITOR_AUTO_CROP``) so
        existing two-arg publishers keep working; the key always pairs the
        content fingerprint with the crop flag.
        """
        materialized = self._materialize_snapshots(snapshots)
        self._cached_global_bounds = bounds
        self._cached_bounds_snapshots_hash = (
            self._snapshots_hash(materialized),
            bool(auto_crop),
        )
        return bounds

    def invalidate_bounds_cache(self):
        self._cached_global_bounds = None
        self._cached_bounds_snapshots_hash = None

    def get_image(self, path, auto_crop=False):
        return self._image_repository.get_image(path, auto_crop)

    @staticmethod
    def _coerce_global_bounds(global_bounds) -> GlobalCanvasBounds | None:
        if global_bounds is None:
            return None
        if isinstance(global_bounds, GlobalCanvasBounds):
            return global_bounds
        raise TypeError("global_bounds must be GlobalCanvasBounds or None")

    @staticmethod
    def _build_render_request(
        out_w,
        out_h,
        font_path,
        auto_crop,
        fit_content,
        global_bounds,
        fill_color,
    ) -> VideoRenderRequest:
        return VideoRenderRequest(
            target_surface=TargetSurfaceSpec(
                width=max(1, int(out_w)),
                height=max(1, int(out_h)),
                fill_rgba=fill_color,
            ),
            font_path=font_path,
            auto_crop=auto_crop,
            fit_content=fit_content,
            global_bounds=global_bounds,
        )

    def _render_snapshot_with_renderer(self, snap, request: VideoRenderRequest):
        _vrlog.debug(
            "render_begin renderer=main out=%sx%s fit_content=%s ts=%s",
            request.target_surface.width,
            request.target_surface.height,
            request.fit_content,
            getattr(snap, "timestamp", None),
        )
        result = self._frame_renderer.render(snap, request)
        _vrlog.debug(
            "render_done renderer=main out=%sx%s backend=%s",
            request.target_surface.width,
            request.target_surface.height,
            result.backend,
        )
        self._last_render_backend = result.backend
        return result.image

    def prepare_snapshot_canvas_frame(
        self,
        snap,
        out_w,
        out_h,
        font_path=None,
        auto_crop=False,
        fit_content=False,
        global_bounds=None,
        fill_color=(0, 0, 0, 0),
    ):
        request = self._build_render_request(
            out_w,
            out_h,
            font_path,
            auto_crop,
            fit_content,
            self._coerce_global_bounds(global_bounds),
            fill_color,
        )
        return self._frame_renderer.prepare_canvas_frame(snap, request)

    def render_snapshot_to_pil(
        self,
        snap,
        out_w,
        out_h,
        font_path=None,
        auto_crop=False,
        fit_content=False,
        global_bounds=None,
        fill_color=(0, 0, 0, 0),
    ):
        request = self._build_render_request(
            out_w,
            out_h,
            font_path,
            auto_crop,
            fit_content,
            self._coerce_global_bounds(global_bounds),
            fill_color,
        )
        return self._render_snapshot_with_renderer(snap, request)

    def set_thumbnail_proxy_resolver(self, resolver) -> None:
        """Inject the thumbnail proxy-source resolver (P1 seam).

        ``resolver(path, auto_crop, target_size)`` returns
        ``(image_or_None, kind)`` or ``None`` to fall through to the
        built-in repository/pyramid lookup. ``None`` restores the default.
        """
        self._thumbnail_proxy_resolver = resolver

    @property
    def last_thumbnail_source(self):
        """Per-side source kinds ``(kind1, kind2)`` of the last thumbnail
        request (``"pyramid"``/``"cache"``/``"miss"``), or ``None`` before
        the first one. Export requests never touch this."""
        return self._last_thumbnail_source

    def _resolve_thumbnail_images(self, snap, auto_crop, target_size):
        """Proxy images for both thumbnail sides — never decodes.

        Returns ``((img1_or_None, img2_or_None), (kind1, kind2))``. Any
        ``None`` side means that side missed; the strip band's
        ``proxy_pair_for`` (the only caller) draws a placeholder tile for
        that block rather than decoding on the interaction path.
        """
        paths = (
            getattr(snap, "image1_path", None),
            getattr(snap, "image2_path", None),
        )
        images: list = []
        kinds: list = []
        for path in paths:
            image, kind = None, THUMBNAIL_SOURCE_MISS
            resolver = self._thumbnail_proxy_resolver
            if resolver is not None:
                try:
                    resolved = resolver(path, auto_crop, target_size)
                except Exception:
                    resolved = None
                if resolved is not None:
                    try:
                        image, kind = resolved
                    except Exception:
                        image, kind = None, THUMBNAIL_SOURCE_MISS
            if image is None:
                image, kind = resolve_thumbnail_proxy_source(
                    self._image_repository,
                    path,
                    auto_crop,
                    target_size,
                )
            images.append(image)
            kinds.append(kind)
        self._last_thumbnail_source = (kinds[0], kinds[1])
        _vrlog.debug(
            "thumbnail_proxy_kinds=(%s,%s) resolver=%s",
            kinds[0],
            kinds[1],
            "session" if self._thumbnail_proxy_resolver is not None else "none",
        )
        if images[0] is None or images[1] is None:
            return (None, None), self._last_thumbnail_source
        return (images[0], images[1]), self._last_thumbnail_source

    def _build_export_job(self, recording, resolution, fps, export_options):
        out_w, out_h = resolution

        custom_dir = export_options.get("output_dir", "").strip()
        custom_name = export_options.get("file_name", "").strip()
        output_dir = (
            custom_dir
            if custom_dir
            else (self.main_store.settings.export_default_dir or os.getcwd())
        )

        ext = export_options.get("container", "mp4")
        if export_options.get("manual_mode") and not ext:
            ext = "mp4"

        if custom_name:
            base_name = (
                custom_name[: -len(f".{ext}")]
                if custom_name.lower().endswith(f".{ext}")
                else custom_name
            )
        else:
            base_name = f"video_{int(time.time())}"

        os.makedirs(output_dir, exist_ok=True)
        output_path = unique_video_path(output_dir, base_name, ext)

        font_path = None
        window_shell = self.main_controller.window_shell if self.main_controller else None
        if window_shell is not None:
            font_path = window_shell.main_window_app.font_path_absolute

        total_duration = recording.get_duration()
        # Single frame-count source (editor.frame_count_for_duration): ceil+1
        # keeps the tail frame on non-integer duration*fps; the old floor
        # split silently dropped it from the stream.
        total_frames = frame_count_for_duration(total_duration, fps)

        fit_content = export_options.get("fit_content", False)
        fill_color = QColor(export_options.get("fit_content_fill_color", "#00000000"))
        fill_rgba = (
            fill_color.red(),
            fill_color.green(),
            fill_color.blue(),
            fill_color.alpha(),
        )
        auto_crop = VIDEO_EDITOR_AUTO_CROP

        global_bounds = None
        if fit_content:
            logger.info("Calculating global canvas bounds for fit_content mode...")
            global_bounds = self._bounds_analyzer.calculate(
                self._materialize_snapshots(recording),
                auto_crop,
            )
            if global_bounds:
                canvas_w = (
                    global_bounds.base_width
                    + global_bounds.pad_left
                    + global_bounds.pad_right
                )
                canvas_h = (
                    global_bounds.base_height
                    + global_bounds.pad_top
                    + global_bounds.pad_bottom
                )
                logger.info(
                    "Global canvas size: %sx%s (base: %sx%s, padding: L=%s, R=%s, T=%s, B=%s)",
                    canvas_w,
                    canvas_h,
                    global_bounds.base_width,
                    global_bounds.base_height,
                    global_bounds.pad_left,
                    global_bounds.pad_right,
                    global_bounds.pad_top,
                    global_bounds.pad_bottom,
                )

        return VideoExportJob(
            recording=recording,
            output_path=output_path,
            width=out_w,
            height=out_h,
            fps=fps,
            total_frames=total_frames,
            font_path=font_path,
            fit_content=fit_content,
            fill_rgba=fill_rgba,
            global_bounds=global_bounds,
            auto_crop=auto_crop,
            export_options=export_options,
        )

    def export_recorded_video(
        self,
        resolution=(1920, 1080),
        fps=60,
        snapshots_override=None,
        export_options=None,
        progress_callback=None,
        log_callback=None,
    ):
        source = snapshots_override if snapshots_override else self.recorder.snapshots
        recording = self._coerce_recording(source)
        if not recording:
            return None

        with self._cancel_lock:
            if self._cancel_requested:
                # Cancel was pressed before the worker even started; honor it
                # instead of erasing the flag with an unconditional False.
                return None
            self._cancel_requested = False
        self._frame_renderer.reset_backend_state()

        if not export_options:
            export_options = {
                "container": "mp4",
                "codec": "h264",
                "quality_mode": "crf",
                "crf": CrfPolicy.DEFAULT,
                "preset": "medium",
            }

        job = self._build_export_job(recording, resolution, fps, export_options)
        logger.info("Rendering %s frames to %s using FFmpeg...", job.total_frames, job.output_path)

        try:
            cmd = self._ffmpeg_command_builder.build(
                job.output_path, job.width, job.height, job.fps, job.export_options
            )
            logger.info("FFmpeg command: %s", " ".join(cmd))
            if log_callback:
                log_callback(f"$ {' '.join(cmd)}")
            process = self._process_manager.start(cmd, stderr_line_callback=log_callback)
        except FileNotFoundError as exc:
            raise RuntimeError(
                "FFmpeg not found. Please install FFmpeg or place ffmpeg.exe next to the application."
            ) from exc
        except Exception as exc:
            raise RuntimeError(f"Failed to start FFmpeg: {exc}") from exc

        export_canceled = False
        stderr_output = ""
        process_returncode = None

        try:
            export_canceled = self._render_loop.render(process, job, progress_callback)
        except Exception as exc:
            logger.error("Error during video rendering loop: %s", exc, exc_info=True)
            raise
        finally:
            process_returncode, stderr_output = self._process_manager.finalize(process)

        if self.is_cancel_requested() or export_canceled:
            self._clear_frame_caches()
            return None

        if process_returncode != 0:
            error_msg = f"FFmpeg exited with error code {process_returncode}"
            if stderr_output:
                error_msg += f"\n\n{stderr_output.strip()}"
            self._clear_frame_caches()
            raise RuntimeError(error_msg)

        logger.info("Video export successful: %s", job.output_path)
        self._clear_frame_caches()
        return job.output_path

    def cleanup(self):
        self._process_manager.cleanup()
