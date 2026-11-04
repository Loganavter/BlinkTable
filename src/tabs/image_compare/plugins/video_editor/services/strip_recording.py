"""Recording adapter for the GPU filmstrip band (strip atlas, phase 2).

``strip_window.build_window`` is duck-typed: it asks the recording for
``evaluate_at(timestamp)``, ``images_for(snapshot, tile_size)`` and a
``layout``. The editor recording knows the first two; the BLOCK geometry
belongs to the timeline (``strip_block_layout`` — block x, block width and the
block's representative frame — plus ``get_base_tile_width`` for the base
tile). This module is the one place that joins them, so the renderer never
carries a second geometry formula:

* :func:`timeline_strip_layout` converts the timeline's visible blocks into a
  :class:`StripBlockLayout` the renderer accepts. ``block_time`` maps a block
  to its representative FRAME (the toolkit's own ``rep_frame``) over the
  timeline fps — a block is not a frame: at the fitted zoom one block spans
  several frames, and ``block / fps`` would ask the recording for the wrong
  instant (owner-visible: every tile showing frame 0's neighbourhood).
* :func:`strip_tile_size` is the tile geometry: one strip BLOCK CELL in device
  pixels — ``strip_block_layout``'s block width times the strip row height,
  scaled by the target DPR and clamped to one array layer, aspect preserved.
  The cell is 16:9 at every reachable zoom (measured 2026-11-04), so the tile
  is too, and ``is_thumbnail_target_size`` (the plan builder's cover gate) reads
  the thumbnail's aspect rather than an exact 160x90.
* :class:`StripRecordingAdapter` is the object handed to ``build_window``:
  ``evaluate_at`` delegates to the recording, ``images_for`` delegates to the
  exporter's proxy seam (never a decode on the interaction path).
* :func:`proxy_pair_for` builds that proxy seam out of
  ``VideoExporterService._resolve_thumbnail_images`` — one source resolution
  for every strip tile (pyramid coarse level / display cache, or a miss).

Nothing here is GPU-aware and nothing here waits: one pure-CPU call per view
change, no timer, no settle gate, no worker.
"""

from __future__ import annotations

from dataclasses import dataclass

from sli_ui_toolkit.ui.widgets.composite.timeline_widget import (
    viewport as timeline_viewport,
)

from tabs.image_compare.services.video_snapshot_rendering.geometry import (
    THUMBNAIL_TARGET_SIZE,
)
from tabs.image_compare.services.video_snapshot_rendering.strip_window import (
    recording_scope,
    LAYER_PX,
    StripBlockLayout,
)

#: Blocks in one window. The window is one texture array with two layers per
#: tile, and a band shows ~viewport/base-tile blocks (11 for a 1200 px strip,
#: 60 for a 4K one); the cap keeps one window's array and upload batch bounded
#: without ever dropping the blocks the user is looking at on a normal screen.
MAX_BLOCKS = 64


@dataclass(frozen=True)
class TimelineStripLayout(StripBlockLayout):
    """The timeline's strip blocks as the renderer's block layout.

    ``block_frames`` are the block representatives the toolkit's own
    ``strip_block_layout`` returned for the visible window (``first_block`` is
    the absolute block index of ``block_frames[0]``). ``block_time`` therefore
    reproduces the exact frame the QPainter strip would have drawn, while the
    tile size / fps / column count stay plain layout data.

    ``block_size`` is the CELL those blocks are drawn in — ``(draw_w, row
    height)`` from the timeline's own layout — and ``dpr`` the target ratio the
    tile was sized at. ``tile_size`` (the base class field ``build_window``
    validates against) is the device-pixel tile derived from them
    (:func:`strip_tile_size`), so the level a window plans at is
    ``(tile_size, dpr=1.0)``: the DPR is already IN the tile, and carrying it
    twice would make the same pixels two levels.
    """

    first_block: int = 0
    block_frames: tuple[int, ...] = ()
    block_size: tuple[int, int] = THUMBNAIL_TARGET_SIZE
    dpr: float = 1.0

    def block_time(self, block: int) -> float:
        index = int(block) - int(self.first_block)
        if 0 <= index < len(self.block_frames):
            return int(self.block_frames[index]) / float(self.fps)
        return super().block_time(block)


