"""Strip window: the visible filmstrip blocks as ONE GPU pass (CPU planning half).

Phase 1 of ``plan-video-strip-atlas-2026-10-30``. The video editor's strip used
to render one offscreen canvas per visible frame; the proven replacement
(``src/devtools/bench_strip_batch.py``, phase 0) plans N frames and draws them
into one target with N viewports, per-tile uniforms and per-tile texture-array
layers, byte-exact against the per-tile reference at N=2/8/17/32/64.

This module is that pass as production API:

* :func:`build_window` — CPU only: resolves each visible block's frame state
  through ``recording.evaluate_at(t)``, builds the per-tile plan through the
  same plan builder the export/thumbnail path uses, allocates the texture-array
  layers through ``TileTextureService`` and packs the per-tile uniform block +
  instance. Importable and testable offscreen (no QRhi). A caller that passes a
  :class:`StripTileCache` reuses the plan/pixels of blocks that stayed visible
  (one plan builder per recording, not per window) — that is what keeps a
  scroll tick inside the interaction budget. A caller that also passes the
  band's own ``TileTextureService`` reuses the SLOTS of those blocks, so the
  pass can skip re-uploading pixels it already has (see ``tile_source_key``).
* :func:`render_window` — one ``beginPass``/``endPass`` over the window; the
  GPU half lives in ``strip_pass`` and is imported lazily so this module stays
  RHI-free at import time.
* :func:`cancel` + generations — a newer window supersedes an older one, and a
  stale window is never applied. Cancellation is not debounce: no timers, no
  waiting for quiet, no per-tile workers.
* Placeholder contract — a block whose frame state, source pair or plan is
  missing becomes a placeholder tile of its OWN layer (fail-closed identity:
  a blank cell can never show a neighbour's pixels) and the window records why.

Geometry is single-source: block timing and the tile size come from the
:class:`StripBlockLayout` the caller passes in (the timeline's own layout), and
``build_window`` refuses a tile size the layout does not declare.
"""

# Audit-Meta: pattern=single-owner size=exempt reason="one strip window: block plan + the proven per-tile packing (letterbox, uniform block, instance, divider spec) share the tile record and must agree byte-for-byte with the phase-0 bench; the GPU half is the one allowed sibling (strip_pass.py) because it needs a live RHI to be tested at all"

from __future__ import annotations

import time
import weakref
from collections import OrderedDict
from dataclasses import dataclass

from PIL import Image

DEFAULT_TILE = (160, 90)
#: Array layer side, in px. Production uploads one tile 1:1 into one layer and
#: ``ArrayResources`` refuses an image larger than the layer, so a strip tile
#: must fit (the bench enforces the same precondition).
LAYER_PX = 256
#: QRhi requires dynamic uniform-buffer bindings to be 256-byte aligned.
UNIFORM_STRIDE = 256
#: What a placeholder tile draws. Fully transparent is the strip's own clear
#: colour, so "no frame here" reads as an empty cell and never as a frame.
PLACEHOLDER_RGBA = (0, 0, 0, 0)


class StripWindowError(RuntimeError):
    """The window cannot be built or applied as asked."""


class StripWindowCancelled(StripWindowError):
    """A stale/cancelled window reached ``render_window`` — nothing is drawn."""


# ---------------------------------------------------------------------------
# Window planning (pure CPU)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StripBlockLayout:
    """The timeline's block layout — the single source of block geometry.

    ``build_window`` never derives a second formula: it asks this layout for
    the block's timestamp and uses the layout's declared tile size, refusing a
    caller-supplied tile size that disagrees.
    """

    tile_size: tuple[int, int] = DEFAULT_TILE
    fps: float = 10.0
    block_count: int = 0
    #: None = the square-ish column count (``cols_for_count``); the strip widget
    #: passes its real visible-column count.
    cols: int | None = None

    def block_time(self, block: int) -> float:
        return int(block) / float(self.fps)

    def cols_for(self, count: int) -> int:
        if self.cols is not None:
            return max(1, min(int(self.cols), max(1, int(count))))
        return cols_for_count(count)


def cols_for_count(count: int) -> int:
    """Columns for ``count`` tiles, square-ish — the one grid formula."""
    count = max(1, int(count))
    cols = 1
    while cols * cols < count:
        cols += 1
    return min(cols, count)


