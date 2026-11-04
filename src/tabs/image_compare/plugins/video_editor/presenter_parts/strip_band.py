# Audit-Meta: pattern=single-owner size=exempt reason="one strip band host: the window lifecycle (build, fail-open on a bad build, supersede, release) and the level cache fill share one state machine — _window/_key/_layout/_adapter plus the fill's latest-wins token and resume cursor all describe 'which view am I drawing and what prepared it'; splitting the fill out would leave the host holding half a state machine and the fill reaching back into it"
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

Window lifecycle: one window per (recording, first block, block count, LEVEL).
Building a newer window supersedes the older one inside ``strip_window``; a
superseded window raises ``StripWindowCancelled`` at render time and draws
nothing. A build that raises is logged and the previous window stays live
(fail-open — placeholder tiles are the renderer's job, a stale frame beats a
blank band). Planned tiles outlive a window (``StripTileCache``, cleared on a
recording change) and are cached PER LEVEL, so a scroll tick plans only the
block that newly entered the range; a level change is a swap when that level
was planned ahead (``prepare_adjacent_level``) and a cold plan when it was not
(``window.level_warm`` says which). The band's ONE ``TileTextureService``
outlives a window the same way (dropped on release/detach or a content change),
so the tiles that stayed visible keep the array layers their pixels are already
in and only the newly exposed block is uploaded.

Level identity: :func:`level_for_layout` takes the tile size from the
timeline's own layout (``timeline_strip_layout`` → ``strip_block_layout``) and
the band target's device pixel ratio, never a second tile formula. The level the
user is about to zoom into is cache-filled ahead by
:meth:`StripBandHost.prepare_adjacent_level`, which is explicitly NOT on the
frame path (see its docstring).