def strip_visible_span(timeline):
    """``(content_start_x, start_x, end_x)`` of the visible strip, or ``None``.

    Expressed with the toolkit's public geometry helpers only — the block
    geometry itself is never re-derived here (``strip_block_layout`` owns it);
    this is just the interval that layout is asked about, the same one the
    toolkit's own ``visible_strip_block_range`` uses. Degenerate view = no
    window (fail-open: the band keeps its previous frame).
    """
    try:
        scroll_area = timeline_viewport.get_scroll_area(timeline)
        scroll_x = (
            scroll_area.horizontalScrollBar().value()
            if scroll_area is not None
            else 0
        )
        content_start_x = float(timeline.LEFT_GUTTER)
        logical_width = float(timeline_viewport.get_logical_width(timeline))
        viewport_width = float(timeline_viewport.get_viewport_width(timeline))
        inset = float(timeline_viewport.right_inset(timeline))
    except Exception:
        return None
    if logical_width <= 0 or viewport_width <= 0:
        return None
    start_x = max(content_start_x, float(scroll_x))
    end_x = min(
        float(scroll_x) + viewport_width - inset,
        content_start_x + logical_width,
    )
    if end_x <= start_x:
        return None
    return (content_start_x, start_x, end_x)


def strip_tile_size(
    block_size: tuple[int, int] | None = None, dpr: float = 1.0
) -> tuple[int, int]:
    """The band's tile geometry: one strip BLOCK CELL, in DEVICE pixels.

    ``block_size`` is the cell the tile is stretched over, as the timeline's
    own layout declares it: ``strip_block_layout``'s ``draw_w`` (the block
    width) plus the strip row height (``strip_row_height``). One tile covers
    exactly one cell, so the tile IS the cell — at the target's device pixel
    ratio, because the band renders into a QRhi target sized in device pixels.

    ``block_size=None`` keeps the historical filmstrip thumbnail
    (``THUMBNAIL_TARGET_SIZE``) for a caller with no layout in hand (the
    latency bench's default case). It is the same 16:9 shape, one block wide.

    Why the cell and not the old fixed thumbnail: a 160x90 tile in a 128x72
    cell (measured live 2026-11-03) was downscaled at 1x and upscaled 1.6x on
    a 2x screen — the band paid resample twice and showed a soft frame. Cell x
    DPR is the pixels the cell actually has.

    **What does NOT move this number: zoom.** Measured 2026-11-04 on the real
    timeline (viewport.strip_block_layout over the whole reachable range, the
    toolkit's fitted zoom 0.047 up to its 1.0 clamp): ``draw_w`` is constant
    128 px and the strip row is 72 px, so the cell — hence the tile — is
    constant. Zoom changes frames-per-block (21 at the fitted view, 1 at 1:1),
    never a cell's pixel size. The reachable user gesture that changes this
    tile is the target DPR (``StripBandHost._dpr``: the window moving to a
    HiDPI screen), which walks the level ladder exactly (128x72 -> 256x144).
    A zoom-driven rung needs a zoom-scaled cell from the toolkit layout; the
    layout exposes ``draw_w`` at the visible span and no zoom scalar, and the
    sibling toolkit checkout is not editable from this cell, so the mapping
    stays honest instead of inventing a second multiplier here.

    Clamped to one array layer (``LAYER_PX``) with the aspect PRESERVED: one
    tile is uploaded 1:1 into one array layer, so a 2.5x cell (320x180) caps
    at 256x144 instead of distorting one axis.
    """
    if block_size is None:
        width, height = THUMBNAIL_TARGET_SIZE
    else:
        width, height = int(block_size[0]), int(block_size[1])
    scale = max(0.1, float(dpr))
    tile_w = max(1, int(round(width * scale)))
    tile_h = max(1, int(round(height * scale)))
    longest = max(tile_w, tile_h)
    if longest > int(LAYER_PX):
        factor = float(LAYER_PX) / float(longest)
        tile_w = max(1, int(round(tile_w * factor)))
        tile_h = max(1, int(round(tile_h * factor)))
    return tile_w, tile_h


def strip_row_height(timeline, block_width: float) -> int:
    """The strip row's declared height, in px — the block cell's height.

    ``strip_block_layout`` returns ``(block_x, draw_w, rep_frame)``: it declares
    the block's WIDTH and no height. The height of the row those blocks are
    laid out in is the timeline's own ``STRIP_HEIGHT`` (the band is placed over
    exactly that row, see ``StripBandHost._place``), so that is what is read —
    never re-derived from the width.

    A stand-in timeline that declares no row height falls back to the block
    width at the thumbnail's aspect (the same 16:9 the measured cells are:
    128x72).
    """
    try:
        height = int(getattr(timeline, "STRIP_HEIGHT", 0) or 0)
    except Exception:
        height = 0
    if height > 0:
        return height
    thumb_w, thumb_h = THUMBNAIL_TARGET_SIZE
    return max(1, int(round(float(block_width) * thumb_h / float(thumb_w))))