def grid_for(count: int, tile_w: int, tile_h: int, cols: int | None = None):
    """``(cols, rows, width, height)`` of the window target."""
    count = max(1, int(count))
    tile_w, tile_h = max(1, int(tile_w)), max(1, int(tile_h))
    cols = cols_for_count(count) if cols is None else max(1, min(int(cols), count))
    rows = -(-count // cols)
    return cols, rows, cols * tile_w, rows * tile_h


def tile_rect(index: int, cols: int, tile_w: int, tile_h: int):
    """``(x, y, w, h)`` of one tile in the window target (row-major fill)."""
    row, col = divmod(int(index), max(1, int(cols)))
    return (
        col * int(tile_w),
        row * int(tile_h),
        int(tile_w),
        int(tile_h),
    )


def resolve_layout(recording, tile_w: int, tile_h: int, layout=None) -> StripBlockLayout:
    """The layout to plan against, and the tile-size single-source check."""
    if layout is None:
        layout = getattr(recording, "strip_layout", None) or getattr(
            recording, "layout", None
        )
    if layout is None:
        layout = StripBlockLayout(
            tile_size=(int(tile_w), int(tile_h)),
            fps=float(getattr(recording, "fps", 10.0) or 10.0),
            block_count=int(getattr(recording, "block_count", 0) or 0),
        )
    else:
        declared = tuple(int(v) for v in layout.tile_size)
        if declared != (int(tile_w), int(tile_h)):
            raise StripWindowError(
                f"tile size {(int(tile_w), int(tile_h))} disagrees with the "
                f"timeline layout's declared {declared}: block layout and tile "
                "size have exactly one source (the layout)"
            )
    return layout


@dataclass(slots=True)
class StripTile:
    """Everything the one-pass renderer needs for one strip cell."""

    index: int
    block: int
    timestamp: float
    rect: tuple[int, int, int, int]
    plan: object | None
    image1: object | None
    image2: object | None
    canvas_w: int
    canvas_h: int
    tile_w: int
    tile_h: int
    uniforms: bytes = b""
    instance: bytes = b""
    #: Divider overlay block; packed during the pass (a divider's clip matrix
    #: only exists once the overlay shell is mounted on the target).
    divider: bytes = b""
    #: Texture-array layers the PRODUCTION allocator handed this tile's two
    #: sides (never ``index * 2``).
    layer1: int = -1
    layer2: int = -1
    #: Why this cell is a placeholder (``None`` for a real frame). Fail-closed:
    #: a placeholder still owns its own layers and its own flat image.
    placeholder_reason: str | None = None

    @property
    def is_placeholder(self) -> bool:
        return self.placeholder_reason is not None


@dataclass(slots=True)
class StripWindow:
    """One visible strip range, planned and packed for a single GPU pass."""

    generation: int
    first_block: int
    count: int
    tile_w: int
    tile_h: int
    cols: int
    rows: int
    target_w: int
    target_h: int
    tiles: tuple[StripTile, ...] = ()
    #: The allocator this window's layers came from. The BAND HOST owns it and
    #: hands the same one to every window, so a block that stayed visible keeps
    #: its slot (and its uploaded pixels) across a scroll tick.
    tile_service: object | None = None
    #: Identity of the recording the window's pixels belong to
    #: (:func:`_recording_token`). Half of every tile's source key: the same
    #: block at the same tile size in another window of the same recording must
    #: address the SAME layer, and another recording or another zoom must not.
    source_scope: object = None
    layer_count: int = 0
    layer_px: int = LAYER_PX
    build_ms: float = 0.0
    cancelled: bool = False
    cancel_reason: str = ""
    applied: bool = False
    #: Planned tiles reused from :class:`StripTileCache` / planned this build.
    cache_hits: int = 0
    cache_misses: int = 0

    @property
    def last_block(self) -> int:
        return self.first_block + self.count - 1

    @property
    def placeholder_tiles(self) -> tuple[StripTile, ...]:
        return tuple(tile for tile in self.tiles if tile.is_placeholder)

    @property
    def placeholder_reasons(self) -> dict[int, str]:
        return {
            int(tile.block): str(tile.placeholder_reason)
            for tile in self.tiles
            if tile.is_placeholder
        }

    def rects(self) -> tuple[tuple[int, int, int, int], ...]:
        return tuple(tile.rect for tile in self.tiles)

    def tile_source_key(self, tile: StripTile, side: int):
        """The allocator key for one tile side: CONTENT identity, no generation.

        A window generation is a scheduling fact ("a newer window supersedes
        this one"), never a content identity. The generation used to be in the
        key, so every scroll tick addressed brand-new slots and re-uploaded
        every visible tile — measured 2026-11-04 as 5.2/17.4/32.4 ms of render
        at 8/17/32 blocks against 0.8/1.3/2.4 ms for a draw-only frame of the
        SAME window. Two windows showing the same block at the same tile size
        for the same recording therefore name one slot; another recording, or
        a zoom (new tile size), is a different key by construction.

        A PLACEHOLDER is its own content class, not the frame at that block: a
        blank cell whose proxy arrives on the next tick must not be skipped as
        "already uploaded" — that would pin the empty cell over the frame
        (fail-closed identity, the same rule the placeholder tile exists for).
        """
        kind = "strip-placeholder" if tile.is_placeholder else "strip-tile"
        return (
            kind,
            self.source_scope,
            int(tile.block),
            int(tile.tile_w),
            int(tile.tile_h),
            int(side),
        )

    def to_dict(self) -> dict:
        return {
            "generation": self.generation,
            "first_block": self.first_block,
            "count": self.count,
            "tile": (self.tile_w, self.tile_h),
            "grid": (self.cols, self.rows),
            "target": (self.target_w, self.target_h),
            "placeholders": self.placeholder_reasons,
            "cancelled": self.cancelled,
            "cache": {"hits": int(self.cache_hits), "misses": int(self.cache_misses)},
        }


# ---------------------------------------------------------------------------
# Generations / cancellation
# ---------------------------------------------------------------------------

_GENERATION = 0
_LIVE_WINDOW: StripWindow | None = None


def next_generation() -> int:
    global _GENERATION
    _GENERATION += 1
    return _GENERATION


def current_generation() -> int:
    return _GENERATION


def live_window() -> StripWindow | None:
    return _LIVE_WINDOW


def is_current(window: StripWindow | None) -> bool:
    """Whether ``window`` may still be applied to a target."""
    return bool(window is not None and _LIVE_WINDOW is window and not window.cancelled)


def cancel(window: StripWindow, reason: str = "cancelled") -> None:
    """Abandon ``window``: it will never be applied (idempotent)."""
    global _LIVE_WINDOW
    if window is None:
        return
    window.cancelled = True
    if not window.cancel_reason:
        window.cancel_reason = str(reason)
    if _LIVE_WINDOW is window:
        _LIVE_WINDOW = None


def _adopt(window: StripWindow) -> StripWindow:
    """Register ``window`` as the live one; the previous window is superseded."""
    global _LIVE_WINDOW
    previous = _LIVE_WINDOW
    if previous is not None and previous is not window:
        cancel(previous, f"superseded by generation {window.generation}")
    _LIVE_WINDOW = window
    return window


# ---------------------------------------------------------------------------
# build_window
# ---------------------------------------------------------------------------


def _default_plan_builder():
    """The production plan builder the bench and the export path share."""
    from tabs.image_compare.services.video_snapshot_rendering.renderer import (
        SnapshotFrameRenderer,
    )

    renderer = SnapshotFrameRenderer(
        image_loader=lambda path, auto_crop=False: None
    )
    return renderer.prepare_canvas_frame_from_images


class StripTileCache:
    """Planned strip tiles kept across windows (LRU, one recording scope).

    A scroll tick moves the visible range by a block or two, so all but the
    newly exposed blocks are the same frames planned for the same tile size:
    planning them again is the interaction budget's whole cost (a window build
    is 16-25 ms of CPU, and 14 spill-mmap flushes were 24 ms of it before
    2026-11-04). Reusing the planned tile — plan, PIL pixels, canvas size —
    turns that tick into one plan plus one render.

    Scope and invalidation: the cache belongs to ONE recording (the caller
    clears it when the recording is cut/undone) and keys a tile by
    ``(block, tile_w, tile_h, request token)``, so a zoom (new tile size) is a
    miss by construction and a re-zoom to a seen size is a hit. Placeholder
    tiles are never cached: a missing proxy or a failed plan is transient, and
    caching it would pin a blank cell over a frame that arrives later.

    Bounded: ``capacity`` planned tiles (default 96 = 48 blocks at two sides,
    ~5 MB of 160x90 pixels), LRU beyond it. The memory is the plan's stores,
    so the cap matters.
    """

    def __init__(self, capacity: int = 96, plan_builder=None) -> None:
        self._capacity = max(1, int(capacity))
        self._token: object = None
        self._entries: OrderedDict = OrderedDict()
        #: The plan builder this scope plans with. It MUST outlive a window:
        #: ``SnapshotFrameRenderer`` owns ``FrameRenderCaches``, and a fresh
        #: renderer per window pays the cold proxy/prescale/plan-cache cost on
        #: its first tile — measured 2026-11-04 at ~13 ms of a tick that planned
        #: a single new block. One builder per recording is what the export path
        #: already does.
        self._plan_builder = plan_builder
        #: Diagnostics for the window's ``to_dict`` and the latency bench.
        self.hits = 0
        self.misses = 0

    def planner(self):
        """The plan builder for this scope (created on first use)."""
        if self._plan_builder is None:
            self._plan_builder = _default_plan_builder()
        return self._plan_builder

    def clear(self) -> None:
        self._entries.clear()
        self._token = None

    def _scope(self, token) -> None:
        if token != self._token:
            self._entries.clear()
            self._token = token

    def get(self, token, key):
        self._scope(token)
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return entry

    def put(self, token, key, tile: "StripTile") -> None:
        self._scope(token)
        self._entries[key] = tile
        self._entries.move_to_end(key)
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)