Geometry single source: :func:`timeline_strip_layout` asks the timeline's own
``strip_block_layout`` for the visible blocks (its representative frames and
the block width that locates the first one), and takes the tile from
:func:`strip_recording.strip_tile_size` — one filmstrip thumbnail, never the
block cell's aspect. So this module holds no block formula and no tile formula
of its own. The per-frame QPainter thumbnail pipeline was deleted in phase 3;
this is the only strip renderer left.
"""

from __future__ import annotations

import logging

from tabs.image_compare.plugins.video_editor.presenter_parts.common import (
    VIDEO_EDITOR_AUTO_CROP,
)
from tabs.image_compare.plugins.video_editor.services.strip_recording import (
    MAX_BLOCKS,
    StripRecordingAdapter,
    proxy_pair_for,
    timeline_strip_layout,
)
from tabs.image_compare.services.video_snapshot_rendering.strip_window import (
    LEVEL_PREPARE_BUDGET_MS,
    StripTileCache,
    StripWindowCancelled,
    build_window,
    cancel,
    level_for_layout,
    next_level,
    prepare_level,
    render_window,
)
from core.log_names import APP_LOGGER_NAME

logger = logging.getLogger(APP_LOGGER_NAME)

#: Layers the band's one array allocator may hand out: two per tile side of a
#: full window (``MAX_BLOCKS`` blocks). The allocator lives as long as the band,
#: so a block that stayed visible keeps the layer its pixels are already in and
#: a scroll tick uploads only what newly entered the range.
STRIP_ARRAY_CAPACITY = 2 * int(MAX_BLOCKS)

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
        tile_cache=None,
        prepare_budget_ms: float = LEVEL_PREPARE_BUDGET_MS,
        prepare_plan_builder=None,
    ):
        self.view = view
        self.editor_service = editor_service
        self.export_controller = export_controller
        self._builder = builder
        self._auto_crop = bool(auto_crop)
        self._band = None
        self._window = None
        self._key = None
        self._layout = None
        self._adapter = None
        #: Planned tiles survive a scroll tick (one new block is planned, the
        #: rest are reused) and a level change (when the new level was prepared
        #: ahead); cleared when the recording's content changes.
        self._tile_cache = tile_cache if tile_cache is not None else StripTileCache()
        #: The plan builder a preparation slice uses. ``None`` = the level
        #: cache's own (one renderer per recording, see ``StripTileCache``).
        self._prepare_plan_builder = prepare_plan_builder
        #: One slice of the adjacent-level cache fill, in ms (the FILL budget's
        #: headroom, never an interaction tick).
        self._prepare_budget_ms = float(prepare_budget_ms)
        #: Latest-wins token for the cache fill: bumped whenever the content or
        #: the window it was planning for changes, which abandons a slice.
        self._prepare_token = 0
        #: The level whose next rung was already cache-filled. A scroll tick must
        #: NOT start another fill (it is an interaction tick, not a level entry),
        #: so the trigger is "a level was first planned", not "the window was
        #: cold".
        self._prepared_level = None
        #: ``(level, first_block, count, next_offset)`` of an unfinished slice,
        #: so the next level entry resumes instead of re-planning.
        self._prepare_cursor = None
        #: The band's ONE texture-array allocator: created on first build,
        #: dropped with the window on release/detach. Keeping it across ticks is
        #: what lets a scroll tick skip re-uploading the blocks that stayed
        #: visible — the pass compares the slot a tile draws from, and a fresh
        #: allocator per window would number every layer from zero again.
        self._service = None
        #: Diagnostics: window builds, build failures kept fail-open, frames
        #: whose window was rendered, and the last failure text.
        self.build_count = 0
        self.build_errors = 0
        self.render_count = 0
        self.last_error: str | None = None
        #: Level bookkeeping: the level of the last window, how many level
        #: changes were a swap of prepared plans vs a cold plan, and the last
        #: preparation report. ``window.level_warm`` carries the same bit.
        self.level = None
        self.level_changes = 0
        self.warm_level_changes = 0
        self.cold_level_changes = 0
        self.prepare_runs = 0
        self.prepare_planned = 0
        self.last_preparation = None

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
        self._tile_cache.clear()
        self._drop_service()
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
        """Scroll/zoom/resize tick: place the band and build the new window.

        ``prepare_level=True`` is the ONLY trigger of the adjacent-level cache
        fill, and it fires on a view change (a fill tick) — never from
        ``_render``. See :meth:`prepare_adjacent_level`.
        """
        band = self._band
        timeline = self._timeline()
        if band is not None and timeline is not None:
            self._place(band, timeline)
        self._ensure_window("view change", prepare_level=True)

    def on_recording_changed(self, *_args) -> None:
        """The recording was cut/undone or the fps changed: new content."""
        self._key = None
        self._tile_cache.clear()
        # The level cache just lost the prepared rung: the same level is a NEW
        # level entry for this host (its plans are gone), so prepare again.
        self._prepared_level = None
        # An in-flight/finished preparation planned the pre-cut frames: the level
        # it filled is void, so abandon it and start over from the new content.
        self._abandon_preparation("recording changed")
        # The content behind every tile key just changed while the recording's
        # identity did not: drop the allocator AND force the pass to re-upload,
        # or a cut would be painted from the pre-cut pixels (the one failure
        # class this path must never ship — a cell showing the wrong frame).
        self._drop_service()
        self._ensure_window("recording change", prepare_level=True)

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

    def _ensure_window(self, reason: str, *, prepare_level: bool = False):
        """Build the window for the current view unless it is already built.

        ``prepare_level`` is the cache-fill trigger and is **never** passed from
        ``_render``: a render tick must not plan another level (see
        :meth:`prepare_adjacent_level`). Only a view/recording change — the fill
        class — may start one, and only when the build entered a LEVEL this host
        has not filled yet (a scroll tick keeps its level and never prepares).
        """
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
        # The level is the layout's tile size at the band target's DPR — one
        # function, no second tile formula in this host.
        level = level_for_layout(layout, self._dpr())
        tile_w, tile_h = level.tile_size
        key = (id(recording), int(first_block), int(count), level)
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
                tile_cache=self._tile_cache,
                tile_service=self.array_service(),
                dpr=float(level.dpr),
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
        self._layout = layout
        self._adapter = adapter
        self._record_level(window, reason)
        level = getattr(window, "level", None)
        if prepare_level and level is not None and level != self._prepared_level:
            # A level was just planned for the first time: cache-fill the one the
            # user would zoom into. Bounded, and only here (a fill tick) — a
            # scroll tick keeps the same level and never reaches this.
            self._prepared_level = level
            try:
                self.prepare_adjacent_level()
            except Exception:
                # The fill is an optimisation, never a correctness path: a failure
                # leaves the level cold and the tick plans it (the honest
                # fallback), so it must not break the view change that started it.
                logger.debug("strip adjacent level prepare failed", exc_info=True)
        return window

    def _record_level(self, window, reason: str) -> None:
        """Count a level change and stamp it on the window (warm or cold)."""
        previous = self.level
        changed = previous is not None and getattr(window, "level", None) != previous
        try:
            window.level_changed = bool(changed)
        except Exception:
            pass
        if changed:
            self.level_changes += 1
            if getattr(window, "level_warm", False):
                self.warm_level_changes += 1
            else:
                self.cold_level_changes += 1
            logger.debug(
                "strip level change (%s): %s -> %s (%s, %s)",
                reason,
                previous.to_dict() if hasattr(previous, "to_dict") else previous,
                window.level.to_dict() if hasattr(window.level, "to_dict") else None,
                "warm" if getattr(window, "level_warm", False) else "cold",
                f"{float(getattr(window, 'build_ms', 0.0)):.2f} ms",
            )
        self.level = getattr(window, "level", None)

    def window(self):
        """The live window, or ``None`` before the first successful build."""
        return self._window

    def window_key(self):
        """The ``(recording, first block, count, level)`` key of the live window."""
        return self._key

    def array_service(self):
        """The band's one texture-array allocator (created on first need).

        ``STRIP_ARRAY_CAPACITY`` is two layers per tile of a full window, so
        the allocator can hold one maximal window inside one texture array —
        the pass binds ``tile_arrays[0]`` only.
        """
        if self._service is None:
            from engine.cache.tile_texture_service import TileTextureService

            self._service = TileTextureService(
                max_array_size=STRIP_ARRAY_CAPACITY
            )
        return self._service

    def _drop_service(self) -> None:
        """Drop the allocator (and make the pass re-upload) — identity is void."""
        self._service = None
        try:
            from tabs.image_compare.services.video_snapshot_rendering.strip_pass import (
                forget_uploads,
            )

            forget_uploads(self._rhi())
        except Exception:
            logger.debug("strip upload bookkeeping not dropped", exc_info=True)

    def _drop_window(self, reason: str) -> None:
        window = self._window
        self._window = None
        self._key = None
        self._layout = None
        self._adapter = None
        self._prepared_level = None
        # No window, no prepared level to swap into: abandon a pending fill so a
        # later tick cannot mistake it for the new window's preparation.
        self._abandon_preparation(reason)
        if window is not None:
            try:
                cancel(window, reason)
            except Exception:
                logger.debug("strip window cancel failed", exc_info=True)

    # -- the adjacent level: a cache fill, never a frame --------------------

    def prepare_adjacent_level(self, *, budget_ms=None):
        """Cache-fill the level the next zoom steps into. **Not the frame path.**

        Contract (what makes the level-change tick a swap instead of a re-plan):

        * started ONLY from a view/recording change, never from ``_render``: a
          render tick gets ``_ensure_window("render")`` with the default
          ``prepare_level=False``, so a frame can never pay for planning another
          level (the interaction budget is the whole point of this split);
        * it is a cache fill — :func:`prepare_level` plans tiles into the level
          cache and does not build/adopt a window, does not touch the visible
          window or its generation, and does not touch the band's
          ``TileTextureService`` (no layer is registered, released or moved);
        * bounded (``budget_ms``, default ``LEVEL_PREPARE_BUDGET_MS``) and
          resumable: what did not fit is picked up by the next level entry;
        * latest-wins and interruptible: ``should_continue`` compares a token
          that :meth:`_abandon_preparation` bumps (a recording change, a dropped
          window, a detach), so a fill stops at the next tile instead of
          finishing work nobody can use.

        Returns the :class:`LevelPreparation` report, or ``None`` when there is
        nothing to prepare (no window yet, or the layout is already at the layer
        ceiling so there is no adjacent level).
        """
        window = self._window
        adapter = self._adapter
        layout = self._layout
        if window is None or adapter is None or layout is None:
            return None
        current = getattr(window, "level", None)
        if current is None:
            return None
        target = next_level(current)
        if target is None:
            # Already at the array-layer ceiling: no rung to prepare, and a
            # "same level" would be a pointless second plan pass.
            return None
        first_block = int(window.first_block)
        count = int(window.count)
        start = 0
        cursor = self._prepare_cursor
        if (
            cursor is not None
            and cursor[0] == target
            and cursor[1] == first_block
            and cursor[2] == count
        ):
            start = int(cursor[3])
        if start >= count:
            self._prepare_cursor = None
            return self.last_preparation
        token = self._prepare_token
        prepared_for = window

        def _continue() -> bool:
            return self._prepare_token == token and self._window is prepared_for

        tile_w, tile_h = target.tile_size
        report = prepare_level(
            adapter,
            target,
            first_block + start,
            count - start,
            request=self._request(tile_w, tile_h),
            layout=layout,
            plan_builder=self._prepare_plan_builder,
            tile_cache=self._tile_cache,
            budget_ms=(
                self._prepare_budget_ms if budget_ms is None else float(budget_ms)
            ),
            should_continue=_continue,
        )
        self.prepare_runs += 1
        self.prepare_planned += int(report.planned)
        self.last_preparation = report
        if report.stopped == "budget" and report.examined < count - start:
            self._prepare_cursor = (
                target,
                first_block,
                count,
                start + int(report.examined),
            )
        else:
            self._prepare_cursor = None
        logger.debug(
            "strip level prepare %s: planned %d, reused %d, placeholders %d, "
            "%s%s",
            target.to_dict(),
            int(report.planned),
            int(report.reused),
            int(report.placeholders),
            f"{report.build_ms:.2f} ms",
            f", stopped={report.stopped}" if report.stopped else "",
        )
        return report

    def _abandon_preparation(self, reason: str) -> None:
        """Invalidate a pending/finished cache fill (latest-wins)."""
        self._prepare_token += 1
        self._prepare_cursor = None
        if self.last_preparation is not None:
            logger.debug("strip level prepare abandoned (%s)", reason)

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
        self._service = None
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

    def _dpr(self) -> float:
        """The band target's device pixel ratio (the level's second axis).

        Read from the band — the surface the tiles are drawn into — and fall
        back to the timeline widget, then to 1.0. Never a ``QScreen`` lookup:
        the DPR that matters is the one the target actually renders at.
        """
        for owner in (self._band, self._timeline()):
            getter = getattr(owner, "devicePixelRatioF", None)
            if not callable(getter):
                continue
            try:
                return float(getter())
            except Exception:
                continue
        return 1.0


__all__ = ["STRIP_ARRAY_CAPACITY", "StripBandHost", "render_request"]
