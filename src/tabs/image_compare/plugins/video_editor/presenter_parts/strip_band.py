"""Host for the GPU filmstrip band inside the video editor (strip atlas, phase 2).

The band itself is toolkit-owned (``sli_ui_toolkit...strip_band.StripBand``: a
paint-only QRhiWidget over the strip row with set_renderer / set_view / place).
The timeline widget creates it, places it on every
resize and pushes the view on every scroll/zoom commit. This host supplies the
two things the band deliberately does not own:

* **the renderer** — ``render(command_buffer, band)`` resolves the current
  window (rebuilding it when the view/recording/layout changed since the last
  build) and then records :func:`render_window` into the band's own target.
  The build is synchronous: a scroll tick reaches the GPU frame directly, with
  no timer, no settle gate and no per-tile worker.
* **the release** — ``release()`` drops the pass resources for the band's RHI
  generation (a ``QRhiWidget``'s ``releaseResources`` contract).

Window lifecycle: one window per (recording, first block, block count, tile
size). Building a newer window supersedes the older one inside
``strip_window``; a superseded window raises ``StripWindowCancelled`` at render
time and draws nothing. A build that raises is logged and the previous window
stays live (fail-open — placeholder tiles are the renderer's job, a stale
frame beats a blank band).

Geometry single source: :func:`timeline_strip_layout` asks the timeline's own
``strip_block_layout`` for the visible blocks (its representative frames and
the block width that locates the first one), and takes the tile from
:func:`strip_recording.strip_tile_size` — one filmstrip thumbnail, never the
block cell's aspect. So this module holds no block formula and no tile formula
of its own. Phase 3 deletes the QPainter thumbnail pipeline
(``ThumbnailService`` / ``ThumbnailCoordinator``) — nothing here touches it.
"""

from __future__ import annotations

import logging

from tabs.image_compare.plugins.video_editor.presenter_parts.common import (
    VIDEO_EDITOR_AUTO_CROP,
)
from tabs.image_compare.plugins.video_editor.services.strip_recording import (
    StripRecordingAdapter,
    proxy_pair_for,
    timeline_strip_layout,
)
from tabs.image_compare.services.video_snapshot_rendering.strip_window import (
    StripWindowCancelled,
    build_window,
    cancel,
    render_window,
)
from core.log_names import APP_LOGGER_NAME

logger = logging.getLogger(APP_LOGGER_NAME)

#: Timeline signals that mean "the view the strip draws for changed".
_VIEW_SIGNALS = ("viewportChanged", "zoomChanged", "viewportChangedState")


def render_request(tile_w: int, tile_h: int):
    """The per-tile ``VideoRenderRequest`` the thumbnail/export path builds."""
    from engine.cache import TargetSurfaceSpec
    from tabs.image_compare.plugins.video_editor.services.video_export_models import (
        VideoRenderRequest,
    )

    return VideoRenderRequest(
        target_surface=TargetSurfaceSpec(
            width=max(1, int(tile_w)),
            height=max(1, int(tile_h)),
            fill_rgba=(0, 0, 0, 0),
        ),
        font_path=None,
        auto_crop=VIDEO_EDITOR_AUTO_CROP,
        fit_content=False,
        global_bounds=None,
    )