def _recording_token(recording):
    """Identity of the recording a plan belongs to (the cache's scope).

    Never a bare ``id()``: CPython hands a collected object's address to the
    next allocation, so a reopened recording would inherit the previous one's
    scope — a stale plan in the tile cache and, worse, a stale ARRAY LAYER in
    the pass (a cell drawn from another frame's pixels). The same recording
    object keeps its serial (a scroll tick still reuses its plans and slots),
    the serial dies with the object, and a new object can never collide with it.
    """
    token = getattr(recording, "cache_key", None)
    if token is not None:
        return token
    return _recording_serial(recording)


#: ``recording -> serial`` for objects with no content ``cache_key`` of their
#: own. Weak: the serial is only meaningful while its recording is alive.
_RECORDING_SCOPES: "weakref.WeakKeyDictionary[object, int]" = weakref.WeakKeyDictionary()
_SCOPE_SERIAL = 0


def _recording_serial(recording) -> object:
    """A per-object serial that cannot be reused after the object dies."""
    global _SCOPE_SERIAL
    try:
        existing = _RECORDING_SCOPES.get(recording)
        if existing is not None:
            return existing
        _SCOPE_SERIAL += 1
        _RECORDING_SCOPES[recording] = _SCOPE_SERIAL
        return _SCOPE_SERIAL
    except TypeError:
        # No weak support (a SimpleNamespace stand-in, a proxy): fall back to
        # the address, which is what every caller had before.
        return id(recording)


def recording_scope(recording):
    """The public identity of a recording (plan cache + array-slot scope).

    Same value :func:`_recording_token` uses for a recording that carries no
    ``cache_key`` of its own; exposed so an adapter that projects onto the
    underlying recording (``StripRecordingAdapter.cache_key``) can hand back
    THIS instead of the object's address. A bare ``id()`` there would defeat
    the serial exactly where it matters: the adapter is rebuilt per tick, so
    its key must come from the recording, and a reused address would alias a
    previous recording's plans and GPU layers.
    """
    return _recording_serial(recording)