def timeline_strip_layout(timeline, *, fps, dpr: float = 1.0):
    """``(layout, first_block, count)`` for the band's current view, or ``None``.

    One ``strip_block_layout`` call decides every number here: the first
    visible block, the block count, each block's representative frame and the
    block WIDTH (``draw_w``) — which is the cell's width, so the tile is sized
    from it rather than from a second formula (the cell's height is the strip
    row's, ``strip_row_height``). The tile is that cell at ``dpr`` (the target's
    device pixel ratio), because the band renders into a device-pixel target.

    Columns equal blocks because the band is one row — so tile ``i`` of the
    window lands on block ``i`` of the strip.
    """
    try:
        total = int(getattr(timeline, "_total_frames", 0) or 0)
    except Exception:
        return None
    if total <= 0 or not fps or float(fps) <= 0:
        return None
    span = strip_visible_span(timeline)
    if span is None:
        return None
    content_start_x, start_x, end_x = span
    try:
        blocks = timeline_viewport.strip_block_layout(
            timeline,
            content_start_x=content_start_x,
            start_x=start_x,
            end_x=end_x,
            overscan_blocks=0,
        )
    except Exception:
        return None
    if not blocks:
        return None
    draw_w = float(blocks[0][1])
    if draw_w <= 0.0:
        return None
    first_block = max(0, int(round((blocks[0][0] - content_start_x) / draw_w)))
    visible = blocks[:MAX_BLOCKS]
    block_size = (
        max(1, int(round(draw_w))),
        strip_row_height(timeline, draw_w),
    )
    tile_w, tile_h = strip_tile_size(block_size, dpr)
    if tile_w <= 0 or tile_h <= 0:
        return None
    layout = TimelineStripLayout(
        tile_size=(tile_w, tile_h),
        fps=float(fps),
        block_count=len(visible),
        cols=len(visible),
        first_block=first_block,
        block_frames=tuple(int(block[2]) for block in visible),
        block_size=block_size,
        dpr=float(dpr),
    )
    return layout, first_block, len(visible)


class StripRecordingAdapter:
    """The ``recording`` object ``build_window`` expects, over the editor.

    ``layout`` is set per view change (see :func:`timeline_strip_layout`);
    ``fps`` and ``block_count`` mirror it so the renderer's fallback path and
    its diagnostics read the same numbers the timeline drew.
    """

    def __init__(self, recording, *, proxy_pair, layout=None, fps=None):
        self._recording = recording
        self._proxy_pair = proxy_pair
        self.layout = layout
        fallback_fps = getattr(recording, "fps", 10.0)
        self.fps = float(fps or fallback_fps or 10.0)
        self.block_count = int(getattr(layout, "block_count", 0) or 0)

    @property
    def cache_key(self):
        """Stable identity of the underlying recording (strip plan cache).

        The adapter itself is rebuilt on every view change, so a plan cache
        scoped by the adapter would never hit; the recording it wraps is the
        content, and the host clears its cache when that content changes.

        The recording half comes from ``recording_scope``, never ``id()``: the
        scope now also names ARRAY SLOTS in the pass, so a collected recording
        whose address is reused by the next one must not inherit its layers
        (that would draw a cell from another frame's pixels).
        """
        return (recording_scope(self._recording), float(self.fps))

    def evaluate_at(self, timestamp):
        """The frame state at ``timestamp`` — the recording's own evaluation."""
        return self._recording.evaluate_at(float(timestamp))

    def images_for(self, snapshot, tile_size):
        """The session's proxy pair for ``snapshot`` at ``tile_size``.

        ``None`` means "no proxy at this size" — the renderer draws a
        fail-closed placeholder tile of its own layer. Never a decode: the
        seam is the exporter's pyramid/display-cache lookup.
        """
        pair = self._proxy_pair(snapshot, (int(tile_size[0]), int(tile_size[1])))
        if not pair:
            return None
        image1, image2 = pair[0], pair[1]
        if image1 is None or image2 is None:
            return None
        return (image1, image2)


def proxy_pair_for(exporter, *, auto_crop=False):
    """``pair_for(snapshot, tile_size)`` over the exporter's proxy seam.

    Uses ``VideoExporterService._resolve_thumbnail_images`` — the resolver the
    thumbnail and export paths already go through, including the injected
    session-state/pyramid resolver. Returns ``None`` when the exporter has no
    such seam (test doubles, a stale exporter), which the adapter turns into a
    placeholder tile rather than a decode.
    """
    resolve = getattr(exporter, "_resolve_thumbnail_images", None)
    if not callable(resolve):
        return None

    def _pair(snapshot, tile_size):
        (image1, image2), _kinds = resolve(
            snapshot,
            bool(auto_crop),
            (max(1, int(tile_size[0])), max(1, int(tile_size[1]))),
        )
        return (image1, image2)

    return _pair


__all__ = [
    "MAX_BLOCKS",
    "StripRecordingAdapter",
    "TimelineStripLayout",
    "proxy_pair_for",
    "strip_row_height",
    "strip_tile_size",
    "strip_visible_span",
    "timeline_strip_layout",
]