class StripBandHost:
    """Drives the timeline's StripBand from the editor's recording."""

    def __init__(
        self,
        view,
        editor_service,
        export_controller=None,
        *,
        builder=build_window,
        auto_crop=VIDEO_EDITOR_AUTO_CROP,
    ):
        self.view = view
        self.editor_service = editor_service
        self.export_controller = export_controller
        self._builder = builder
        self._auto_crop = bool(auto_crop)
        self._band = None
        self._window = None
        self._key = None
        #: Diagnostics: window builds, build failures kept fail-open, frames
        #: whose window was rendered, and the last failure text.
        self.build_count = 0
        self.build_errors = 0
        self.render_count = 0
        self.last_error: str | None = None

    # -- lifecycle ------------------------------------------------------

    def attach(self) -> bool:
        """Enable the band on the timeline and wire the view-change signals."""
        timeline = self._timeline()
        if timeline is None:
            logger.debug("strip band not attached: timeline unavailable")
            return False
        enable = getattr(timeline, "enable_strip_band", None)
        if not callable(enable):
            logger.debug(
                "strip band not attached: timeline has no enable_strip_band"
            )
            return False
        try:
            band = enable(render=self._render, release=self._release)
        except Exception:
            logger.debug("strip band enable failed", exc_info=True)
            return False
        self._band = band
        for name in _VIEW_SIGNALS:
            signal = getattr(timeline, name, None)
            if signal is None:
                continue
            try:
                signal.connect(self.on_view_changed)
            except Exception:
                logger.debug("strip band signal %s not connected", name, exc_info=True)
        self.on_view_changed()
        return True

    def detach(self) -> None:
        """Disconnect, drop the window and disable the band (idempotent)."""
        timeline = self._timeline()
        for name in _VIEW_SIGNALS:
            signal = getattr(timeline, name, None) if timeline is not None else None
            if signal is None:
                continue
            try:
                signal.disconnect(self.on_view_changed)
            except (TypeError, RuntimeError):
                pass
        self._drop_window("host detached")
        disable = getattr(timeline, "disable_strip_band", None) if timeline is not None else None
        if callable(disable):
            try:
                disable()
            except Exception:
                logger.debug("strip band disable failed", exc_info=True)
        self._band = None

    def cleanup(self) -> None:
        self.detach()
        self.view = None

    # -- view changes ---------------------------------------------------

    def on_view_changed(self, *_args) -> None:
        """Scroll/zoom/resize tick: place the band and build the new window."""
        band = self._band
        timeline = self._timeline()
        if band is not None and timeline is not None:
            self._place(band, timeline)
        self._ensure_window("view change")

    def on_recording_changed(self, *_args) -> None:
        """The recording was cut/undone or the fps changed: new content."""
        self._key = None
        self._ensure_window("recording change")

    def _place(self, band, timeline) -> None:
        """Re-place the band over the strip row (synchronous, idempotent)."""
        rect = getattr(timeline, "strip_band_rect", None)
        place = getattr(band, "place", None)
        if not callable(rect) or not callable(place):
            return
        try:
            place(rect())
        except Exception:
            logger.debug("strip band placement failed", exc_info=True)

    # -- window lifecycle ----------------------------------------------

    def _ensure_window(self, reason: str):
        """Build the window for the current view unless it is already built."""
        timeline = self._timeline()
        if timeline is None:
            return None
        try:
            fps = float(self.editor_service.get_fps())
        except Exception:
            logger.debug("strip window skipped: no fps", exc_info=True)
            return None
        current = timeline_strip_layout(timeline, fps=fps)
        if current is None:
            return self._window
        layout, first_block, count = current
        try:
            recording = self.editor_service.get_current_recording()
        except Exception:
            logger.debug("strip window skipped: no recording", exc_info=True)
            return self._window
        if recording is None:
            return self._window
        tile_w, tile_h = (int(v) for v in layout.tile_size)
        key = (id(recording), int(first_block), int(count), tile_w, tile_h)
        if key == self._key and self._window is not None:
            return self._window
        self._key = key
        adapter = StripRecordingAdapter(
            recording,
            proxy_pair=self._proxy_pair(),
            layout=layout,
            fps=layout.fps,
        )
        try:
            window = self._builder(
                adapter,
                int(first_block),
                int(count),
                tile_w,
                tile_h,
                request=self._request(tile_w, tile_h),
                layout=layout,
                rhi=self._rhi(),
            )
        except Exception as exc:
            # Fail-open: the previous window keeps drawing (a stale strip is a
            # better frame than a blank band). The renderer turns a missing
            # plan/source into placeholder tiles on its own.
            self.build_errors += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.debug("strip window build failed (%s)", reason, exc_info=True)
            return self._window
        self.build_count += 1
        self.last_error = None
        self._window = window
        return window

    def window(self):
        """The live window, or ``None`` before the first successful build."""
        return self._window

    def window_key(self):
        """The ``(recording, first block, count, tile)`` key of the live window."""
        return self._key

    def _drop_window(self, reason: str) -> None:
        window = self._window
        self._window = None
        self._key = None
        if window is not None:
            try:
                cancel(window, reason)
            except Exception:
                logger.debug("strip window cancel failed", exc_info=True)

    # -- the band's GPU callbacks --------------------------------------

    def _render(self, command_buffer, band) -> None:
        window = self._ensure_window("render")
        if window is None:
            return
        target = band.renderTarget()
        try:
            render_window(
                target,
                window,
                rhi=band.rhi(),
                command_buffer=command_buffer,
            )
        except StripWindowCancelled:
            # A newer window superseded this one between build and render;
            # the in-flight window is never applied.
            return
        except Exception:
            logger.debug("strip window render failed", exc_info=True)
            return
        self.render_count += 1

    def _release(self) -> None:
        band = self._band
        rhi = None
        if band is not None:
            try:
                rhi = band.rhi()
            except Exception:
                rhi = None
        try:
            from tabs.image_compare.services.video_snapshot_rendering.strip_pass import (
                release_window_resources,
            )

            release_window_resources(rhi)
        except Exception:
            logger.debug("strip pass release failed", exc_info=True)
        self._drop_window("gpu resources released")

    # -- small guards ----------------------------------------------------

    def _timeline(self):
        view = self.view
        if view is None:
            return None
        try:
            return getattr(view, "timeline", None)
        except RuntimeError:
            return None

    def _exporter(self):
        exporter = self.export_controller
        exporter = getattr(exporter, "video_exporter", None) or exporter
        return exporter

    def _proxy_pair(self):
        exporter = self._exporter()
        if exporter is None:
            return lambda _snapshot, _size: None
        pair = proxy_pair_for(exporter, auto_crop=self._auto_crop)
        if pair is None:
            return lambda _snapshot, _size: None
        return pair

    def _request(self, tile_w: int, tile_h: int):
        exporter = self._exporter()
        build = getattr(exporter, "_build_render_request", None)
        if callable(build):
            try:
                return build(
                    tile_w,
                    tile_h,
                    None,
                    self._auto_crop,
                    False,
                    None,
                    (0, 0, 0, 0),
                )
            except Exception:
                logger.debug("exporter render request failed", exc_info=True)
        return render_request(tile_w, tile_h)

    def _rhi(self):
        band = self._band
        if band is None:
            return None
        try:
            return band.rhi()
        except Exception:
            return None


__all__ = ["StripBandHost", "render_request"]