def _request_token(request):
    """The request fields a plan's geometry depends on (cache key half)."""
    surface = getattr(request, "target_surface", None)
    bounds = getattr(request, "global_bounds", None)
    bounds_token = None
    if bounds is not None:
        bounds_token = (
            int(getattr(bounds, "pad_left", 0)),
            int(getattr(bounds, "pad_right", 0)),
            int(getattr(bounds, "pad_top", 0)),
            int(getattr(bounds, "pad_bottom", 0)),
            int(getattr(bounds, "base_width", 0)),
            int(getattr(bounds, "base_height", 0)),
        )
    return (
        int(getattr(surface, "width", 0) or 0),
        int(getattr(surface, "height", 0) or 0),
        tuple(getattr(surface, "fill_rgba", (0, 0, 0, 0)) or (0, 0, 0, 0)),
        bool(getattr(request, "auto_crop", False)),
        bool(getattr(request, "fit_content", False)),
        bounds_token,
    )


def _reposition(cached: StripTile, index: int, block: int, timestamp: float, rect) -> StripTile:
    """``cached``'s plan and pixels at a new window position."""
    return StripTile(
        index=int(index),
        block=int(block),
        timestamp=float(timestamp),
        rect=rect,
        plan=cached.plan,
        image1=cached.image1,
        image2=cached.image2,
        canvas_w=cached.canvas_w,
        canvas_h=cached.canvas_h,
        tile_w=cached.tile_w,
        tile_h=cached.tile_h,
    )


def _pil_image(source):
    """Plan image -> PIL, whether it is a PIL image or a ``TiledPixelStore``."""
    if source is None:
        return None
    if hasattr(source, "read_array") and hasattr(source, "size"):
        width, height = source.size
        return Image.fromarray(source.read_array((0, 0, width, height)), "RGBA")
    return source


def _pair_from(recording, snapshot, tile_size):
    """The source pair for one block, through the recording's own resolver."""
    resolver = getattr(recording, "images_for", None)
    if resolver is None:
        return None, "no source resolver"
    try:
        pair = resolver(snapshot, tile_size)
    except Exception as exc:  # a resolver failure is a blank cell, not a crash
        return None, f"source resolver failed: {exc}"
    if not pair or len(pair) < 2:
        return None, "no source pair"
    image1, image2 = pair[0], pair[1]
    if image1 is None or image2 is None:
        return None, "no source pixels"
    return (image1, image2), ""


def _placeholder_tile(
    index: int,
    block: int,
    timestamp: float,
    rect,
    tile_w: int,
    tile_h: int,
    reason: str,
) -> StripTile:
    flat = Image.new("RGBA", (int(tile_w), int(tile_h)), PLACEHOLDER_RGBA)
    return StripTile(
        index=index,
        block=block,
        timestamp=timestamp,
        rect=rect,
        plan=None,
        image1=flat,
        image2=flat,
        canvas_w=int(tile_w),
        canvas_h=int(tile_h),
        tile_w=int(tile_w),
        tile_h=int(tile_h),
        placeholder_reason=str(reason),
    )


