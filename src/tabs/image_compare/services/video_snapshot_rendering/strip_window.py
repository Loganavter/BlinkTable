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
  instance. Importable and testable offscreen (no QRhi).
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
    tile_service: object | None = None
    layer_count: int = 0
    layer_px: int = LAYER_PX
    build_ms: float = 0.0
    cancelled: bool = False
    cancel_reason: str = ""
    applied: bool = False

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
        """The allocator key for one tile side (one home, used by the upload)."""
        return ("strip-window", int(self.generation), int(tile.block), int(side))

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
    )
    planner = plan_builder if plan_builder is not None else _default_plan_builder()
    tiles: list[StripTile] = []
    for index in range(count):
        block = first_block + index
        timestamp = float(resolved.block_time(block))
        rect = tile_rect(index, cols, tile_w, tile_h)
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
        tiles.append(
            StripTile(
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
        )

    service = _assign_layers(tiles, window, tile_service)
    window.tiles = tuple(tiles)
    window.tile_service = service
    window.layer_count = _layer_count_for(tiles)
    for tile in tiles:
        tile.layer1, tile.layer2 = _layers_for(tile, service, window)
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
    return max(2, 2 * len(tiles))


def _assign_layers(tiles, window: StripWindow, tile_service):
    """Allocate each tile side's array layer through ``TileTextureService``.

    Same production path as the bench: register a 1x1 grid per side, mark the
    ``(0, 0)`` tile resident, read the slot back through ``slot_for`` — the
    allocator owns capacity and free-list reuse, this module assigns no layer
    numbering of its own.
    """
    from engine.cache.tile_texture_service import TileTextureService

    service = tile_service
    if service is None:
        service = TileTextureService(max_array_size=_layer_count_for(tiles))
    for tile in tiles:
        for side, image in _tile_sides(tile):
            service.register_source(
                window.tile_source_key(tile, side),
                (int(image.width), int(image.height)),
            )
    for tile in tiles:
        for side, image in _tile_sides(tile):
            service.mark_resident(
                window.tile_source_key(tile, side),
                (0, 0),
                int(image.width) * int(image.height) * 4,
                (int(image.width), int(image.height)),
            )
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
                f"({window.layer_count} layers) — give the TileTextureService "
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