def build_window(
    recording,
    first_block: int,
    count: int,
    tile_w: int,
    tile_h: int,
    *,
    request,
    layout=None,
    plan_builder=None,
    rhi=None,
    tile_service=None,
    generation: int | None = None,
    layer_px: int = LAYER_PX,
    tile_cache: "StripTileCache | None" = None,
) -> StripWindow:
    """Plan + pack ``count`` visible blocks starting at ``first_block``.

    ``recording`` provides ``evaluate_at(t)`` (required), ``images_for(snap,
    tile_size)`` (the session's proxy pair) and optionally ``layout`` /
    ``fps`` / ``block_count``. ``request`` is the same ``VideoRenderRequest``
    the export/thumbnail path builds per tile. ``rhi`` only feeds the uniform
    packer's clip-space correction; ``None`` is the identity used by CPU tests.

    The returned window is the live one: building another supersedes it, and
    :func:`render_window` refuses to apply a superseded/cancelled window.
    """
    global _GENERATION
    started = time.perf_counter()
    first_block = int(first_block)
    count = int(count)
    tile_w, tile_h = int(tile_w), int(tile_h)
    if count <= 0:
        raise StripWindowError("build_window needs count >= 1")
    if tile_w <= 0 or tile_h <= 0:
        raise StripWindowError("build_window needs a positive tile size")
    if max(tile_w, tile_h) > int(layer_px):
        raise StripWindowError(
            f"tile {tile_w}x{tile_h} exceeds the {int(layer_px)}px array layer "
            "(one tile is uploaded 1:1 into one layer)"
        )
    resolved = resolve_layout(recording, tile_w, tile_h, layout)
    gen = next_generation() if generation is None else int(generation)
    if generation is not None:
        # An explicit generation must not rewind the module counter (a later
        # window has to supersede this one).
        _GENERATION = max(_GENERATION, gen)

    cols, rows, target_w, target_h = grid_for(
        count, tile_w, tile_h, resolved.cols_for(count)
    )
    source_scope = _recording_token(recording)
    window = StripWindow(
        generation=gen,
        first_block=first_block,
        count=count,
        tile_w=tile_w,
        tile_h=tile_h,
        cols=cols,
        rows=rows,
        target_w=target_w,
        target_h=target_h,
        layer_px=int(layer_px),
        source_scope=source_scope,
    )
    planner = (
        plan_builder
        if plan_builder is not None
        else (tile_cache.planner() if tile_cache is not None else _default_plan_builder())
    )
    cache_token = source_scope if tile_cache is not None else None
    request_key = _request_token(request) if tile_cache is not None else None
    tiles: list[StripTile] = []
    for index in range(count):
        block = first_block + index
        timestamp = float(resolved.block_time(block))
        rect = tile_rect(index, cols, tile_w, tile_h)
        cache_key = (
            (int(block), int(tile_w), int(tile_h), request_key)
            if tile_cache is not None
            else None
        )
        if tile_cache is not None:
            cached = tile_cache.get(cache_token, cache_key)
            if cached is not None:
                window.cache_hits += 1
                tiles.append(_reposition(cached, index, block, timestamp, rect))
                continue
        window.cache_misses += 1
        snapshot, reason = None, ""
        try:
            snapshot = recording.evaluate_at(timestamp)
        except Exception as exc:
            reason = f"evaluate_at failed: {exc}"
        if not reason and snapshot is None:
            reason = "no frame state"
        pair, pair_reason = (None, reason) if reason else _pair_from(
            recording, snapshot, (tile_w, tile_h)
        )
        if pair is None:
            tiles.append(
                _placeholder_tile(
                    index,
                    block,
                    timestamp,
                    rect,
                    tile_w,
                    tile_h,
                    pair_reason or reason or "no source",
                )
            )
            continue
        image1, image2 = pair
        plan = None
        try:
            prepared = planner(snapshot, request, image1, image2)
            plan = getattr(prepared, "plan", prepared)
        except Exception as exc:
            tiles.append(
                _placeholder_tile(
                    index,
                    block,
                    timestamp,
                    rect,
                    tile_w,
                    tile_h,
                    f"plan failed: {exc}",
                )
            )
            continue
        plan_image1 = _pil_image(getattr(plan, "image1", image1))
        plan_image2 = _pil_image(getattr(plan, "image2", image2))
        if plan_image1 is None or plan_image2 is None:
            tiles.append(
                _placeholder_tile(
                    index,
                    block,
                    timestamp,
                    rect,
                    tile_w,
                    tile_h,
                    "plan carries no pixels",
                )
            )
            continue
        tile = StripTile(
            index=index,
            block=block,
            timestamp=timestamp,
            rect=rect,
            plan=plan,
            image1=plan_image1,
            image2=plan_image2,
            canvas_w=int(getattr(plan, "canvas_w", 0) or tile_w),
            canvas_h=int(getattr(plan, "canvas_h", 0) or tile_h),
            tile_w=tile_w,
            tile_h=tile_h,
        )
        tiles.append(tile)
        if tile_cache is not None:
            # Only a real frame is cacheable: a placeholder means "the proxy or
            # the plan was not ready", which the next tick may resolve.
            tile_cache.put(cache_token, cache_key, tile)

    service = _assign_layers(tiles, window, tile_service)
    window.tiles = tuple(tiles)
    window.tile_service = service
    for tile in tiles:
        tile.layer1, tile.layer2 = _layers_for(tile, service, window)
    # The array must address every slot the allocator actually handed out, not
    # just two per tile: a service that still holds a slot outside
    # ``2 * len(tiles)`` (a shorter window than the previous one, a clamp the
    # driver forced) would otherwise be drawn at a layer the array does not
    # have — ``tests/contracts/test_capacity_agreement.py``'s drift, in the
    # pass instead of at construction.
    window.layer_count = _layer_count_for(tiles)
    for tile in tiles:
        tile.uniforms = pack_tile_uniforms(rhi, tile)
        tile.instance = pack_tile_instance(tile, tile.layer1, tile.layer2, layer_px)
        if len(tile.uniforms) != uniform_block_size():
            raise StripWindowError(
                f"tile {tile.index}: uniform block {len(tile.uniforms)}B, "
                f"shader expects {uniform_block_size()}B"
            )
    window.build_ms = (time.perf_counter() - started) * 1000.0
    return _adopt(window)


def _layer_count_for(tiles) -> int:
    """Layers the bound array needs: two per tile, and every slot in use."""
    wanted = max(2, 2 * len(tiles))
    for tile in tiles:
        wanted = max(wanted, int(tile.layer1) + 1, int(tile.layer2) + 1)
    return wanted


#: Keys a caller-owned (shared) ``TileTextureService`` currently holds for a
#: strip window, per service — weak, because the service dies with the band
#: host. ``TileTextureService`` deliberately exposes no key enumeration, so
#: this is how a new window learns which keys left the range and must hand
#: their layers back. It is keyed by the service OBJECT, so two allocators can
#: never be confused about each other's slots.
_STRIP_KEYS: "weakref.WeakKeyDictionary[object, set]" = weakref.WeakKeyDictionary()


def _tracked_keys(service) -> set:
    """``service``'s live key set (a throwaway one when it cannot be weak-keyed)."""
    try:
        return _STRIP_KEYS.setdefault(service, set())
    except TypeError:
        # A stand-in that is unhashable or refuses weak references: tracking it
        # is impossible, so nothing is released and every tile re-uploads. That
        # is the fail-closed direction (a cost, never a wrong cell).
        return set()


def _release_departed(service, wanted: set) -> tuple:
    """Hand the layers of keys that left the range back to the allocator.

    Must run BEFORE the new window's keys are registered: a jump to a
    completely different range of 64 blocks would otherwise need both windows'
    layers at once (256 > the band's 128), open a second texture array and
    fail the build for a window one array can hold.
    """
    tracked = _tracked_keys(service)
    departed = [key for key in tracked if key not in wanted]
    if not departed:
        return ()
    invalidate = getattr(service, "invalidate_source", None)
    if not callable(invalidate):
        return ()
    for key in departed:
        invalidate(key)
        tracked.discard(key)
    return tuple(departed)


def _already_allocated(service, key, size) -> bool:
    """Whether ``service`` holds this exact key at this exact content size.

    Re-registering a live key would ``reset_source`` it (dropping its residency
    and freeing its slot), which is exactly the reuse this module is here to
    keep. A size that disagrees is a content change: treat it as new.
    """
    is_resident = getattr(service, "is_resident", None)
    content_size_for = getattr(service, "content_size_for", None)
    if not callable(is_resident) or not callable(content_size_for):
        return False
    try:
        if not is_resident(key, (0, 0)):
            return False
        return tuple(content_size_for(key, (0, 0)) or ()) == tuple(size)
    except Exception:
        return False


def _fit_service_capacity(service, want: int) -> None:
    """Let the allocator hand out ``want`` layers before this window allocates.

    The pass clamps the allocator to the layer count the driver actually
    accepted (``ArrayResources.set_capacity_sink``), so a band that grew from
    8 to 17 blocks must raise the ceiling again — otherwise the allocator opens
    a second texture array for a window one array can hold. Refusals are the
    service's own business (it never drops a live slot to obey one).
    """
    resize = getattr(service, "set_max_array_size", None)
    if not callable(resize):
        return
    # ``TileTextureService`` has no public getter for the ceiling it currently
    # allows; reading the allocator avoids a pointless shrink-and-regrow cycle
    # on every window.
    allocator = getattr(service, "_slot_allocator", None)
    current = getattr(allocator, "max_array_size", None)
    try:
        if current is not None and int(current) >= int(want):
            return
        resize(int(want))
    except (TypeError, ValueError):
        return


def _assign_layers(tiles, window: StripWindow, tile_service):
    """Allocate each tile side's array layer through ``TileTextureService``.

    Same production path as the bench: register a 1x1 grid per side, mark the
    ``(0, 0)`` tile resident, read the slot back through ``slot_for`` — the
    allocator owns capacity and free-list reuse, this module assigns no layer
    numbering of its own.

    ``tile_service`` is the band host's one service (``2 * MAX_BLOCKS`` layers):
    a side whose key is already resident keeps its slot, and keys that left the
    range are released before anything new is allocated. With no service the
    window gets a private allocator (CPU-only callers and tests).
    """
    from engine.cache.tile_texture_service import TileTextureService

    shared = tile_service is not None
    service = tile_service
    if service is None:
        service = TileTextureService(max_array_size=_layer_count_for(tiles))
    wanted = {
        window.tile_source_key(tile, side)
        for tile in tiles
        for side, _image in _tile_sides(tile)
    }
    if shared:
        _release_departed(service, wanted)
        _fit_service_capacity(service, 2 * len(tiles))
    for tile in tiles:
        for side, image in _tile_sides(tile):
            key = window.tile_source_key(tile, side)
            size = (int(image.width), int(image.height))
            if _already_allocated(service, key, size):
                continue
            service.register_source(key, size)
            service.mark_resident(key, (0, 0), size[0] * size[1] * 4, size)
            if shared:
                _tracked_keys(service).add(key)
    return service


def _tile_sides(tile: StripTile):
    return ((1, tile.image1), (2, tile.image2))


def _layers_for(tile: StripTile, service, window: StripWindow):
    layers = []
    for side, _image in _tile_sides(tile):
        slot = service.slot_for(window.tile_source_key(tile, side), (0, 0))
        if slot is None:
            raise StripWindowError(
                f"tile {tile.index} side {side}: no allocated array slot"
            )
        if int(slot[0]) != 0:
            # The pass binds exactly one texture array; a slot in a second array
            # would be drawn as a layer of array 0 — another tile's pixels.
            raise StripWindowError(
                f"tile {tile.index} side {side}: allocator opened array "
                f"{int(slot[0])}; a strip window must fit one texture array "
                f"({len(window.tiles)} tiles) — give the TileTextureService "
                "at least 2 layers per tile"
            )
        layers.append(int(slot[1]))
    return layers[0], layers[1]


# ---------------------------------------------------------------------------
# Per-tile packing (the proven bench layout, one production home)
# ---------------------------------------------------------------------------


def scene_field(scene, name: str, default=None):
    """Read one ``RenderScene`` field, tolerating a dict stand-in."""
    value = getattr(scene, name, None)
    if value is None and isinstance(scene, dict):
        value = scene.get(name)
    return default if value is None else value


def _fill_rgba(tile: StripTile):
    fill = getattr(tile.plan, "fill_rgba", None) if tile.plan is not None else None
    if fill is None and tile.plan is None:
        fill = PLACEHOLDER_RGBA
    return fill


def tile_letterbox_geometry(tile: StripTile):
    """``(letterbox, canvas_letterbox, content_rect_px)`` for one tile.

    Mirrors the export canvas path for an unpadded plan
    (``apply_plan_letterbox_from_clip`` in
    ``src/engine/cache/display_tier_service.py``): the plan's padded canvas
    frame is fitted into the render target (here: the tile, whose size equals
    the plan's canvas), then ``overlay_clip_rect`` (canvas px) is nested inside
    that frame. A placeholder tile has no plan and stays at full-target UVs.
    """
    from ui.canvas_infra.scene.frame_geometry import resolve_canvas_content_geometry

    widget_w = max(1, int(tile.tile_w))
    widget_h = max(1, int(tile.tile_h))
    canvas_w = max(1, int(tile.canvas_w))
    canvas_h = max(1, int(tile.canvas_h))
    full = (0.0, 0.0, 1.0, 1.0)
    none = (0.0, 0.0, 0.0, 0.0)
    whole = (0, 0, widget_w, widget_h)

    plan = tile.plan
    clip = scene_field(getattr(plan, "render_scene", None), "overlay_clip_rect")
    if (
        clip is None
        or not bool(getattr(plan, "geometry_letterbox", False))
        or bool(getattr(plan, "image_is_padded_composite", False))
    ):
        return full, none, whole
    geometry = resolve_canvas_content_geometry(
        widget_width=widget_w,
        widget_height=widget_h,
        image_width=canvas_w,
        image_height=canvas_h,
        virtual_layout=None,
    )
    inner = geometry.inner_rect_px
    if inner is None:
        return full, none, whole
    frame_x, frame_y, frame_w, frame_h = (float(v) for v in inner)
    if frame_w <= 0.0 or frame_h <= 0.0:
        return full, none, whole
    sx = frame_w / float(canvas_w)
    sy = frame_h / float(canvas_h)
    clip_x, clip_y, clip_w, clip_h = (float(v) for v in clip)
    letterbox = (
        (frame_x + clip_x * sx) / widget_w,
        (frame_y + clip_y * sy) / widget_h,
        clip_w * sx / widget_w,
        clip_h * sy / widget_h,
    )
    canvas_letterbox = (
        frame_x / widget_w,
        frame_y / widget_h,
        frame_w / widget_w,
        frame_h / widget_h,
    )
    content_rect = (
        int(round(frame_x + clip_x * sx)),
        int(round(frame_y + clip_y * sy)),
        max(1, int(round(clip_w * sx))),
        max(1, int(round(clip_h * sy))),
    )
    return letterbox, canvas_letterbox, content_rect


@dataclass(slots=True)
class _BaseImageView:
    """The production ``BaseImagePrimitive`` fields the array packer reads."""

    zoom: float
    pan_offset_x: float
    pan_offset_y: float
    split_position: float
    is_horizontal: bool
    channel_mode_int: int
    diff_mode_int: int
    letterbox1: tuple[float, float, float, float]
    letterbox2: tuple[float, float, float, float]
    canvas_letterbox: object
    letterbox_fill: object


def _base_image_view(tile: StripTile) -> _BaseImageView:
    """Export/video plans render the whole canvas: zoom 1, pan 0.

    The split is the scene's ``split_position_visual`` — the field the export
    canvas feeds both ``base.frag`` and ``DividerPass`` (``split_position``
    does not exist on ``RenderScene``).
    """
    scene = getattr(tile.plan, "render_scene", None)
    split = float(scene_field(scene, "split_position_visual", 0.5))
    split = max(0.0, min(1.0, split))
    letterbox, canvas_letterbox, _rect = tile_letterbox_geometry(tile)
    fill = _fill_rgba(tile)
    letterbox_fill = None
    if fill is not None and len(fill) >= 4 and float(fill[3]) > 0:
        letterbox_fill = (
            float(fill[0]) / 255.0,
            float(fill[1]) / 255.0,
            float(fill[2]) / 255.0,
            float(fill[3]) / 255.0,
        )
    return _BaseImageView(
        zoom=1.0,
        pan_offset_x=0.0,
        pan_offset_y=0.0,
        split_position=split,
        is_horizontal=bool(scene_field(scene, "is_horizontal", False)),
        channel_mode_int=int(scene_field(scene, "channel_mode_int", 0) or 0),
        diff_mode_int=int(scene_field(scene, "diff_mode_int", 0) or 0),
        letterbox1=letterbox,
        letterbox2=letterbox,
        canvas_letterbox=canvas_letterbox,
        letterbox_fill=letterbox_fill,
    )


def _load_uniform_module():
    from tabs.image_compare.canvas.rhi_renderer import uniforms as uniform_module

    return uniform_module


def uniform_block_size() -> int:
    return int(_load_uniform_module()._ARRAY_UNIFORM_BLOCK_SIZE)


class _FixedClipRhi:
    """Stand-in ``rhi`` for CPU-side packing: identity clip correction.

    ``pack_array_uniforms`` reads exactly one thing off the RHI — the backend's
    clip-space correction matrix — so CPU-only packing can use the identity and
    stay on the production packer instead of re-implementing std140.
    """

    def __init__(self) -> None:
        from PySide6.QtGui import QMatrix4x4

        self._matrix = QMatrix4x4()

    def clipSpaceCorrMatrix(self):  # noqa: N802 - QRhi API name
        return self._matrix


def pack_tile_uniforms(rhi, tile: StripTile) -> bytes:
    """Production uniform packing for one tile (``_ARRAY_UNIFORM_BLOCK_SIZE``)."""
    packing_rhi = _FixedClipRhi() if rhi is None else rhi
    return _load_uniform_module().pack_array_uniforms(
        packing_rhi, _base_image_view(tile), diff_source_ready=False
    )


def pack_tile_instance(tile: StripTile, layer1: int, layer2: int, layer_px: int = LAYER_PX) -> bytes:
    """One full-canvas instance for ``base_array.vert`` (production layout).

    The tile covers the whole canvas (rects and bbox are unit), so the instance
    differs per tile only in its texture-array layers and in each side's
    content scale (uploaded image size / layer size).
    """
    from engine.gpu.array_draw import ArrayInstance, pack_array_instance

    layer_px = float(max(1, int(layer_px)))
    return pack_array_instance(
        ArrayInstance(
            rect1=(0.0, 0.0, 1.0, 1.0),
            rect2=(0.0, 0.0, 1.0, 1.0),
            content_scale=(
                tile.image1.width / layer_px,
                tile.image1.height / layer_px,
                tile.image2.width / layer_px,
                tile.image2.height / layer_px,
            ),
            content_scale_diff=(1.0, 1.0),
            layer1=int(layer1),
            layer2=int(layer2),
            layer_diff=0,
            bbox=(0.0, 0.0, 1.0, 1.0),
        )
    )


def wants_divider(tile: StripTile) -> bool:
    """Whether this tile's plan asks for a divider line at all.

    Reads the same two scene payload fields ``DividerPass.should_paint`` gates
    on (``show_divider`` + ``divider_thickness``).
    """
    if tile.plan is None:
        return False
    scene = getattr(tile.plan, "render_scene", None)
    payloads = getattr(scene, "feature_overrides", None)
    if not isinstance(payloads, dict):
        return False
    if not bool(payloads.get("show_divider", False)):
        return False
    try:
        return float(payloads.get("divider_thickness", 0) or 0) > 0.0
    except (TypeError, ValueError):
        return False


def divider_spec_for_tile(tile: StripTile):
    """``(show, position_px, thickness_px, is_horizontal, qcolor, clip)``.

    Mirror of the divider feature's stateless spec resolver
    (``tabs/image_compare/canvas/features/divider/overlay_model.py``) — shared
    code may not import a canvas feature (tests/contracts/
    test_canvas_features_imports.py); only the arithmetic is mirrored and the
    byte-exact bench comparison keeps the mirror honest.
    """
    scene = getattr(tile.plan, "render_scene", None)
    payloads = getattr(scene, "feature_overrides", None)
    payloads = payloads if isinstance(payloads, dict) else {}
    show = bool(payloads.get("show_divider", False))
    thickness = float(payloads.get("divider_thickness", 0) or 0)
    color = payloads.get("divider_color")
    is_horizontal = bool(scene_field(scene, "is_horizontal", False))
    split = float(scene_field(scene, "split_position_visual", 0.5))
    split = max(0.0, min(1.0, split))
    _letterbox, _canvas_letterbox, content_rect = tile_letterbox_geometry(tile)
    clip_x, clip_y, clip_w, clip_h = (float(v) for v in content_rect)
    # Identity camera (zoom 1, pan 0) for an export tile: the content rect is
    # already render-target px, so the pass's view-transform step is a no-op.
    position = clip_y + clip_h * split if is_horizontal else clip_x + clip_w * split
    return (
        show,
        position,
        thickness,
        is_horizontal,
        color,
        (clip_x, clip_y, clip_w, clip_h),
    )


def pack_tile_divider(tile: StripTile, clip_matrix) -> bytes:
    """One tile's divider block, in the production 128 B layout.

    The block is packed for a render target the size of ONE TILE: the tile
    viewport IS the reference tile's full-target viewport, and the divider
    shader works in target-relative px. ``b""`` when this frame shows no
    divider.
    """
    from PySide6.QtGui import QColor

    from engine.overlay import RectItem, Rgba, pack_divider

    show, position, thickness, is_horizontal, color, clip = divider_spec_for_tile(tile)
    if not show or thickness <= 0.0:
        return b""
    if isinstance(color, QColor):
        fill = Rgba(color.redF(), color.greenF(), color.blueF(), color.alphaF())
    else:
        # The feature's own default when the scene payload carries no colour
        # (white, opaque). Plain engine values, no QColor literal
        # (tests/contracts/test_no_hardcoded_qcolor.py).
        fill = Rgba(1.0, 1.0, 1.0, 1.0)
    half = thickness * 0.5
    if is_horizontal:
        bar = RectItem(
            x=clip[0], y=position - half, w=clip[2], h=thickness, color=fill
        )
    else:
        bar = RectItem(
            x=position - half, y=clip[1], w=thickness, h=clip[3], color=fill
        )
    clip_item = RectItem(
        x=clip[0],
        y=clip[1],
        w=clip[2],
        h=clip[3],
        color=Rgba(0.0, 0.0, 0.0, 0.0),
    )
    blocks = pack_divider(
        (bar, clip_item), clip_matrix, float(tile.tile_w), float(tile.tile_h)
    )
    return blocks[0] if blocks else b""


# ---------------------------------------------------------------------------
# One-pass render (GPU half lives in strip_pass, imported lazily)
# ---------------------------------------------------------------------------


def render_window(
    target,
    window: StripWindow,
    *,
    rhi,
    tile_service=None,
    command_buffer=None,
    never_shown: bool = False,
) -> None:
    """Draw ``window`` into ``target``: one pass, N viewports, no readback.

    A superseded/cancelled window raises :class:`StripWindowCancelled` before
    any GPU work happens — the in-flight result is never applied.
    """
    if not is_current(window):
        raise StripWindowCancelled(
            f"window generation {getattr(window, 'generation', '?')} is "
            f"{window.cancel_reason or 'superseded'}; not applied"
        )
    from tabs.image_compare.services.video_snapshot_rendering.strip_pass import (
        render_strip_window,
    )

    render_strip_window(
        target,
        window,
        rhi=rhi,
        tile_service=tile_service,
        command_buffer=command_buffer,
        never_shown=never_shown,
    )
    window.applied = True


__all__ = [
    "DEFAULT_TILE",
    "LAYER_PX",
    "PLACEHOLDER_RGBA",
    "StripBlockLayout",
    "StripTile",
    "StripWindow",
    "StripWindowCancelled",
    "StripWindowError",
    "build_window",
    "cancel",
    "cols_for_count",
    "current_generation",
    "divider_spec_for_tile",
    "grid_for",
    "is_current",
    "live_window",
    "next_generation",
    "pack_tile_divider",
    "pack_tile_instance",
    "pack_tile_uniforms",
    "render_window",
    "resolve_layout",
    "tile_rect",
    "uniform_block_size",
    "wants_divider",
]
