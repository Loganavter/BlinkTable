"""Phase-0 bench: can the video strip's N frame plans render in ONE GPU pass?

The proposed strip redesign drops per-frame caches and per-tile offscreen
renders: the visible strip is drawn by one pass with N frame plans, one
viewport per tile. This devtool is the falsification instrument for that
assumption. It is a *bench*, never imported by app code.

What it measures (per N, default 17 / 32 / 64, tile 160x90):

* ``plan_ms``     - building the N plans through the REAL plan builder
                    (``SnapshotFrameRenderer.prepare_canvas_frame_from_images``)
                    from two generated 3000x2000 sources.
* reference       - the CURRENT per-tile route: each plan rendered on its own
                    offscreen canvas surface (``GpuExportService.render_plan``,
                    the same GPU step ``render_snapshot_thumbnail_to_pil`` /
                    ``_render_thumbnail_from_proxy`` ends in) and hashed.
* ``pass_ms``     - one target, one pass: N viewports + per-tile uniforms and
                    per-tile texture-array layers, recorded in a single
                    beginPass/endPass by ``_StripAtlasWidget``.
* ``readback_ms`` - ONE grab of the atlas target plus the PIL conversion.

Exactness: every atlas tile is sliced out of the single readback and hashed
against its per-tile reference; ``exact_matches/N`` is the column that
decides the assumption.

Exit codes: ``0`` every tile matches and N=17 fits the 20 ms budget,
``1`` mismatch or over budget, ``2`` RHI cannot start (no display / no GL).

Why exit 2 offscreen: a ``QRhiWidget`` under ``QT_QPA_PLATFORM=offscreen``
reports ``renderFailed`` and a null framebuffer grab, so a "pass" there would
be a fabricated result. ``--cpu-only`` exists for exactly that case.

Flags: ``--self-test`` (pure functions, no GPU), ``--cpu-only`` (planning +
uniform packing only), ``--frames 17,32,64``, ``--tiles WxH``,
``--reference-samples K`` (hash-reference cap for a fast first look; the
default hashes every tile), ``--varied-sources`` (worst-case per-frame pixel
shift; informational — see below), ``--renderer production`` (drive the app's
``strip_window.build_window`` + ``render_window`` instead of this bench's own
atlas widget; the local path stays the reference implementation).

``--band`` drives the EDITOR's band wiring on top of that production seam —
the instrument for "the band renders the wrong pixels" reports. It builds the
window the way ``StripBandHost`` does (``strip_recording.strip_tile_size`` for
the tile, ``strip_band.render_request`` for the request, ``proxy_pair_for``
over an exporter seam that resolves a pyramid LEVEL exactly as
``_resolve_thumbnail_images`` does, ``StripRecordingAdapter`` for the
recording), renders it into a target of the band's own device-pixel size
(``--band-target``, ``--band-cols`` for the timeline's one-row grid), slices
each cell with the window's rects and prints a stage table: target vs grid,
per-tile tile/canvas/image/letterbox/content rect, the resolved proxy size,
and the pixels. A stage that disagrees with the filmstrip contract (one band
tile IS one filmstrip thumbnail — the 160x90 target ``is_thumbnail_target_size``
keys the cover plan on — and it must FILL its cell) fails the run even when
the batch itself is byte-exact, so "the pass is right and the plan is wrong"
cannot pass as green.

The 2026-10-30 phase-2 report ("each cell shows a narrow sliver of the frame
with black beside it") was that stage, twice: a cell-sized tile
(``--tiles 128x72``) planned every frame through the fit-inside path (measured
106 of 128 px of content), and then a 160x90 thumbnail whose cover still
covered the already-fitted content box, so a non-16:9 source kept its bars
(measured 2026-11-03: a portrait source left 40 of 160 px, i.e. 32 of a 128 px
cell). ``strip_tile_size`` is the first fix (the tile is the thumbnail the
QPainter strip rendered); the full-bleed content box in ``prepare_miss`` is
the second (the thumbnail covers the tile canvas, exactly like the CPU
compositor's ``_cover_pil(source, tile_size)``).

The live band target is OFF-GRID (a 1280x90 grid drawn into the band's own
1020x72 device size), so the off-grid row compares every cell with its own
per-tile reference RESAMPLED to the cell size: a fractional resample is never
byte-identical, so the verdict is "same content, same footprint" inside the
printed tolerance (a geometry break is 10-100x it), and the stage table still
demands a full-bleed tile.

The ``--renderer production`` flag is the phase-1 integration seam: it plans a
window through the production recording contract (``evaluate_at`` +
``images_for``), renders it through the production one-pass renderer and
compares the same slices against the same per-tile reference.

Scope of one tile: the base-image array draw AND the divider overlay, both in
that tile's own viewport/scissor inside the single pass. Layers come from the
production allocator (``TileTextureService.mark_resident``/``slot_for``) and
the uploads go through ``ArrayResources.upload_tile_to_array`` — the bench
assigns no layer numbering of its own.

Measured status of the batch (llvmpipe via a private Xvfb, 2026-10-30 — the
cell that wrote this tool has no real GPU):

* the pass is cheap: 17 tiles 2.5 ms, 32 tiles 4.0 ms, 64 tiles 8.8 ms (one
  beginPass/endPass, N viewports + N divider draws, one readback outside the
  pass: 2.3-12.8 ms depending on N);
* **the batch is byte-exact at every N measured**: 2/2, 8/8, 17/17, 32/32,
  64/64 tiles identical to the per-tile reference, divider band included
  (14040/14400 px was phase 0's number, before the divider was in scope);
* the phase-0 "array layers >= 2 do not land" verdict was a **row-order bug
  in this bench, not a layer bug**: a never-shown widget's framebuffer is
  bottom-up, so a top-down tile rect drawn through ``QRhiViewport``/
  ``QRhiScissor`` lands in the mirrored row. A one-row atlas (N <= 2) is
  unaffected, which is why N=2 looked perfect; a taller atlas reads back as
  "tile 0 shows another frame" and blank cells appear where the mirrored,
  incomplete last row lands (a 5x4 atlas has 3 dead cells, i.e. exactly the
  "3 transparent tiles at N=17" phase 0 reported). The fix is the engine's
  own COMPOSITED_SCISSOR rule (``composited_scissor_needs_y_flip``), applied
  to the viewport AND the scissor;
* the layer path itself was never the problem: a per-layer readback probe
  showed every allocated layer holding exactly its own frame's pixels, so
  the production allocator + upload path was adopted for fidelity, not as a
  fix;
* ``--varied-sources`` (each frame gets its own shifted proxy) is a planning/
  packing worst case: its GPU exactness column is informational, because the
  reference re-resolves the plan's own proxy for those frames — a scope
  difference in the harness, not a batching defect.

Temp files live under a ``tempfile.mkdtemp()`` removed at exit; the sources
are generated deterministically (no seed needed, no network).
"""

# Audit-Meta: pattern=instrument reason="phase-0 bench: CPU planning + one-pass atlas + exactness harness + self-test are one measurement, and the band-host seam (--band) reuses the same planning/packing/slicing so its stage verdict cannot drift from the atlas verdict; splitting them would let the halves disagree"
from __future__ import annotations

import argparse
import hashlib
import math
import os
import shutil
import struct
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parent.parent
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

DEFAULT_FRAMES = (17, 32, 64)
DEFAULT_TILE = (160, 90)
SOURCE_SIZE = (3000, 2000)
# One texture-array layer holds one tile's side image 1:1 (production uploads
# unresampled); 256 comfortably covers the bench's 160x90 tiles.
LAYER_PX = 256
# QRhi requires dynamic uniform-buffer bindings to be 256-byte aligned.
UNIFORM_STRIDE = 256
FPS = 10.0
BUDGET_MS_17 = 20.0


# ---------------------------------------------------------------------------
# Pure geometry / packing helpers (the --self-test surface)
# ---------------------------------------------------------------------------


def plan_tile_grid(count: int, tile_w: int, tile_h: int, cols: int | None = None):
    """Atlas grid for ``count`` tiles: ``(cols, rows, width, height)``.

    Column-major fill is irrelevant here (tiles are independent); rows are
    whatever is needed to hold every tile at the given column count.
    """
    count = max(1, int(count))
    tile_w, tile_h = max(1, int(tile_w)), max(1, int(tile_h))
    if cols is None:
        cols = 1
        while cols * cols < count:
            cols += 1
    cols = max(1, min(int(cols), count))
    rows = -(-count // cols)
    return cols, rows, cols * tile_w, rows * tile_h


def tile_rects(count: int, tile_w: int, tile_h: int, cols: int) -> list[tuple[int, int, int, int]]:
    """``(x, y, w, h)`` for every tile, in the atlas target.

    Every rect is exactly ``tile_w x tile_h`` and the rects tile the
    ``cols x rows`` area without overlap. The last row may be padded with
    unused cells (never drawn into), which is why the coverage assertion in
    the self-test compares the *drawn* area, not the whole target.
    """
    tile_w, tile_h = max(1, int(tile_w)), max(1, int(tile_h))
    rects = []
    for index in range(max(0, int(count))):
        row, col = divmod(index, max(1, int(cols)))
        rects.append((col * tile_w, row * tile_h, tile_w, tile_h))
    return rects


def target_units(rects: list[tuple[int, int, int, int]], grabbed_size: tuple[int, int]):
    """Map logical atlas rects onto the grabbed (device) framebuffer size.

    An offscreen ``QRhiWidget`` can have a DPR != 1, in which case the grab
    is larger than the logical target; every rect is scaled here so the
    slicing never silently reads the wrong grid.
    """
    if not rects:
        return rects, (1.0, 1.0)
    logical_w = max(rect[0] + rect[2] for rect in rects)
    logical_h = max(rect[1] + rect[3] for rect in rects)
    grab_w, grab_h = max(1, int(grabbed_size[0])), max(1, int(grabbed_size[1]))
    sx = grab_w / float(logical_w)
    sy = grab_h / float(logical_h)
    scaled = [
        (
            int(round(x * sx)),
            int(round(y * sy)),
            int(round(w * sx)),
            int(round(h * sy)),
        )
        for (x, y, w, h) in rects
    ]
    return scaled, (sx, sy)


def slice_tile_bytes(rgba: bytes, size: tuple[int, int], rect: tuple[int, int, int, int]) -> bytes:
    """Copy one rect out of a packed RGBA8888 buffer (``size`` = full buffer)."""
    width, height = int(size[0]), int(size[1])
    x, y, w, h = (int(v) for v in rect)
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > width or y + h > height:
        raise ValueError(f"tile rect {rect} outside {width}x{height} buffer")
    stride = width * 4
    row_bytes = w * 4
    out = bytearray(row_bytes * h)
    for row in range(h):
        start = (y + row) * stride + x * 4
        out[row * row_bytes : (row + 1) * row_bytes] = rgba[start : start + row_bytes]
    return bytes(out)


def hash_rgba(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def diff_stats(reference: bytes, candidate: bytes, size: tuple[int, int]):
    """``(differing_pixels, first_xy)`` between two same-size RGBA buffers.

    A hash says "not identical"; this says "by how much and where" — the
    difference between an overlay line the batch does not draw and a real
    per-tile content/geometry break.
    """
    if len(reference) != len(candidate):
        raise ValueError("diff_stats needs equal-length buffers")
    differing = 0
    first = None
    width = max(1, int(size[0]))
    for offset in range(0, len(reference), 4):
        if reference[offset : offset + 4] != candidate[offset : offset + 4]:
            differing += 1
            if first is None:
                pixel = offset // 4
                first = (pixel % width, pixel // width)
    return differing, first


def pad_uniform_block(block: bytes, stride: int = UNIFORM_STRIDE) -> bytes:
    """Pad one packed uniform block to the dynamic-buffer stride."""
    if len(block) > stride:
        raise ValueError(f"uniform block {len(block)}B exceeds stride {stride}B")
    return bytes(block) + b"\x00" * (stride - len(block))


def uniform_offset(index: int, stride: int = UNIFORM_STRIDE) -> int:
    offset = int(index) * int(stride)
    if offset % 256:
        raise ValueError(f"uniform offset {offset} is not 256-byte aligned")
    return offset


# ---------------------------------------------------------------------------
# Shared tile payload
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TileFrame:
    """Everything the batch pass needs for one strip tile."""

    index: int
    plan: object
    image1: object
    image2: object
    canvas_w: int
    canvas_h: int
    tile_w: int
    tile_h: int
    uniforms: bytes = b""
    instance: bytes = b""
    #: Texture-array layers this tile's two sides were ALLOCATED (never
    #: computed from the tile index: they come from the production slot
    #: allocator, see ``assign_tile_layers``).
    layer1: int = -1
    layer2: int = -1
    #: The divider overlay block for this tile, packed during the pass —
    #: ``b""`` when this frame draws no divider.
    divider: bytes = b""


def _load_uniform_module():
    from tabs.image_compare.canvas.rhi_renderer import uniforms as uniform_module

    return uniform_module


class _FixedClipRhi:
    """Stand-in ``rhi`` for CPU-side uniform packing.

    ``pack_array_uniforms`` reads exactly one thing off the RHI -- the
    backend's clip-space correction matrix -- so CPU-only packing can use the
    identity and stay on the production packer instead of re-implementing the
    std140 layout. The GPU path passes the real ``QRhi``.
    """

    def __init__(self) -> None:
        from PySide6.QtGui import QMatrix4x4

        self._matrix = QMatrix4x4()

    def clipSpaceCorrMatrix(self):  # noqa: N802 - QRhi API name
        return self._matrix


def scene_field(scene, name: str, default=None):
    """Read one ``RenderScene`` field, tolerating a dict stand-in."""
    value = getattr(scene, name, None)
    if value is None and isinstance(scene, dict):
        value = scene.get(name)
    return default if value is None else value


def tile_letterbox_geometry(
    tile: TileFrame,
) -> tuple[
    tuple[float, float, float, float],
    tuple[float, float, float, float],
    tuple[int, int, int, int],
]:
    """``(letterbox, canvas_letterbox, content_rect_px)`` for one tile.

    Mirrors the export canvas path for an unpadded plan
    (``apply_plan_letterbox_from_clip`` in
    ``src/engine/cache/display_tier_service.py``): the plan's padded canvas
    frame is fitted into the render target (here: the tile, whose size equals
    the plan's canvas), then ``overlay_clip_rect`` (canvas px) is nested
    inside that frame. No clip rect / no geometry letterbox / a padded
    composite plan all leave the pair at full-target UVs.
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


def _base_image_view(tile: TileFrame):
    """The production ``BaseImagePrimitive`` fields the array packer reads.

    Export/video plans render the whole canvas: zoom 1, pan 0. The split is
    the scene's ``split_position_visual`` — the field the export canvas feeds
    both ``base.frag`` and ``DividerPass`` (``split_position`` does not exist
    on ``RenderScene``, and defaulting to 0.5 there silently draws every tile
    with half the frame showing the wrong side).
    """
    scene = getattr(tile.plan, "render_scene", None)
    split = float(scene_field(scene, "split_position_visual", 0.5))
    split = max(0.0, min(1.0, split))
    letterbox, canvas_letterbox, _rect = tile_letterbox_geometry(tile)
    fill = getattr(tile.plan, "fill_rgba", None)
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


@dataclass(slots=True)
class _BaseImageView:
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


def pack_tile_uniforms(rhi, tile: TileFrame) -> bytes:
    """Production uniform packing for one tile (``_ARRAY_UNIFORM_BLOCK_SIZE``)."""
    uniform_module = _load_uniform_module()
    return uniform_module.pack_array_uniforms(
        rhi, _base_image_view(tile), diff_source_ready=False
    )


def pack_tile_instance(tile: TileFrame, layer1: int, layer2: int) -> bytes:
    """One full-canvas instance for ``base_array.vert`` (production layout).

    The tile covers the whole canvas (rects and bbox are unit), so the
    instance differs per tile only in its texture-array layers and in each
    side's content scale (uploaded image size / layer size).
    """
    from engine.gpu.array_draw import ArrayInstance, pack_array_instance

    return pack_array_instance(
        ArrayInstance(
            rect1=(0.0, 0.0, 1.0, 1.0),
            rect2=(0.0, 0.0, 1.0, 1.0),
            content_scale=(
                tile.image1.width / float(LAYER_PX),
                tile.image1.height / float(LAYER_PX),
                tile.image2.width / float(LAYER_PX),
                tile.image2.height / float(LAYER_PX),
            ),
            content_scale_diff=(1.0, 1.0),
            layer1=int(layer1),
            layer2=int(layer2),
            layer_diff=0,
            bbox=(0.0, 0.0, 1.0, 1.0),
        )
    )


def divider_spec_for_tile(tile: TileFrame):
    """``(show, position_px, thickness_px, is_horizontal, qcolor, clip)``.

    Bench mirror of the divider feature's stateless spec resolver
    (``tabs/image_compare/canvas/features/divider/overlay_model.py`` —
    ``resolve_divider_spec`` / ``content_split_visual``), which shared code is
    not allowed to import (tests/contracts/test_canvas_features_imports.py).
    Only the arithmetic is mirrored: the inputs are the same ones the export
    canvas reads (the plan scene's ``feature_overrides`` payload and split, the
    letterbox content rect after the identity camera), and the packing below
    uses the production ``pack_divider``. The byte-exact comparison against the
    per-tile reference is what keeps this mirror honest — drift here shows up
    as a mismatch, not as a silent pass.
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


def _plan_wants_divider(tile: TileFrame) -> bool:
    """Whether this tile's plan asks for a divider line at all.

    Reads the same two scene payload fields ``DividerPass.should_paint``
    gates on (``show_divider`` + ``divider_thickness``); the geometry is still
    resolved per tile by ``pack_tile_divider``.
    """
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


def pack_tile_divider(tile: TileFrame, clip_matrix) -> bytes:
    """One tile's divider block, in the production 128 B layout.

    ``DividerPass`` packs ``(bar, clip)`` rect items through
    ``pack_divider``; the rect assembly is mirrored from its semantic
    resolver (see ``divider_spec_for_tile`` for why it is mirrored), and the
    block is packed for a render target the size of ONE TILE: the reference
    tile is its own 160x90 surface, and the divider shader works in
    target-relative px (``vTexCoord * resolution``). ``b""`` when this frame
    shows no divider.
    """
    from PySide6.QtGui import QColor

    from engine.overlay import RectItem, Rgba, pack_divider

    show, position, thickness, is_horizontal, color, clip = divider_spec_for_tile(tile)
    if not show or thickness <= 0.0:
        return b""
    if isinstance(color, QColor):
        fill = Rgba(color.redF(), color.greenF(), color.blueF(), color.alphaF())
    else:
        # The feature's own default when the scene payload carries no color
        # (white, opaque). Plain engine colour values, no QColor literal:
        # palette colours belong to the theme, and a devtool has no business
        # inventing one (tests/contracts/test_no_hardcoded_qcolor.py).
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
# Production texture-array layer assignment
# ---------------------------------------------------------------------------


def tile_source_key(tile: TileFrame, side: int) -> tuple:
    """The ``TileTextureService`` source id for one tile's one side.

    Production keys its slot allocator by whatever the caller uses as a
    source identity (``"stored_0"``/``LevelKey``/...); the bench's tile
    images are independent one-tile sources, so the identity is
    ``(run, tile, side)``. ``register_source`` gives each one a 1x1 grid,
    which is what makes ``tile_key`` collapse to the source id itself.
    """
    return ("strip-tile", int(tile.index), int(side))


def _tile_sides(tile: TileFrame):
    return ((1, tile.image1), (2, tile.image2))


def assign_tile_layers(tiles: list[TileFrame]):
    """Allocate every tile side's array layer through ``TileTextureService``.

    Returns ``(service, layer_count)``. This is the production assignment
    path — grid registration plus ``mark_resident`` for the ``(source, (0,0))``
    pair, read back via ``slot_for`` — never ``index * 2``. The allocator is
    the thing phase 0 needs to exercise: it owns capacity, free-list reuse
    and the down-sized capacity the GPU owner publishes.

    Deviation from the canvas, stated once: production calls
    ``mark_resident`` from *inside* ``ArrayResources.upload_tile_to_array``
    (``src/engine/gpu/array_resources.py:216``), i.e. after the upload batch
    has been staged. The bench has to know each tile's layers before it packs
    the per-tile instance buffer, and its uploads all happen in one frame, so
    it registers + marks resident first and then uploads through the same
    ``upload_tile_to_array`` (which finds the already-allocated slot and
    uploads into it). Same allocator, same upload call, one-frame reorder.
    """
    from engine.cache.tile_texture_service import TileTextureService

    layer_count = max(2, 2 * len(tiles))
    service = TileTextureService(max_array_size=layer_count)
    for tile in tiles:
        for _side, image in _tile_sides(tile):
            service.register_source(
                tile_source_key(tile, _side),
                (int(image.width), int(image.height)),
            )
    for tile in tiles:
        for side, image in _tile_sides(tile):
            service.mark_resident(
                tile_source_key(tile, side),
                (0, 0),
                int(image.width) * int(image.height) * 4,
                (int(image.width), int(image.height)),
            )
    return service, layer_count


def tile_layers(tile: TileFrame, service) -> tuple[int, int]:
    """``(layer1, layer2)`` the allocator handed this tile's two sides."""
    layers = []
    for side, _image in _tile_sides(tile):
        slot = service.slot_for(tile_source_key(tile, side), (0, 0))
        if slot is None:
            raise RuntimeError(f"tile {tile.index} side {side}: no allocated slot")
        layers.append(int(slot[1]))
    return layers[0], layers[1]


# ---------------------------------------------------------------------------
# Deterministic synthetic recording
# ---------------------------------------------------------------------------


def make_sources(directory: Path) -> tuple[Path, Path]:
    """Two generated sources: a gradient field and a 37 px shifted variant."""
    from PIL import Image, ImageChops

    directory.mkdir(parents=True, exist_ok=True)
    base = Image.radial_gradient("L").resize(SOURCE_SIZE, Image.Resampling.BICUBIC)
    # Three decorrelated L channels -> one colour field; deterministic, no seed.
    channel_r = base
    channel_g = ImageChops.invert(base)
    channel_b = ImageChops.offset(base, 90, 40)
    left = Image.merge("RGB", (channel_r, channel_g, channel_b))
    # A shifted variant (not a resize): the strip's frames differ in content,
    # so a mis-sliced atlas tile cannot hide behind identical pixels.
    shifted = ImageChops.offset(left, 37, 11)
    path1 = directory / "source_a.png"
    path2 = directory / "source_b.png"
    left.save(path1)
    shifted.save(path2)
    return path1, path2


def _activate_image_compare_tab():
    """The plan builder reads store.viewport/settings; the registry must agree."""
    from tabs.registry import get_shared_tab_registry

    registry = get_shared_tab_registry()
    previous = registry._active_session_type
    registry._active_session_type = "image_compare"
    return registry, previous


def _build_store():
    from core.store import Store

    store = Store()
    store.create_workspace_session(session_type="image_compare", activate=True)
    _disable_overlays(store)
    return store


# Overlay features the bench switches off so the reference renders the same
# scope the batch draws: the base-image pass plus the divider. The divider is
# deliberately NOT in this list -- it is part of the per-tile scope the batch
# now reproduces (``pack_tile_divider``), so disabling it in the reference
# would compare a scope the strip never renders. Guides/magnifier/capture/
# filename overlays stay off: they are interactive-ish chrome the strip does
# not batch, and leaving them on would make every tile differ for a reason
# this bench does not measure.
_OVERLAY_COMMANDS = (
    ("guides", ("toggle_enabled",)),
    ("magnifier", ("toggle_enabled",)),
    ("capture", ("settings.toggle_visibility", "toggle_visibility")),
    ("filename_overlay", ("settings.toggle_visibility", "toggle_visibility")),
)


def _disable_overlays(store) -> list[str]:
    """Turn off every canvas overlay feature; returns the ones still on.

    Two paths on purpose: ``execute_feature_command`` covers the features
    that expose a state command (guides/magnifier), and the settings mutation
    covers the rest — their visibility lives in the feature setting
    ``<feature>.visible`` and is what the render-scene overrides read.
    """
    from ui.canvas_infra.scene.feature_state_api import execute_feature_command

    remaining = []
    for feature, commands in _OVERLAY_COMMANDS:
        disabled = False
        for command in commands:
            try:
                execute_feature_command(store, feature, command, False)
                disabled = True
                break
            except Exception:
                continue
        if not disabled:
            try:
                store.settings.mutations.set_canvas_feature_setting(
                    f"{feature}.visible",
                    False,
                    invalidate_render_cache=True,
                    request_core_update=True,
                )
                disabled = True
            except Exception:
                disabled = False
        if not disabled:
            remaining.append(feature)
    return remaining


def _build_snapshot(store, timestamp: float, image1_path: str, image2_path: str):
    from tabs.image_compare.plugins.video_editor.services.keyframing.types import (
        FrameSnapshot,
    )

    # The recorder freezes each frame's state (``freeze_for_export``): every
    # frame owns a DISTINCT viewport/settings clone. Passing the live store
    # would alias all frames to one state and make a batched strip look
    # cheaper than it is. The split is nudged per frame so the plans really
    # differ (a recording's viewport changes over time).
    viewport = store.viewport.freeze_for_export()
    try:
        viewport.view_state.split_position_visual = 0.30 + 0.02 * (
            int(round(float(timestamp) * 60.0)) % 20
        )
    except Exception:
        pass
    return FrameSnapshot(
        timestamp=float(timestamp),
        viewport_state=viewport,
        settings_state=store.settings.freeze_for_export(),
        image1_path=image1_path,
        image2_path=image2_path,
        name1=Path(image1_path).name,
        name2=Path(image2_path).name,
    )


def _build_request(tile_w: int, tile_h: int):
    from engine.cache import TargetSurfaceSpec
    from tabs.image_compare.plugins.video_editor.services.video_export_models import (
        VideoRenderRequest,
    )

    return VideoRenderRequest(
        target_surface=TargetSurfaceSpec(
            width=int(tile_w), height=int(tile_h), fill_rgba=(0, 0, 0, 0)
        ),
        font_path=None,
        auto_crop=False,
        fit_content=False,
        global_bounds=None,
    )


def _pil_from_source(source):
    """Plan image -> PIL, whether it is a PIL image or a ``TiledPixelStore``.

    Export/video plans carry stores (the decoded pair); the live path carries
    PIL. The batched pass uploads exactly what the plan carries.
    """
    if hasattr(source, "read_array") and hasattr(source, "size"):
        from PIL import Image

        width, height = source.size
        return Image.fromarray(source.read_array((0, 0, width, height)), "RGBA")
    return source


def build_tiles(
    count: int,
    tile_w: int,
    tile_h: int,
    image1_path: Path,
    image2_path: Path,
    *,
    verbose: bool = True,
    varied_sources: bool = False,
) -> tuple[list[TileFrame], float]:
    """Build ``count`` real frame plans; returns ``(tiles, plan_ms)``.

    Every frame is planned through the production builder with the two source
    images, at a timestamp that differs per frame (the strip's N visible
    frames span the recording).
    """
    from PIL import Image

    from tabs.image_compare.services.video_snapshot_rendering.renderer import (
        SnapshotFrameRenderer,
    )

    store = _build_store()
    request = _build_request(tile_w, tile_h)
    # The strip never feeds full-res pixels to a tile: `_resolve_thumbnail_images`
    # hands the plan builder a pyramid/display *proxy* at the requested size.
    # Modelling that here keeps the plan's images uploadable into one array
    # layer, exactly as the batched pass will need them. Each frame gets its
    # own shifted proxy so a tile cannot pass by reading a neighbour's layer.
    from PIL import ImageChops

    proxy1 = Image.open(image1_path).convert("RGBA").resize(
        (tile_w, tile_h), Image.Resampling.BICUBIC
    )
    proxy2 = Image.open(image2_path).convert("RGBA").resize(
        (tile_w, tile_h), Image.Resampling.BICUBIC
    )
    renderer = SnapshotFrameRenderer(image_loader=lambda path, auto_crop=False: None)

    started = time.perf_counter()
    tiles: list[TileFrame] = []
    for index in range(count):
        snap = _build_snapshot(
            store, index / FPS, str(image1_path), str(image2_path)
        )
        # The real strip resolves the SAME source pair for every frame (the
        # session's pyramids); only the frame state differs. ``varied_sources``
        # keeps the old per-frame pixel shift as an explicit worst case
        # (different sources per tile), never as the default.
        if varied_sources:
            frame1 = ImageChops.offset(proxy1, (index * 7) % tile_w, (index * 3) % tile_h)
            frame2 = ImageChops.offset(proxy2, (index * 5) % tile_w, (index * 11) % tile_h)
        else:
            frame1, frame2 = proxy1, proxy2
        prepared = renderer.prepare_canvas_frame_from_images(
            snap, request, frame1, frame2
        )
        plan = prepared.plan
        # Upload the PLAN's own pixels (the builder prescales the passed pair,
        # so the raw proxy would differ from the reference by resample
        # rounding everywhere — a bench artefact, not a batching defect).
        plan_image1 = _pil_from_source(getattr(plan, "image1", frame1))
        plan_image2 = _pil_from_source(getattr(plan, "image2", frame2))
        tiles.append(
            TileFrame(
                index=index,
                plan=plan,
                image1=plan_image1,
                image2=plan_image2,
                canvas_w=int(getattr(plan, "canvas_w", 0) or tile_w),
                canvas_h=int(getattr(plan, "canvas_h", 0) or tile_h),
                tile_w=int(tile_w),
                tile_h=int(tile_h),
            )
        )
    plan_ms = (time.perf_counter() - started) * 1000.0
    if verbose:
        first = tiles[0]
        print(
            f"  built {count} plans in {plan_ms:.1f} ms "
            f"(canvas {first.canvas_w}x{first.canvas_h}, "
            f"images {first.image1.size}/{first.image2.size})"
        )
    return tiles, plan_ms


def pack_tiles(tiles: list[TileFrame], *, rhi=None):
    """Allocate layers + pack per-tile uniforms/instances.

    Returns ``(pack_ms, tile_service)`` — the service is the same one the GPU
    uploads must go through, so layer assignment and upload use one allocator.
    """
    packing_rhi = rhi if rhi is not None else _FixedClipRhi()
    tile_service, _layer_count = assign_tile_layers(tiles)
    started = time.perf_counter()
    for index, tile in enumerate(tiles):
        tile.layer1, tile.layer2 = tile_layers(tile, tile_service)
        tile.uniforms = pack_tile_uniforms(packing_rhi, tile)
        tile.instance = pack_tile_instance(tile, tile.layer1, tile.layer2)
        if len(tile.uniforms) != _uniform_block_size():
            raise RuntimeError(
                f"tile {index}: uniform block {len(tile.uniforms)}B, "
                f"shader expects {_uniform_block_size()}B"
            )
        uniform_offset(index)  # asserts 256-byte alignment
    return (time.perf_counter() - started) * 1000.0, tile_service


def _uniform_block_size() -> int:
    from tabs.image_compare.canvas.rhi_renderer.uniforms import _ARRAY_UNIFORM_BLOCK_SIZE

    return int(_ARRAY_UNIFORM_BLOCK_SIZE)


# ---------------------------------------------------------------------------
# GPU: reference per-tile route
# ---------------------------------------------------------------------------


def _qt_app():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _must_have_rhi(widget, what: str) -> None:
    if widget.rhi() is None:
        raise _NoDisplayError(
            f"{what}: QRhi is not available in this process (no window/GPU "
            "surface). A batched-pass result cannot be measured offscreen."
        )


class _NoDisplayError(RuntimeError):
    """RHI cannot start here -- the caller exits 2 instead of faking a pass."""


class _PreconditionError(RuntimeError):
    """The bench cannot compare 1:1 (e.g. a device-pixel ratio != 1).

    A silent resample here would turn a resolution difference into a
    "mismatch" verdict, so the run stops instead.
    """


def preflight_rhi() -> None:
    """Prove a QRhi surface can start before planning N frames.

    A ``QRhiWidget`` under ``QT_QPA_PLATFORM=offscreen`` never gets an RHI and
    its framebuffer grab is null; failing here (exit 2) is the honest outcome,
    not a fabricated pass or a planning run whose numbers can never be used.
    """
    from PySide6.QtWidgets import QRhiWidget

    from engine.gpu.offscreen_canvas import (
        configure_offscreen_widget,
        resize_and_show_offscreen_widget,
    )

    _qt_app()
    widget = QRhiWidget()
    configure_offscreen_widget(widget)
    try:
        resize_and_show_offscreen_widget(widget, (64, 64))
        if widget.rhi() is None:
            raise _NoDisplayError(
                "QRhi did not start for a probe QRhiWidget (null rhi(); "
                f"platform={os.environ.get('QT_QPA_PLATFORM', '<unset>')}). "
                "The strip's GPU pass needs a real or Xvfb display."
            )
    finally:
        try:
            widget.close()
            widget.deleteLater()
        except Exception:
            pass


def render_reference_tiles(tiles: list[TileFrame], sample_count: int | None):
    """The CURRENT per-tile route, one offscreen surface per tile.

    ``GpuExportService.render_plan`` is the GPU step the strip's
    ``render_snapshot_thumbnail_to_pil`` / ``_render_thumbnail_from_proxy``
    ends in (same plan -> same offscreen canvas widget -> same grab).
    """
    from plugins.export.services.gpu_export import GpuExportService

    service = GpuExportService()
    limit = len(tiles) if sample_count is None else max(1, min(int(sample_count), len(tiles)))
    results = []
    started = time.perf_counter()
    try:
        for tile in tiles[:limit]:
            image, debug = service.render_plan(tile.plan, diff_image=None)
            rgba = image.convert("RGBA")
            if rgba.size != (tile.tile_w, tile.tile_h):
                rgba = rgba.resize((tile.tile_w, tile.tile_h))
            raw = rgba.tobytes("raw", "RGBA")
            results.append((hash_rgba(raw), debug, raw))
    finally:
        try:
            service.shutdown()
        except Exception:
            pass
    elapsed = (time.perf_counter() - started) * 1000.0
    return results, elapsed


# ---------------------------------------------------------------------------
# GPU: batched atlas pass
# ---------------------------------------------------------------------------


def _array_resources(rhi_getter, sampler_getter, layer_count: int, name_prefix: str):
    from tabs.image_compare.canvas.rhi_renderer.array_resources import ArrayResources

    return ArrayResources(
        rhi_getter=rhi_getter,
        sampler_getter=sampler_getter,
        load_shader=_load_ic_shader,
        layer_px=LAYER_PX,
        array_capacity=int(layer_count),
        name_prefix=name_prefix,
    )


def _load_ic_shader(name: str):
    """The tab's own compiled shader (base_array.vert/.frag) — no copy."""
    from PySide6.QtGui import QShader

    shader_dir = _SRC_ROOT / "tabs" / "image_compare" / "canvas" / "shaders"
    shader = QShader.fromSerialized((shader_dir / name).read_bytes())
    if not shader.isValid():
        raise RuntimeError(f"Invalid compiled shader: {name}")
    return shader


def _qimage_from_pil(image):
    """RGBA PIL -> ``QImage`` without a copy of the pixel buffer lifetime."""
    from PySide6.QtGui import QImage

    rgba = image.convert("RGBA")
    data = rgba.tobytes("raw", "RGBA")
    qimage = QImage(data, rgba.width, rgba.height, QImage.Format.Format_RGBA8888)
    return qimage.copy()


def _make_atlas_widget(tiles: list[TileFrame], cols: int, tile_service):
    from PySide6.QtWidgets import QRhiWidget

    from engine.gpu.offscreen_canvas import configure_offscreen_widget

    class _StripAtlasWidget(QRhiWidget):
        """ONE target, ONE pass, N viewports — the batching assumption.

        Every tile draws the production ``base_array.vert``/``.frag`` quad from its own
        texture-array layer pair (allocated by ``TileTextureService``, uploaded
        through ``ArrayResources.upload_tile_to_array``) and its own uniform
        block, then the production divider overlay for that tile in the same
        tile viewport/scissor; the pass is opened once and the target is read
        back once by the caller.
        """

        def __init__(self) -> None:
            super().__init__()
            configure_offscreen_widget(self)
            self._tiles = list(tiles)
            self._cols = int(cols)
            self._tile_service = tile_service
            self._layer_count = max(2, 2 * len(self._tiles))
            self._resources = None
            self._vertex_buffer = None
            self._uniform_buffers = []
            self._instance_buffers = []
            self._srbs: list = []
            self._pipeline_ready = False
            self._uploaded = False
            self._last_error: str | None = None
            self._divider = None
            self._divider_slots: dict[int, int] = {}
            # Whether this run draws the divider at all: decided from each
            # tile's own plan (the export scene's ``show_divider`` payload and
            # thickness), the same fields ``DividerPass.should_paint`` reads.
            # Decided up front because the pack needs a mounted shell (rhi).
            self._divider_wanted = any(
                _plan_wants_divider(tile) for tile in self._tiles
            )

        # -- lifecycle -------------------------------------------------
        def initialize(self, command_buffer):
            try:
                self._initialize(command_buffer)
            except BaseException as exc:  # Qt swallows render-override raises
                self._last_error = f"initialize failed: {exc}"
                raise

        def render(self, command_buffer):
            try:
                self._render(command_buffer)
            except BaseException as exc:
                self._last_error = f"render failed: {exc}"
                raise

        def _initialize(self, command_buffer):
            rhi = self.rhi()
            if rhi is None:
                self._last_error = "renderer initialize without an RHI"
                return
            # Qt re-runs initialize() on every target recreation (resize); all
            # ranked GPU state below is new, so the upload/pipeline flags must
            # not survive it (a stale _uploaded=True leaves the array blank).
            self._uploaded = False
            self._upload_qimages = []
            self._pipeline_ready = False
            self._srbs = []
            # The overlay shell owns GPU objects bound to the previous target;
            # re-mount on the new one and repack the divider blocks with its
            # (possibly different) clip matrix.
            if self._divider is not None:
                try:
                    self._divider.teardown()
                except Exception:
                    pass
            self._divider = None
            self._divider_slots = {}
            for tile in self._tiles:
                tile.divider = b""
            from PySide6.QtGui import QRhiBuffer, QRhiSampler

            samplers = {}
            for name, filter_mode, mip_mode in (
                ("nearest", QRhiSampler.Filter.Nearest, QRhiSampler.Filter.None_),
                ("linear", QRhiSampler.Filter.Linear, QRhiSampler.Filter.Linear),
            ):
                sampler = rhi.newSampler(
                    filter_mode,
                    filter_mode,
                    mip_mode,
                    QRhiSampler.AddressMode.ClampToEdge,
                    QRhiSampler.AddressMode.ClampToEdge,
                )
                if not sampler.create():
                    raise RuntimeError(f"bench: failed to create {name} sampler")
                samplers[name] = sampler
            self._samplers = samplers
            self._resources = _array_resources(
                lambda: self.rhi(),
                lambda name: self._samplers[name],
                self._layer_count,
                "bench-strip",
            )
            # The allocator that handed out the layers must follow a capacity
            # the driver refuses (ArrayResources halves it on a failed probe);
            # otherwise the plan keeps sampling layers this array cannot
            # address (tests/contracts/test_capacity_agreement.py).
            self._resources.set_capacity_sink(
                lambda capacity: self._tile_service.set_max_array_size(capacity)
            )
            from tabs.image_compare.canvas.rhi_renderer.resources import _VERTICES

            vertex_buffer = rhi.newBuffer(
                QRhiBuffer.Type.Immutable,
                QRhiBuffer.UsageFlag.VertexBuffer,
                len(_VERTICES),
            )
            if not vertex_buffer.create():
                raise RuntimeError("bench: failed to create quad vertex buffer")
            self._vertex_buffer = vertex_buffer
            self._pending_vertex_bytes = _VERTICES

            # One uniform + one instance buffer PER TILE. Offset-addressed
            # bindings (one big buffer, per-tile offsets) drew only tile 0 on
            # this backend; separate buffers are the shape a batched pass can
            # actually rely on.
            uniform_buffers = []
            instance_buffers = []
            for index in range(len(self._tiles)):
                uniform_buffer = rhi.newBuffer(
                    QRhiBuffer.Type.Dynamic,
                    QRhiBuffer.UsageFlag.UniformBuffer,
                    UNIFORM_STRIDE,
                )
                if not uniform_buffer.create():
                    raise RuntimeError(f"bench: failed to create uniform buffer {index}")
                uniform_buffers.append(uniform_buffer)
                instance_buffer = rhi.newBuffer(
                    QRhiBuffer.Type.Dynamic,
                    QRhiBuffer.UsageFlag.VertexBuffer,
                    max(1, len(self._tiles[index].instance))
                    or self._resources._instance_stride,
                )
                if not instance_buffer.create():
                    raise RuntimeError(f"bench: failed to create instance buffer {index}")
                instance_buffers.append(instance_buffer)
            self._uniform_buffers = uniform_buffers
            self._instance_buffers = instance_buffers
            # The SRBs bind tile_arrays[0]; create it first (the array is
            # allocated up front with every layer the run needs).
            self._resources._ensure_tile_array(0)
            self._ensure_srbs()

        def _ensure_srbs(self) -> None:
            from PySide6.QtGui import QRhiShaderResourceBinding

            rhi = self.rhi()
            stages = (
                QRhiShaderResourceBinding.StageFlag.VertexStage
                | QRhiShaderResourceBinding.StageFlag.FragmentStage
            )
            fragment = QRhiShaderResourceBinding.StageFlag.FragmentStage
            tile_array = self._resources.tile_arrays[0]
            sampler = self._samplers["linear"]
            block = _uniform_block_size()
            srbs = []
            for index in range(len(self._tiles)):
                srb = rhi.newShaderResourceBindings()
                srb.setBindings(
                    [
                        QRhiShaderResourceBinding.uniformBuffer(
                            0, stages, self._uniform_buffers[index], 0, block
                        ),
                        QRhiShaderResourceBinding.sampledTexture(
                            1, fragment, tile_array, sampler
                        ),
                    ]
                )
                if not srb.create():
                    raise RuntimeError(f"bench: failed to create SRB {index}")
                srbs.append(srb)
            self._srbs = srbs

        def releaseResources(self):
            self._srbs = []
            if self._divider is not None:
                try:
                    self._divider.teardown()
                except Exception:
                    pass
            self._divider = None
            self._divider_slots = {}
            if self._resources is not None:
                try:
                    self._resources.release()
                except Exception:
                    pass
            self._resources = None
            buffers = [
                self._vertex_buffer,
                *getattr(self, "_uniform_buffers", []),
                *getattr(self, "_instance_buffers", []),
            ]
            for buffer in buffers:
                if buffer is not None:
                    try:
                        buffer.destroy()
                    except Exception:
                        pass
            self._vertex_buffer = None
            self._uniform_buffers = []
            self._instance_buffers = []

        # -- recording -------------------------------------------------
        def _render(self, command_buffer):
            if self._resources is None or not self._uniform_buffers:
                return
            from PySide6.QtGui import (
                QRhiDepthStencilClearValue,
                QRhiScissor,
                QRhiViewport,
            )

            rhi = self.rhi()
            target = self.renderTarget()
            if rhi is None or target is None:
                return
            if not self._pipeline_ready:
                self._resources.ensure_array_pipeline(target)
                self._pipeline_ready = True
            self._resources._ensure_tile_array(0)
            if len(self._srbs) != len(self._tiles) or not self._srbs:
                self._ensure_srbs()

            updates = rhi.nextResourceUpdateBatch()
            if not self._uploaded:
                # Production upload path: every layer goes through
                # ``ArrayResources.upload_tile_to_array`` (bound guard,
                # ``mark_resident``/``slot_for`` layers, ``dirty_layers``)
                # instead of a bench-local ``index * 2`` layer literal.
                updates.uploadStaticBuffer(
                    self._vertex_buffer, self._pending_vertex_bytes
                )
                command_buffer.resourceUpdate(updates)
                updates = rhi.nextResourceUpdateBatch()
                # The upload descriptions hold their QImage by reference; a
                # collected source image leaves the GPU upload reading freed
                # pixels (observed as blank/garbage tiles once a run uploads
                # more than a handful of layers). Keep them alive until the
                # run ends.
                self._upload_qimages = []
                dirty_layers: dict[int, set[int]] = {}
                for tile in self._tiles:
                    for side, image in _tile_sides(tile):
                        qimage = _qimage_from_pil(image)
                        self._upload_qimages.append(qimage)
                        self._resources.upload_tile_to_array(
                            self._tile_service,
                            tile_source_key(tile, side),
                            (0, 0),
                            qimage,
                            updates,
                            dirty_layers,
                        )
                # Production shape: every tile's upload rides ONE resource
                # update batch, submitted before the mips are regenerated
                # (``upload_tile_to_array`` documents that ``generateMips``
                # must not be in the same batch as the upload it reads).
                command_buffer.resourceUpdate(updates)
                updates = rhi.nextResourceUpdateBatch()
                for array_index in sorted(dirty_layers):
                    # The upload call defers mip generation to its caller
                    # (production runs the engine mip cascade over the dirty
                    # layers after submitting, instead of regen-per-upload).
                    # The bench has no cascade and uploads once per run, so it
                    # regenerates the whole bound array once -- same resulting
                    # levels, coarser granularity, stated as the deviation.
                    if array_index != 0:
                        continue
                    self._resources._ensure_tile_array(array_index)
                    updates.generateMips(self._resources.tile_arrays[array_index])
                self._uploaded = True

            blocks = [(index, tile.divider) for index, tile in enumerate(self._tiles)]
            if self._divider_wanted:
                if self._divider is None:
                    from engine.overlay import DIVIDER_UNIFORM_SIZE, OverlayShell

                    shell = OverlayShell(DIVIDER_UNIFORM_SIZE)
                    shell.mount(
                        rhi,
                        target,
                        _SRC_ROOT
                        / "tabs"
                        / "image_compare"
                        / "canvas"
                        / "features"
                        / "divider"
                        / "shaders",
                        "divider",
                    )
                    self._divider = shell
                # Packed once per run: the clip matrix comes from the mounted
                # shell, and every tile's spec is static for this frame.
                blocks = []
                for index, tile in enumerate(self._tiles):
                    if not tile.divider:
                        tile.divider = pack_tile_divider(tile, self._divider.clip_matrix())
                    blocks.append((index, tile.divider))
                # Slot per tile: ``stage_packed`` grows the shell's uniform
                # buffers and stages the shared quad, one slot per block.
                present = [index for index, block in blocks if block]
                self._divider_slots = {tile: slot for slot, tile in enumerate(present)}
                self._divider.stage_packed(
                    updates, [tile.divider for tile in self._tiles if tile.divider]
                )

            for index, tile in enumerate(self._tiles):
                # One update batch per tile: a single batch carrying every
                # per-tile buffer update loses some of them on this backend
                # (the tiles whose update was dropped draw nothing at all).
                updates.updateDynamicBuffer(
                    self._uniform_buffers[index],
                    0,
                    pad_uniform_block(tile.uniforms),
                )
                updates.updateDynamicBuffer(
                    self._instance_buffers[index],
                    0,
                    tile.instance,
                )
                command_buffer.resourceUpdate(updates)
                updates = rhi.nextResourceUpdateBatch()

            command_buffer.beginPass(
                target,
                _clear_color(),
                QRhiDepthStencilClearValue(1.0, 0),
                updates,
            )
            try:
                tile_w = self._tiles[0].tile_w
                tile_h = self._tiles[0].tile_h
                logical_rects = tile_rects(len(self._tiles), tile_w, tile_h, self._cols)
                # Device pixels, derived from the render target (never from the
                # logical widget size): a viewport sized in logical px paints
                # into 1/DPR of the framebuffer per axis.
                pixel_size = target.pixelSize()
                fb_w = max(1, int(pixel_size.width()))
                fb_h = max(1, int(pixel_size.height()))
                atlas_w = max(1, self.width())
                atlas_h = max(1, self.height())
                fb_scale_x = fb_w / float(atlas_w)
                fb_scale_y = fb_h / float(atlas_h)
                # Per-tile rects in FRAMEBUFFER pixels (derived from
                # target.pixelSize() above), never from the logical widget size.
                fb_rects = [
                    (
                        int(round(logical_x * fb_scale_x)),
                        int(round(logical_y * fb_scale_y)),
                        int(round(logical_w * fb_scale_x)),
                        int(round(logical_h * fb_scale_y)),
                    )
                    for (logical_x, logical_y, logical_w, logical_h) in logical_rects
                ]
                # The framebuffer origin is bottom-left for a widget that is
                # never shown (WA_DontShowOnScreen, which
                # ``configure_offscreen_widget`` sets): Qt's compositing step
                # never absorbs the Y convention, so a top-down rect must be
                # flipped before it reaches QRhi. This is the COMPOSITED_SCISSOR
                # rule from ``engine/gpu/rt_y_orientation.py`` (the same
                # predicate production's ``scissor_from_widget_rect`` uses), and
                # it applies to the viewport exactly as it does to the scissor:
                # viewport and scissor live in one framebuffer space. Getting it
                # wrong does NOT blank the atlas -- it silently draws each row
                # into the mirrored row, so a 1-row atlas (N<=2) looks perfect
                # and every taller one reads back as another tile's frame
                # (which is what phase 0 mistook for a texture-layer failure).
                from engine.gpu.rt_y_orientation import composited_scissor_needs_y_flip

                from PySide6.QtCore import Qt

                flip_rows = composited_scissor_needs_y_flip(
                    rhi,
                    never_shown=self.testAttribute(
                        Qt.WidgetAttribute.WA_DontShowOnScreen
                    ),
                )
                if flip_rows:
                    fb_rects = [
                        (x, fb_h - (y + h), w, h) for (x, y, w, h) in fb_rects
                    ]
                command_buffer.setGraphicsPipeline(self._resources.array_pipeline)
                for index, (tile_left_fb, tile_top_fb, tile_w_fb, tile_h_fb) in enumerate(
                    fb_rects
                ):
                    command_buffer.setViewport(
                        QRhiViewport(
                            float(tile_left_fb),
                            float(tile_top_fb),
                            float(tile_w_fb),
                            float(tile_h_fb),
                        )
                    )
                    # Explicit per-tile scissor: a batched pass may not assume
                    # the target's default scissor covers more than the surface
                    # Qt painted last (observed: every tile past the top-left
                    # 320x180 quadrant came back clear with viewport alone).
                    command_buffer.setScissor(
                        QRhiScissor(
                            int(tile_left_fb),
                            int(tile_top_fb),
                            int(tile_w_fb),
                            int(tile_h_fb),
                        )
                    )
                    command_buffer.setVertexInput(
                        0,
                        [
                            (self._vertex_buffer, 0),
                            (self._instance_buffers[index], 0),
                        ],
                    )
                    command_buffer.setShaderResources(self._srbs[index])
                    command_buffer.draw(4, 1)

                # Divider overlay, per tile, in the SAME open pass and with
                # the same viewport/scissor shape as that tile's base draw:
                # the reference tile is its own 160x90 target, and the divider
                # shader works in target-relative px, so the tile viewport IS
                # the reference's full-target viewport.
                if self._divider is not None and self._divider.pipeline is not None:
                    for index, (tile_left_fb, tile_top_fb, tile_w_fb, tile_h_fb) in enumerate(
                        fb_rects
                    ):
                        slot = self._divider_slots.get(index)
                        if slot is None:
                            continue
                        command_buffer.setGraphicsPipeline(self._divider.pipeline)
                        command_buffer.setViewport(
                            QRhiViewport(
                                float(tile_left_fb),
                                float(tile_top_fb),
                                float(tile_w_fb),
                                float(tile_h_fb),
                            )
                        )
                        command_buffer.setScissor(
                            QRhiScissor(
                                int(tile_left_fb),
                                int(tile_top_fb),
                                int(tile_w_fb),
                                int(tile_h_fb),
                            )
                        )
                        command_buffer.setVertexInput(
                            0, [(self._divider.vertex_buffer, 0)]
                        )
                        command_buffer.setShaderResources(
                            self._divider.slot_bindings[slot]
                        )
                        command_buffer.draw(4)
            finally:
                command_buffer.endPass()

    return _StripAtlasWidget()


def _clear_color():
    from PySide6.QtGui import QColor

    return QColor(0, 0, 0, 0)


def run_band_window(window, target_size=None):
    """Render the window the way the BAND does; slice its own framebuffer.

    The band's target is its own device-pixel size, which the host never
    equates with the window grid: ``StripPassResources`` scales the grid onto
    whatever ``band.renderTarget().pixelSize()`` reports. So this drives the
    production renderer into a target of the given size and slices the cells
    out of one grab. The rects the PASS recorded (``framebuffer_rects``,
    bottom-up / Y-flipped) are reported alongside; slicing uses the window's
    logical rects scaled to the grab, because a grab is top-down — phase 0's
    row-order lesson. An off-grid target is a resample, never a silent pass.
    """
    from engine.gpu.offscreen_canvas import (
        render_widget_frame,
        resize_and_show_offscreen_widget,
    )
    from tabs.image_compare.services.video_snapshot_rendering.strip_pass import (
        framebuffer_rects,
        scaled_tile_rects,
    )

    _qt_app()
    grid = (int(window.target_w), int(window.target_h))
    size = grid if target_size is None else (int(target_size[0]), int(target_size[1]))
    widget = _make_production_widget(window)
    resize_and_show_offscreen_widget(widget, size)
    _must_have_rhi(widget, "band strip window")

    pass_started = time.perf_counter()
    render_widget_frame(widget)
    render_widget_frame(widget)
    pass_ms = (time.perf_counter() - pass_started) * 1000.0
    if widget._last_error:
        raise _NoDisplayError(widget._last_error)

    from engine.gpu.export_tiling import qimage_to_pil_rgba

    readback_started = time.perf_counter()
    qimage = widget.grabFramebuffer()
    if qimage is None or qimage.isNull():
        raise _NoDisplayError(
            "band strip window: grabFramebuffer() returned a null image"
        )
    image = qimage_to_pil_rgba(qimage)
    readback_ms = (time.perf_counter() - readback_started) * 1000.0
    # What the PASS recorded (bottom-up framebuffer space, Y flipped where the
    # never-shown rule says so) — reported, never used to slice: a grab is
    # top-down, so the visual rects are the window's logical ones scaled.
    pass_rects = framebuffer_rects(
        window, widget.renderTarget(), rhi=widget.rhi(), never_shown=True
    )
    # Slicing uses the PASS's own rect rule (``scaled_tile_rects``: the same
    # edge snapping, minus the Y rule) against the grab's size: a grab is
    # top-down, so the Y-flipped pass rects must not be reused for slicing
    # (phase 0's row-order lesson) — but the X/width rule is one rule.
    rects = scaled_tile_rects(window, image.size[0], image.size[1])
    scale = (image.size[0] / float(grid[0]), image.size[1] / float(grid[1]))
    upload_stats = {"capacity": 0, "skipped": 0, "layers": 0}
    try:
        from tabs.image_compare.services.video_snapshot_rendering import strip_pass

        resources = strip_pass._PASSES.get(id(widget.rhi()))
        if resources is not None and resources._array is not None:
            upload_stats = {
                "capacity": int(resources._array.array_capacity),
                "skipped": int(resources._array.array_upload_skipped_count),
                "layers": int(resources._tile_count),
            }
    except Exception:
        pass
    try:
        widget.close()
        widget.deleteLater()
    except Exception:
        pass
    return {
        "image": image,
        "rects": rects,
        "pass_rects": pass_rects,
        "scale": scale,
        "pass_ms": pass_ms,
        "readback_ms": readback_ms,
        "upload": upload_stats,
    }


def _release_production_window(window) -> None:
    """Cancel the consumed window and drop its planning graph.

    ``cancel`` clears the module's live-window reference; the tiles' plans hold
    ``TiledPixelStore``s, and a store still alive at interpreter shutdown fails
    to unlink its spill file from ``__del__`` (noise, not a failure). GPU
    resources go with the widget's ``releaseResources``.
    """
    from tabs.image_compare.services.video_snapshot_rendering.strip_window import (
        cancel,
    )

    try:
        cancel(window, "bench finished with this window")
    except Exception:
        pass


def _grab_tiles(widget, rects, tile_w: int, tile_h: int):
    """Grab the atlas once and slice every tile; returns hashes/bytes/scale."""
    from engine.gpu.export_tiling import qimage_to_pil_rgba

    readback_started = time.perf_counter()
    qimage = widget.grabFramebuffer()
    if qimage is None or qimage.isNull():
        raise _NoDisplayError(
            "strip pass: grabFramebuffer() returned a null image "
            "(QRhi surface not present)"
        )
    image = qimage_to_pil_rgba(qimage)
    readback_ms = (time.perf_counter() - readback_started) * 1000.0

    scaled, scale = target_units(rects, image.size)
    if scale != (1.0, 1.0):
        # The reference tile is a logical 160x90 PIL render; comparing it to a
        # device-pixel slice would be a silent resample, not an exactness test.
        raise _PreconditionError(
            f"atlas framebuffer is {image.size[0]}x{image.size[1]} for a "
            f"{rects[-1][0] + tile_w}x{rects[-1][1] + tile_h} logical target "
            f"(device-pixel ratio {scale[0]:.2f}x{scale[1]:.2f}). Run with "
            "QT_SCALE_FACTOR=1: the bench compares 1:1 device pixels."
        )
    raw = image.tobytes("raw", "RGBA")
    hashes = []
    tile_bytes_list = []
    for rect in scaled:
        tile_bytes = slice_tile_bytes(raw, image.size, rect)
        tile_bytes_list.append(tile_bytes)
        hashes.append(hash_rgba(tile_bytes))
    return hashes, tile_bytes_list, readback_ms, scale


def run_batch_pass(tiles: list[TileFrame], cols: int, tile_service):
    """Render all tiles into one target; returns ``(tile_hashes, timings)``."""
    from engine.gpu.offscreen_canvas import (
        render_widget_frame,
        resize_and_show_offscreen_widget,
    )

    _qt_app()
    tile_w = tiles[0].tile_w
    tile_h = tiles[0].tile_h
    _, rows, atlas_w, atlas_h = plan_tile_grid(len(tiles), tile_w, tile_h, cols)
    widget = _make_atlas_widget(tiles, cols, tile_service)
    resize_and_show_offscreen_widget(widget, (atlas_w, atlas_h))
    _must_have_rhi(widget, "batched strip pass")

    pass_started = time.perf_counter()
    # Two pumps: QRhiWidget needs a second event-loop pass before the frame
    # it just recorded is actually present (see render_widget_frame).
    render_widget_frame(widget)
    render_widget_frame(widget)
    pass_ms = (time.perf_counter() - pass_started) * 1000.0
    if widget._last_error:
        raise _NoDisplayError(widget._last_error)

    logical_rects = tile_rects(len(tiles), tile_w, tile_h, cols)
    hashes, tile_bytes_list, readback_ms, scale = _grab_tiles(
        widget, logical_rects, tile_w, tile_h
    )
    try:
        array_capacity = (
            int(widget._resources.array_capacity) if widget._resources is not None else 0
        )
        upload_skipped = (
            int(widget._resources.array_upload_skipped_count)
            if widget._resources is not None
            else 0
        )
    except Exception:
        array_capacity, upload_skipped = 0, 0
    try:
        widget.close()
        widget.deleteLater()
    except Exception:
        pass
    return hashes, tile_bytes_list, {
        "pass_ms": pass_ms,
        "readback_ms": readback_ms,
        "rows": rows,
        "atlas": (atlas_w, atlas_h),
        "dpr_scale": scale,
        # What the pass actually sampled per tile: the layers the production
        # allocator handed out (not index*2), so a mismatch report can say
        # which layer a tile read.
        "layers": [(int(t.index), int(t.layer1), int(t.layer2)) for t in tiles],
        "divider_tiles": sorted(widget._divider_slots),
        # The capacity the driver actually backed (ArrayResources verifies and
        # halves a refused allocation) plus every upload it refused: a layer
        # the array cannot address is the difference between "the batch is
        # wrong" and "this backend will not hold N layers".
        "array_capacity": array_capacity,
        "upload_skipped": upload_skipped,
    }


# ---------------------------------------------------------------------------
# Production renderer seam (phase 1): build_window + render_window
# ---------------------------------------------------------------------------


def _proxies(image1_path, image2_path, tile_w: int, tile_h: int):
    """The shared proxy pair every frame resolves (the strip's real sources)."""
    from PIL import Image

    proxy1 = Image.open(image1_path).convert("RGBA").resize(
        (int(tile_w), int(tile_h)), Image.Resampling.BICUBIC
    )
    proxy2 = Image.open(image2_path).convert("RGBA").resize(
        (int(tile_w), int(tile_h)), Image.Resampling.BICUBIC
    )
    return proxy1, proxy2


class _BenchRecording:
    """The strip's recording contract, backed by this bench's own frame state.

    ``build_window`` drives ``evaluate_at`` + ``images_for`` exactly as the
    strip widget will drive the session recording, so the production path
    exercises the real planning seam rather than a re-packed tile list.
    """

    def __init__(
        self,
        store,
        image1_path,
        image2_path,
        proxy1,
        proxy2,
        *,
        varied: bool = False,
    ) -> None:
        self._store = store
        self._image1_path = image1_path
        self._image2_path = image2_path
        self._proxy1 = proxy1
        self._proxy2 = proxy2
        self._varied = bool(varied)

    def evaluate_at(self, timestamp: float):
        return _build_snapshot(
            self._store, timestamp, self._image1_path, self._image2_path
        )

    def images_for(self, snapshot, tile_size):
        if not self._varied:
            return self._proxy1, self._proxy2
        from PIL import ImageChops

        index = int(round(float(snapshot.timestamp) * FPS))
        width, height = int(tile_size[0]), int(tile_size[1])
        frame1 = ImageChops.offset(
            self._proxy1, (index * 7) % width, (index * 3) % height
        )
        frame2 = ImageChops.offset(
            self._proxy2, (index * 5) % width, (index * 11) % height
        )
        return frame1, frame2


def build_production_window(
    store,
    count: int,
    tile_w: int,
    tile_h: int,
    image1_path,
    image2_path,
    *,
    cols: int,
    varied: bool = False,
):
    """``build_window`` for N blocks, with this bench's frame state/sources."""
    from tabs.image_compare.services.video_snapshot_rendering.strip_window import (
        StripBlockLayout,
        build_window,
    )

    proxy1, proxy2 = _proxies(image1_path, image2_path, tile_w, tile_h)
    recording = _BenchRecording(
        store,
        str(image1_path),
        str(image2_path),
        proxy1,
        proxy2,
        varied=varied,
    )
    return build_window(
        recording,
        0,
        count,
        tile_w,
        tile_h,
        request=_build_request(tile_w, tile_h),
        layout=StripBlockLayout(
            tile_size=(int(tile_w), int(tile_h)),
            fps=FPS,
            block_count=int(count),
            cols=int(cols),
        ),
    )


def _make_production_widget(window):
    """A grab-only QRhiWidget whose whole ``render`` is the production pass."""
    from PySide6.QtWidgets import QRhiWidget

    from engine.gpu.offscreen_canvas import configure_offscreen_widget
    from tabs.image_compare.services.video_snapshot_rendering.strip_window import (
        render_window,
    )

    class _StripWindowWidget(QRhiWidget):
        """Delegates the window's single pass to production ``render_window``."""

        def __init__(self) -> None:
            super().__init__()
            configure_offscreen_widget(self)
            self._window = window
            self._rhi = None
            self._last_error: str | None = None

        def render(self, command_buffer):
            try:
                rhi = self.rhi()
                self._rhi = rhi
                render_window(
                    self.renderTarget(),
                    self._window,
                    rhi=rhi,
                    tile_service=self._window.tile_service,
                    command_buffer=command_buffer,
                    # This surface is grab-only (WA_DontShowOnScreen): the
                    # bottom-up framebuffer rule applies without a compositor.
                    never_shown=True,
                )
            except BaseException as exc:  # Qt swallows render-override raises
                self._last_error = f"render failed: {exc}"
                raise

        def releaseResources(self):
            from tabs.image_compare.services.video_snapshot_rendering.strip_pass import (
                release_window_resources,
            )

            rhi = self._rhi
            self._rhi = None
            if rhi is None:
                # Never renderered: releasing by ``None`` would drop every
                # RHI's resources, not just this widget's.
                return
            release_window_resources(rhi)

    return _StripWindowWidget()


def run_production_window(window):
    """Render the window through production ``render_window``; hash the tiles."""
    from engine.gpu.offscreen_canvas import (
        render_widget_frame,
        resize_and_show_offscreen_widget,
    )

    _qt_app()
    widget = _make_production_widget(window)
    resize_and_show_offscreen_widget(widget, (window.target_w, window.target_h))
    _must_have_rhi(widget, "production strip window")

    pass_started = time.perf_counter()
    render_widget_frame(widget)
    render_widget_frame(widget)
    pass_ms = (time.perf_counter() - pass_started) * 1000.0
    if widget._last_error:
        raise _NoDisplayError(widget._last_error)

    hashes, tile_bytes_list, readback_ms, scale = _grab_tiles(
        widget, window.rects(), window.tile_w, window.tile_h
    )
    timings = {
        "pass_ms": pass_ms,
        "readback_ms": readback_ms,
        "rows": window.rows,
        "atlas": (window.target_w, window.target_h),
        "dpr_scale": scale,
        "layers": [
            (int(tile.index), int(tile.layer1), int(tile.layer2))
            for tile in window.tiles
        ],
        "divider_tiles": sorted(
            index for index, tile in enumerate(window.tiles) if tile.divider
        ),
        "array_capacity": window.layer_count,
        "upload_skipped": 0,
    }
    try:
        widget.close()
        widget.deleteLater()
    except Exception:
        pass
    return hashes, tile_bytes_list, timings


# ---------------------------------------------------------------------------
# Band host seam (phase 2 wiring): build/plan exactly as the editor does
# ---------------------------------------------------------------------------


def filmstrip_proxy_size(source_size, tile_size):
    """The pyramid level the exporter's thumbnail seam hands back for a tile.

    ``VideoExporterService._resolve_thumbnail_images`` resolves a pyramid
    LEVEL (``_pick_thumbnail_proxy_from_base`` -> ``demand_level`` over
    ``thumbnail_proxy_dest_scale``), never a copy resized to the request box:
    a level keeps the SOURCE aspect and its size is a halving ladder of the
    source, so it only incidentally equals the tile. Modelling the production
    size rule (not "resize to the box") is what lets the band stage report see
    a proxy that disagrees with the tile aspect.
    """
    from tabs.image_compare.services.video_snapshot_rendering.geometry import (
        thumbnail_proxy_dest_scale,
    )

    sw = max(1, int(source_size[0]))
    sh = max(1, int(source_size[1]))
    tw = max(1, int(tile_size[0]))
    th = max(1, int(tile_size[1]))
    scale = thumbnail_proxy_dest_scale(sw, sh, tw, th)
    if scale <= 0.0:
        return (sw, sh)
    level_count = max(1, int(math.floor(math.log2(max(sw, sh)))) + 1)
    from engine.cache.lod import demand_level

    level = int(demand_level(scale, level_count))
    level = max(0, min(int(level), level_count - 1))
    return (max(1, sw >> level), max(1, sh >> level))


class _BandExporter:
    """The exporter seam the band's ``proxy_pair_for`` drives (no decode).

    Same public shape the host talks to: ``_resolve_thumbnail_images(snap,
    auto_crop, target_size) -> ((img1, img2), kinds)``. The pixels are the
    bench pair's encoded sources resampled to the pyramid level the service
    would have picked, so every stage the band consumes (proxy size, plan
    canvas, letterbox, pixels) is the one the editor sees.
    """

    def __init__(self, image1_path, image2_path) -> None:
        from PIL import Image

        self._source1 = Image.open(image1_path).convert("RGBA")
        self._source2 = Image.open(image2_path).convert("RGBA")
        self._levels: dict[tuple[int, int], tuple] = {}
        self.calls: list[tuple] = []

    @property
    def source_size(self) -> tuple[int, int]:
        return (self._source1.width, self._source1.height)

    def _resolve_thumbnail_images(self, snap, auto_crop, target_size):
        size = filmstrip_proxy_size(self.source_size, target_size)
        self.calls.append((bool(auto_crop), tuple(int(v) for v in target_size), size))
        return (self._level(size), ("pyramid", "pyramid"))

    def _level(self, size):
        """Both sides at one level size, hoisted: the strip resolves one pair."""
        key = (int(size[0]), int(size[1]))
        cached = self._levels.get(key)
        if cached is None:
            cached = (self._resized(self._source1, key), self._resized(self._source2, key))
            self._levels[key] = cached
        return cached

    @staticmethod
    def _resized(image, size):
        from PIL import Image

        if (image.width, image.height) == (int(size[0]), int(size[1])):
            return image
        return image.resize((int(size[0]), int(size[1])), Image.Resampling.BICUBIC)


def build_band_window(
    store,
    count: int,
    tile_w: int,
    tile_h: int,
    image1_path,
    image2_path,
    exporter: _BandExporter,
    *,
    cols: int | None = None,
):
    """``build_window`` driven exactly as ``StripBandHost`` drives it.

    The host's own pieces, not a bench re-implementation: the tile comes from
    ``strip_recording.strip_tile_size`` (the layout's declared size), the
    source pair from ``strip_recording.proxy_pair_for`` over the exporter seam
    (never a decode), the plan request from ``strip_band.render_request``
    (the same ``VideoRenderRequest`` the host builds), and the recording is
    the host's ``StripRecordingAdapter``. Only the block layout is synthetic —
    the bench has no timeline — and it is the same ``StripBlockLayout`` shape.
    """
    from tabs.image_compare.plugins.video_editor.presenter_parts.strip_band import (
        render_request as band_render_request,
    )
    from tabs.image_compare.plugins.video_editor.services.strip_recording import (
        StripRecordingAdapter,
        proxy_pair_for,
    )
    from tabs.image_compare.services.video_snapshot_rendering.strip_window import (
        StripBlockLayout,
        build_window,
    )

    resolved_cols = int(count) if cols is None else int(cols)
    layout = StripBlockLayout(
        tile_size=(int(tile_w), int(tile_h)),
        fps=FPS,
        block_count=int(count),
        cols=resolved_cols,
    )
    frames = _BenchRecording(
        store, str(image1_path), str(image2_path), None, None
    )
    recording = StripRecordingAdapter(
        frames,
        proxy_pair=proxy_pair_for(exporter),
        layout=layout,
        fps=layout.fps,
    )
    return build_window(
        recording,
        0,
        int(count),
        int(tile_w),
        int(tile_h),
        request=band_render_request(int(tile_w), int(tile_h)),
        layout=layout,
    )


def band_stages(window, exporter: _BandExporter, target_size=None) -> dict:
    """Every value that decides what the band draws, in one dict.

    The band's divergence instrument: a stage that disagrees with the
    filmstrip contract names itself here instead of showing up as "wrong
    pixels". ``target_size`` is the render target's DEVICE pixel size (the
    band's own ``renderTarget().pixelSize()``); ``None`` means the window grid
    1:1.
    """
    from tabs.image_compare.services.video_snapshot_rendering.geometry import (
        THUMBNAIL_TARGET_SIZE,
    )
    from tabs.image_compare.services.video_snapshot_rendering.strip_window import (
        tile_letterbox_geometry,
    )

    grid = (int(window.target_w), int(window.target_h))
    target = grid if target_size is None else (int(target_size[0]), int(target_size[1]))
    tiles = []
    for tile in window.tiles:
        letterbox, canvas_letterbox, content_rect = tile_letterbox_geometry(tile)
        tiles.append(
            {
                "index": int(tile.index),
                "tile": (int(tile.tile_w), int(tile.tile_h)),
                "canvas": (int(tile.canvas_w), int(tile.canvas_h)),
                "image": (
                    int(getattr(tile.image1, "width", 0)),
                    int(getattr(tile.image1, "height", 0)),
                ),
                "letterbox": tuple(round(float(v), 4) for v in letterbox),
                "canvas_letterbox": tuple(
                    round(float(v), 4) for v in canvas_letterbox
                ),
                "content_rect": tuple(int(v) for v in content_rect),
                "placeholder": tile.placeholder_reason,
            }
        )
    return {
        "grid": grid,
        "target": target,
        "scale": (target[0] / float(grid[0]), target[1] / float(grid[1])),
        "count": int(window.count),
        "cols": int(window.cols),
        "rows": int(window.rows),
        "thumbnail": tuple(int(v) for v in THUMBNAIL_TARGET_SIZE),
        "proxy_calls": list(exporter.calls[:1]),
        "source_size": exporter.source_size,
        "tiles": tiles,
    }


def band_divergences(stages: dict) -> list[str]:
    """Stages that disagree with the filmstrip contract (empty = none).

    The contract: one band tile IS one filmstrip thumbnail — the request target
    the QPainter strip rendered (``THUMBNAIL_TARGET_SIZE``, the one size that
    selects the plan builder's cover path and the pyramid pick the thumbnail
    seam uses) — and the pass stretches that tile over one block cell, whose
    aspect is the timeline's, not the frame's.

    So the tile's PLAN must be full-bleed: the content box is the whole canvas
    and nothing is left empty for the cell to show beside it. A fit-inside tile
    (the ``is_thumbnail_target_size`` cover branch covering the already-fitted
    content box instead of the canvas) keeps the frame's letterbox bars, and
    the pass then stretches those bars over the cell — the "each cell shows a
    narrow strip of the frame with empty space beside it" report
    (2026-11-03: a portrait source left 40 of 160 px of content, i.e. 32 of a
    128 px cell). This gate names that stage by its empty fraction instead of
    leaving it to the pixel comparison, which cannot see it: the per-tile
    reference is rendered from the SAME plan and carries the same bars.
    """
    findings: list[str] = []
    thumbnail = stages["thumbnail"]
    for tile in stages["tiles"]:
        if tile["placeholder"]:
            findings.append(
                f"tile {tile['index']}: placeholder ({tile['placeholder']})"
            )
            continue
        if tile["tile"] != thumbnail:
            findings.append(
                f"tile {tile['index']}: tile {tile['tile']} is not the filmstrip "
                f"thumbnail {thumbnail} — the plan takes the fit-inside path and "
                f"is drawn inside letterbox {tile['letterbox']} "
                f"(content rect {tile['content_rect']})"
            )
        if tile["canvas"] != tile["tile"]:
            findings.append(
                f"tile {tile['index']}: plan canvas {tile['canvas']} != tile "
                f"{tile['tile']}"
            )
        filled = _content_fills_tile(tile)
        if not filled:
            findings.append(
                f"tile {tile['index']}: content rect {tile['content_rect']} does "
                f"not fill the {tile['tile']} tile (letterbox {tile['letterbox']}, "
                f"plan image {tile['image']}) — the pass stretches the tile over "
                "a block cell, so every empty part of the tile becomes an empty "
                "part of the cell"
            )
    return findings


def _content_fills_tile(tile: dict) -> bool:
    """Whether the tile's plan content covers the whole tile canvas."""
    tile_w, tile_h = tile["tile"]
    x, y, w, h = tile["content_rect"]
    letterbox = tile["letterbox"]
    return (
        (int(x), int(y)) == (0, 0)
        and (int(w), int(h)) == (int(tile_w), int(tile_h))
        and all(abs(float(v) - target) <= 1e-6 for v, target in zip(letterbox, (0.0, 0.0, 1.0, 1.0)))
    )


def print_band_stages(stages: dict, divergences, quiet: bool = False) -> None:
    """Print the band stage table (and the stages that diverge)."""
    if quiet:
        return
    tile = stages["tiles"][0] if stages["tiles"] else {}
    print(
        f"  band target {stages['target'][0]}x{stages['target'][1]} px for a "
        f"{stages['grid'][0]}x{stages['grid'][1]} grid "
        f"({stages['count']} blocks, {stages['cols']}x{stages['rows']} cells)"
    )
    if tile:
        print(
            f"  band tile {tile['tile']} (filmstrip thumbnail "
            f"{stages['thumbnail'][0]}x{stages['thumbnail'][1]}), plan canvas "
            f"{tile['canvas']}, plan image {tile['image']}, letterbox "
            f"{tile['letterbox']}, content rect {tile['content_rect']}"
        )
    for call in stages["proxy_calls"]:
        print(
            f"  band proxy: requested {call[1]} -> {call[2]} "
            f"(source {stages['source_size'][0]}x{stages['source_size'][1]})"
        )
    scale = stages["scale"]
    if abs(scale[0] - 1.0) > 1e-9 or abs(scale[1] - 1.0) > 1e-9:
        # A note, not a verdict: the host's band is its own widget size, and
        # the strip has always scaled one tile into one block cell. The cells
        # are still compared (against the reference resampled to the cell).
        print(
            f"  note: target {stages['target']} != grid {stages['grid']} "
            f"(scale {scale[0]:.3f}x{scale[1]:.3f}) — every cell is a uniform "
            "resample, compared against the reference resampled to the cell"
        )
    if divergences:
        for finding in divergences:
            print(f"  DIVERGES: {finding}")
    else:
        print("  band stages: all agree with the filmstrip contract")


#: Off-grid band target verdict: every cell is compared with its own per-tile
#: reference RESAMPLED to the cell's device size. A fractional resample is not
#: byte-identical by construction (the GPU filters the layer with its own
#: sampler and mips; PIL resamples the reference), so the verdict is "same
#: content, same footprint" with a measured tolerance. A geometry break — a
#: tile at the wrong offset/layer, a letterboxed sliver, a mirrored row — is
#: 10-100x these numbers, which is why they are stated instead of a hash.
BAND_RESAMPLE_MEAN_MAX = 12.0
BAND_RESAMPLE_OUTLIER_DELTA = 64
#: Measured worst cell at the live target: mean 2.4/255 and 1.6% of pixels
#: beyond 64/255 (the divider line, where the GPU's own filtering of a 3 px bar
#: and PIL's resample of it agree only approximately). A geometry break — a
#: tile at the wrong offset or layer, a sliver, a mirrored row — puts 30-100%
#: of the cell's pixels there, i.e. an order of magnitude past this.
BAND_RESAMPLE_OUTLIER_FRAC_MAX = 0.03


def compare_band_cells(band_result, reference, tile_size):
    """``(matching, total, worst_mean, worst_outlier_frac, findings)``.

    Slices each cell out of the one band readback and compares it with its own
    reference tile (rendered by the per-tile route at ``tile_size``) resized to
    that cell. Off-grid only: an on-grid target is byte-exact and compared as
    bytes by the caller.
    """
    from PIL import Image

    image = band_result["image"]
    size = image.size
    raw = image.tobytes("raw", "RGBA")
    matching = 0
    worst_mean = 0.0
    worst_frac = 0.0
    findings = []
    rects = band_result["rects"]
    for index, rect in enumerate(rects):
        if index >= len(reference):
            findings.append(f"cell {index}: no reference tile")
            continue
        cell = Image.frombytes(
            "RGBA", (int(rect[2]), int(rect[3])), slice_tile_bytes(raw, size, rect)
        )
        ref_raw = reference[index][2]
        ref = Image.frombytes(
            "RGBA", (int(tile_size[0]), int(tile_size[1])), ref_raw
        )
        expected = ref.resize(cell.size, Image.Resampling.LANCZOS).tobytes(
            "raw", "RGBA"
        )
        actual = cell.tobytes("raw", "RGBA")
        total_delta = 0
        outliers = 0
        for offset in range(0, len(actual), 4):
            worst_channel = 0
            for channel in range(4):
                delta = abs(actual[offset + channel] - expected[offset + channel])
                total_delta += delta
                if delta > worst_channel:
                    worst_channel = delta
            if worst_channel > BAND_RESAMPLE_OUTLIER_DELTA:
                outliers += 1
        pixels = max(1, len(actual) // 4)
        mean = total_delta / float(max(1, len(actual)))
        frac = outliers / float(pixels)
        worst_mean = max(worst_mean, mean)
        worst_frac = max(worst_frac, frac)
        if mean <= BAND_RESAMPLE_MEAN_MAX and frac <= BAND_RESAMPLE_OUTLIER_FRAC_MAX:
            matching += 1
        else:
            findings.append(
                f"cell {index} {cell.size}: mean {mean:.1f}/255, "
                f"{100.0 * frac:.1f}% of px beyond {BAND_RESAMPLE_OUTLIER_DELTA}/255 "
                "vs the reference resampled to the cell"
            )
    total = len(rects)
    return matching, total, worst_mean, worst_frac, findings


# ---------------------------------------------------------------------------
# Self-test (no GPU, no Qt display)
# ---------------------------------------------------------------------------


def run_self_test(quiet: bool = False) -> int:
    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            if not quiet:
                print(f"ok   {name}")
            return
        failures.append(f"{name}{(': ' + detail) if detail else ''}")
        print(f"FAIL {name}{(': ' + detail) if detail else ''}")

    # 1. tile grid / viewport rects
    for count in DEFAULT_FRAMES:
        cols, rows, width, height = plan_tile_grid(count, 160, 90)
        rects = tile_rects(count, 160, 90, cols)
        check(f"N={count}: one rect per tile", len(rects) == count)
        check(
            f"N={count}: rects stay inside the atlas",
            all(x + w <= width and y + h <= height for x, y, w, h in rects),
        )
        check(
            f"N={count}: rects do not overlap",
            len({(x, y) for x, y, _w, _h in rects}) == len(rects),
        )
        drawn = sum(w * h for _x, _y, w, h in rects)
        check(
            f"N={count}: every rect is exactly one tile",
            all((w, h) == (160, 90) for _x, _y, w, h in rects),
            f"drawn_area={drawn}",
        )
        cells = {(x // 160, y // 90) for x, y, _w, _h in rects}
        expected_cells = {(index % cols, index // cols) for index in range(count)}
        check(
            f"N={count}: grid cells match the row-major fill",
            cells == expected_cells,
            f"{sorted(cells)} != {sorted(expected_cells)}",
        )

    # 2. uniform stride / packing matches the shader's std140 block
    uniform_module = _load_uniform_module()
    block_size = int(uniform_module._ARRAY_UNIFORM_BLOCK_SIZE)
    packed_size = struct.calcsize(uniform_module._ARRAY_UNIFORM_FMT)
    check("uniform block size == shader std140 size", block_size == packed_size, f"{block_size} vs {packed_size}")
    check("uniform stride is 256-byte aligned", UNIFORM_STRIDE % 256 == 0)
    check("uniform block fits the stride", block_size <= UNIFORM_STRIDE, f"{block_size} > {UNIFORM_STRIDE}")

    from engine.gpu.array_draw import ARRAY_INSTANCE_STRIDE, ARRAY_INSTANCE_FMT

    check(
        "instance stride == packed instance size",
        int(ARRAY_INSTANCE_STRIDE) == struct.calcsize(ARRAY_INSTANCE_FMT),
        f"{ARRAY_INSTANCE_STRIDE} vs {struct.calcsize(ARRAY_INSTANCE_FMT)}",
    )
    tile = _synthetic_tile()
    unpacked = uniform_module.pack_array_uniforms(
        _FixedClipRhi(), _base_image_view(tile), diff_source_ready=False
    )
    check("packed uniform block length", len(unpacked) == block_size, str(len(unpacked)))
    padded = pad_uniform_block(unpacked)
    check("padding preserves the block bytes", padded[:block_size] == unpacked and len(padded) == UNIFORM_STRIDE)
    check(
        "second tile's uniform offset is stride-aligned",
        uniform_offset(1) == UNIFORM_STRIDE and uniform_offset(0) == 0,
    )
    instance = pack_tile_instance(tile, 0, 1)
    check("packed instance length", len(instance) == ARRAY_INSTANCE_STRIDE, str(len(instance)))
    from engine.gpu.array_draw import unpack_instance_layer2

    check("instance layer2 round-trips", unpack_instance_layer2(instance, 0) == 1)

    # 3. hash plumbing: deterministic, per-tile sensitive
    from PIL import Image

    image = Image.new("RGBA", (40, 40))
    image.putdata(
        [((x * 7) % 256, (y * 11) % 256, (x + y) % 256, 255) for y in range(40) for x in range(40)]
    )
    raw = image.tobytes("raw", "RGBA")
    rects = tile_rects(4, 20, 20, 2)
    first = [hash_rgba(slice_tile_bytes(raw, image.size, rect)) for rect in rects]
    second = [hash_rgba(slice_tile_bytes(raw, image.size, rect)) for rect in rects]
    check("tile hashing is deterministic", first == second)
    check("distinct tiles hash differently", len(set(first)) == len(first))
    mutated = bytearray(raw)
    mutated[0] = (mutated[0] + 1) % 256
    after = [hash_rgba(slice_tile_bytes(bytes(mutated), image.size, rect)) for rect in rects]
    check("one changed pixel moves exactly one tile hash", after[0] != first[0] and after[1:] == first[1:])
    check(
        "slice rejects out-of-range rects",
        _raises(ValueError, slice_tile_bytes, raw, image.size, (30, 0, 20, 20)),
    )

    # 4. DPR remap keeps the grid exact
    scaled, (sx, sy) = target_units(rects, (80, 80))
    check("DPR remap scales the grid", (sx, sy) == (2.0, 2.0) and scaled[-1][:2] == (40, 40), str(scaled))

    # 5. layers come from the production allocator, not from the tile index
    synthetic = [_synthetic_tile(index) for index in range(5)]
    service, layer_count = assign_tile_layers(synthetic)
    layer_pairs = [tile_layers(tile, service) for tile in synthetic]
    flat = [layer for pair in layer_pairs for layer in pair]
    check("layers: one per tile side", len(flat) == 2 * len(synthetic), str(flat))
    check("layers: no two sides share a layer", len(set(flat)) == len(flat), str(flat))
    check(
        "layers: every layer addressable by the bound array",
        all(0 <= layer < layer_count for layer in flat),
        f"{flat} vs capacity {layer_count}",
    )
    check(
        "layers: every side is resident in the service",
        all(
            service.is_resident(tile_source_key(tile, side), (0, 0))
            for tile in synthetic
            for side, _image in _tile_sides(tile)
        ),
    )
    replay_service, _cap = assign_tile_layers([_synthetic_tile(index) for index in range(5)])
    replay = [
        tile_layers(tile, replay_service) for tile in [_synthetic_tile(i) for i in range(5)]
    ]
    check("layers: assignment is deterministic", replay == layer_pairs, f"{replay} vs {layer_pairs}")

    # 6. divider overlay packs per tile in the production 128 B layout
    from engine.overlay.semantic import DIVIDER_UNIFORM_FMT
    from types import SimpleNamespace

    identity = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)

    def _divider_tile(split: float, show: bool = True, thickness: int = 3):
        plan = _SyntheticPlan()
        plan.render_scene = SimpleNamespace(
            split_position_visual=split,
            is_horizontal=False,
            overlay_clip_rect=None,
            feature_overrides={
                "show_divider": show,
                "divider_thickness": thickness,
            },
        )
        return TileFrame(
            index=0,
            plan=plan,
            image1=plan.image1,
            image2=plan.image2,
            canvas_w=160,
            canvas_h=90,
            tile_w=160,
            tile_h=90,
        )

    divider_block = pack_tile_divider(_divider_tile(0.3), identity)
    check(
        "divider: block is the production 128 B size",
        len(divider_block) == 128,
        str(len(divider_block)),
    )
    fields = struct.unpack(DIVIDER_UNIFORM_FMT, divider_block)
    check(
        "divider: position/fb size follow the plan's split",
        abs(fields[16] - 160.0) < 1e-6
        and abs(fields[17] - 90.0) < 1e-6
        and abs(fields[18] - 160.0 * 0.3) < 1e-4,
        f"fb=({fields[16]}, {fields[17]}) position={fields[18]}",
    )
    check(
        "divider: per-tile splits pack different blocks",
        pack_tile_divider(_divider_tile(0.3), identity)
        != pack_tile_divider(_divider_tile(0.7), identity),
    )
    check(
        "divider: hidden/unthick divider packs nothing",
        pack_tile_divider(_divider_tile(0.3, show=False), identity) == b""
        and pack_tile_divider(_divider_tile(0.3, thickness=0), identity) == b"",
    )

    print(f"\nself-test: {len(failures)} failure(s)")
    return 0 if not failures else 1


def _raises(exc, func, *args, **kwargs) -> bool:
    try:
        func(*args, **kwargs)
    except exc:
        return True
    return False


class _SyntheticPlan:
    """Minimal plan stand-in for the pure-function self-test."""

    render_scene = None

    def __init__(self) -> None:
        from PIL import Image

        self.canvas_w = 160
        self.canvas_h = 90
        self.image1 = Image.new("RGBA", (160, 90), (10, 20, 30, 255))
        self.image2 = Image.new("RGBA", (160, 90), (40, 50, 60, 255))


def _synthetic_tile(index: int = 0) -> TileFrame:
    plan = _SyntheticPlan()
    return TileFrame(
        index=int(index),
        plan=plan,
        image1=plan.image1,
        image2=plan.image2,
        canvas_w=160,
        canvas_h=90,
        tile_w=160,
        tile_h=90,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_frames(text: str) -> tuple[int, ...]:
    values = tuple(int(part) for part in str(text).replace(" ", "").split(",") if part)
    if not values or any(value <= 0 for value in values):
        raise argparse.ArgumentTypeError("--frames must be a comma-separated list of positive ints")
    return values


def _parse_tile(text: str) -> tuple[int, int]:
    parts = str(text).lower().replace(" ", "").split("x")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("--tiles must look like 160x90")
    width, height = int(parts[0]), int(parts[1])
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("--tiles must be positive")
    return width, height


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Phase-0 bench: N strip frame plans in one GPU pass."
    )
    parser.add_argument("--self-test", action="store_true", help="pure-function checks, no GPU")
    parser.add_argument("--cpu-only", action="store_true", help="planning + uniform packing only")
    parser.add_argument(
        "--renderer",
        choices=("local", "production"),
        default="local",
        help=(
            "local = this bench's own atlas widget (the phase-0 reference "
            "implementation); production = the app module "
            "video_snapshot_rendering.strip_window build_window + render_window"
        ),
    )
    parser.add_argument("--frames", default=",".join(str(n) for n in DEFAULT_FRAMES))
    parser.add_argument(
        "--band",
        action="store_true",
        help=(
            "drive the editor's band wiring instead of a bare window: the tile "
            "comes from strip_recording.strip_tile_size (the layout's declared "
            "size), the VideoRenderRequest from strip_band.render_request, the "
            "source pair from proxy_pair_for over an exporter seam that resolves "
            "a pyramid LEVEL (the production size rule), and the target is the "
            "band's own device-pixel size (--band-target). Prints the stage "
            "table and names the stage that diverges from the filmstrip "
            "contract. --tiles overrides the tile (--tiles 128x72 reproduces "
            "the phase-2 cell-aspect tile)."
        ),
    )
    parser.add_argument(
        "--band-target",
        default=None,
        help=(
            "band render-target pixel size WxH (default = the window grid, i.e. "
            "cols*tile). An off-grid target is the live case (a 1280x90 grid "
            "drawn into the band's own device size, e.g. 1020x72): every cell "
            "is then compared with its own per-tile reference RESAMPLED to the "
            "cell, with a stated tolerance (a fractional resample is never "
            "byte-identical), and the stage table still demands a full-bleed "
            "tile."
        ),
    )
    parser.add_argument(
        "--band-cols",
        default=None,
        help=(
            "columns of the band window (default = the square-ish column count "
            "for N). The timeline's band is ONE row, so the live geometry is "
            "--band-cols N: e.g. -N 8 --band-cols 8 --band-target 1020x72 is "
            "an 8 x 160x90 grid (1280x90) drawn into the band's 1020x72 device "
            "size."
        ),
    )
    parser.add_argument("--tiles", default=None)
    parser.add_argument("--reference-samples", type=int, default=0, help="hash only the first K reference tiles (0 = all)")
    parser.add_argument(
        "--varied-sources",
        action="store_true",
        help=(
            "worst case: give every frame its own shifted pixels instead of the "
            "session's shared pair (planning/packing scope; the GPU exactness "
            "column is informational in this mode)"
        ),
    )
    parser.add_argument("-q", "--quiet", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_test(quiet=args.quiet)

    frames = _parse_frames(args.frames)
    if args.tiles is None:
        if args.band:
            from tabs.image_compare.plugins.video_editor.services.strip_recording import (
                strip_tile_size,
            )

            tile_w, tile_h = strip_tile_size()
        else:
            tile_w, tile_h = DEFAULT_TILE
    else:
        tile_w, tile_h = _parse_tile(args.tiles)
    band_target = _parse_tile(args.band_target) if args.band_target else None
    if band_target is not None and not args.band:
        print("\n--band-target only applies with --band", file=sys.stderr)
        return 1
    band_cols = int(args.band_cols) if args.band_cols else None
    if band_cols is not None:
        if not args.band:
            print("\n--band-cols only applies with --band", file=sys.stderr)
            return 1
        if band_cols <= 0:
            print("\n--band-cols must be positive", file=sys.stderr)
            return 1
    if max(tile_w, tile_h) > LAYER_PX:
        # Production uploads a tile 1:1 into one array layer and refuses an
        # image larger than the layer (ArrayResources._image_rejection), so a
        # bigger tile would read back blank and look like a batching verdict.
        print(
            f"\nprecondition not met: --tiles {tile_w}x{tile_h} exceeds the "
            f"{LAYER_PX}px array layer (one tile is uploaded 1:1 into one layer).",
            file=sys.stderr,
        )
        return 1

    workdir = Path(tempfile.mkdtemp(prefix="bench-strip-batch-"))
    registry = None
    previous_tab = None
    try:
        _qt_app()
        registry, previous_tab = _activate_image_compare_tab()
        if not args.cpu_only:
            preflight_rhi()
        if not args.quiet:
            print(f"sources: generating {SOURCE_SIZE[0]}x{SOURCE_SIZE[1]} pair in {workdir}")
        image1_path, image2_path = make_sources(workdir)

        rows = []
        # Band-mode verdicts: per-N stage findings (a diverging stage is a
        # failure even when the batch itself is byte-exact).
        band_stage_findings: dict[int, list[str]] = {}

        # The production renderer plans through the app's own recording seam,
        # so it needs the store the plan builder reads (same store shape
        # ``build_tiles`` builds internally for the local path).
        production_store = (
            _build_store() if (args.band or args.renderer == "production") else None
        )
        # One warm-up plan before any timing: source decode + first plan
        # compile are one-time costs that would otherwise land on N=17 and
        # read as "planning got 10x cheaper with more tiles".
        _warm_tiles, warmup_ms = build_tiles(
            1,
            tile_w,
            tile_h,
            image1_path,
            image2_path,
            verbose=False,
            varied_sources=args.varied_sources,
        )
        if not args.quiet:
            print(f"warm-up plan (source decode): {warmup_ms:.1f} ms")
        for count in frames:
            if not args.quiet:
                print(f"\nN={count}")
            seam = "band" if args.band else args.renderer
            exporter = None
            stages = None
            if seam == "band":
                exporter = _BandExporter(image1_path, image2_path)
                window = build_band_window(
                    production_store,
                    count,
                    tile_w,
                    tile_h,
                    image1_path,
                    image2_path,
                    exporter,
                    cols=band_cols if band_cols is not None else _cols_for(count),
                )
                tiles = list(window.tiles)
                plan_ms = window.build_ms
                pack_ms = 0.0
                placeholders = window.placeholder_reasons
                stages = band_stages(window, exporter, band_target)
                divergences = band_divergences(stages)
                print_band_stages(stages, divergences, quiet=args.quiet)
            elif seam == "production":
                window = build_production_window(
                    production_store,
                    count,
                    tile_w,
                    tile_h,
                    image1_path,
                    image2_path,
                    cols=_cols_for(count),
                    varied=args.varied_sources,
                )
                tiles = list(window.tiles)
                plan_ms = window.build_ms
                pack_ms = 0.0
                placeholders = window.placeholder_reasons
            else:
                tiles, plan_ms = build_tiles(
                    count,
                    tile_w,
                    tile_h,
                    image1_path,
                    image2_path,
                    verbose=not args.quiet,
                    varied_sources=args.varied_sources,
                )
                pack_ms, tile_service = pack_tiles(tiles)
                placeholders = {}
            if args.cpu_only:
                if not args.quiet:
                    if seam in ("production", "band"):
                        label = "band window" if seam == "band" else "production window"
                        print(
                            f"  {label}: {len(tiles)} tiles planned + "
                            f"packed in {plan_ms:.2f} ms "
                            f"(block {_uniform_block_size()}B, stride "
                            f"{UNIFORM_STRIDE}B, instance "
                            f"{len(tiles[0].instance)}B); placeholders "
                            f"{len(placeholders)}; layers "
                            f"{[(t.index, t.layer1, t.layer2) for t in tiles[:4]]}"
                            f"{'...' if len(tiles) > 4 else ''}"
                        )
                    else:
                        print(
                            f"  pack {len(tiles)} tile uniforms+instances in {pack_ms:.2f} ms "
                            f"(block {_uniform_block_size()}B, stride {UNIFORM_STRIDE}B, "
                            f"instance {len(tiles[0].instance)}B); layers "
                            f"{[(t.index, t.layer1, t.layer2) for t in tiles[:4]]}"
                            f"{'...' if len(tiles) > 4 else ''}"
                        )
                rows.append((count, plan_ms, pack_ms, None, None, None, ""))
                if seam in ("production", "band"):
                    _release_production_window(window)
                    window = None
                    tiles = None
                continue
            if placeholders:
                raise _PreconditionError(
                    f"the production window left placeholder tiles {sorted(placeholders)}: "
                    "the exactness matrix needs every block planned (a placeholder "
                    "is a planning failure, not a rendering result)"
                )

            reference, reference_ms = render_reference_tiles(
                tiles, args.reference_samples or None
            )
            band_result = None
            band_cells = None
            if seam == "band":
                band_result = run_band_window(window, band_target)
                scale = band_result["scale"]
                exact_1to1 = abs(scale[0] - 1.0) <= 1e-9 and abs(
                    scale[1] - 1.0
                ) <= 1e-9
                batch_hashes: list = []
                batch_bytes: list = []
                if exact_1to1:
                    raw = band_result["image"].tobytes("raw", "RGBA")
                    batch_bytes = [
                        slice_tile_bytes(raw, band_result["image"].size, rect)
                        for rect in band_result["rects"]
                    ]
                    batch_hashes = [hash_rgba(blob) for blob in batch_bytes]
                else:
                    # The live case: the grid is drawn into the band's own
                    # device size. Compare every cell with its own reference
                    # RESAMPLED to the cell (a fractional resample cannot be
                    # byte-identical) and keep the numbers.
                    band_cells = compare_band_cells(
                        band_result, reference, (tile_w, tile_h)
                    )
                timings = {
                    "pass_ms": band_result["pass_ms"],
                    "readback_ms": band_result["readback_ms"],
                    "upload": band_result["upload"],
                    "layers": [
                        (int(tile.index), int(tile.layer1), int(tile.layer2))
                        for tile in window.tiles
                    ],
                    "divider_tiles": [
                        int(tile.index) for tile in window.tiles if tile.divider
                    ],
                }
            elif seam == "production":
                batch_hashes, batch_bytes, timings = run_production_window(window)
            else:
                batch_hashes, batch_bytes, timings = run_batch_pass(
                    tiles, cols=_cols_for(count), tile_service=tile_service
                )
            resampled_note = ""
            if band_cells is not None:
                matches, total, worst_mean, worst_frac, cell_findings = band_cells
                resampled_note = " (resampled)"
                print(
                    f"  off-grid band target {band_result['image'].size} for a "
                    f"{window.target_w}x{window.target_h} grid (scale "
                    f"{scale[0]:.3f}x{scale[1]:.3f}): every cell compared with "
                    "its own tile resampled to the cell"
                )
                print(
                    f"  worst cell: mean {worst_mean:.2f}/255, "
                    f"{100.0 * worst_frac:.2f}% of px beyond "
                    f"{BAND_RESAMPLE_OUTLIER_DELTA}/255 (tolerance mean <= "
                    f"{BAND_RESAMPLE_MEAN_MAX:.0f}, outliers <= "
                    f"{100.0 * BAND_RESAMPLE_OUTLIER_FRAC_MAX:.0f}%)"
                )
                for finding in cell_findings[:4]:
                    print(f"  MISMATCH {finding}")
            else:
                matches = sum(
                    1
                    for index, (ref_hash, _dbg, _raw) in enumerate(reference)
                    if index < len(batch_hashes) and batch_hashes[index] == ref_hash
                )
                total = len(reference)
            still_offgrid = band_cells is not None
            if not args.quiet:
                print(
                    f"  reference {len(reference)} tiles in {reference_ms:.1f} ms; "
                    f"pass in {timings['pass_ms']:.2f} ms; "
                    f"readback in {timings['readback_ms']:.2f} ms"
                )
            first_bad = None
            if not still_offgrid:
                first_bad = next(
                    (
                        index
                        for index, (ref_hash, _dbg, _raw) in enumerate(reference)
                        if index >= len(batch_hashes) or batch_hashes[index] != ref_hash
                    ),
                    None,
                )
            total_px = tile_w * tile_h
            blank = [
                index
                for index, blob in enumerate(batch_bytes)
                if not any(blob[3::4])
            ]
            layers = {index: (layer1, layer2) for index, layer1, layer2 in timings["layers"]}
            if blank:
                print(
                    f"  {len(blank)}/{len(batch_bytes)} tiles read back fully "
                    f"transparent (tiles {blank[:8]}"
                    f"{'...' if len(blank) > 8 else ''}): their texture-array "
                    "layer was not populated — an upload limit, not a geometry "
                    "or viewport mismatch"
                )
            if first_bad is not None:
                ref_hash, _dbg, ref_raw = reference[first_bad]
                print(
                    f"  MISMATCH tile {first_bad}: reference {ref_hash[:16]} vs batch "
                    f"{(batch_hashes[first_bad][:16] if first_bad < len(batch_hashes) else '<missing>')}; "
                    f"tile read layers {layers.get(first_bad, ('?', '?'))}"
                )
                if first_bad < len(batch_bytes) and first_bad not in blank:
                    differing, first_xy = diff_stats(
                        ref_raw, batch_bytes[first_bad], (tile_w, tile_h)
                    )
                    print(
                        f"  tile {first_bad}: {differing}/{total_px} px differ "
                        f"({100.0 * differing / total_px:.1f}%), first at {first_xy} "
                        "(base draw + divider; nothing else is in either scope)"
                    )
                    same = total_px - differing
                    print(f"  tile {first_bad}: {same}/{total_px} px byte-identical")
                populated = [
                    index
                    for index, blob in enumerate(batch_bytes)
                    if any(blob[3::4])
                ]
                worst = max(
                    (
                        diff_stats(reference[index][2], batch_bytes[index], (tile_w, tile_h))[0]
                        for index in populated
                    ),
                    default=0,
                )
                print(f"  worst populated tile: {worst}/{total_px} px differ")
            if not args.quiet:
                print(
                    "  tile -> (layer1, layer2) actually sampled: "
                    + ", ".join(
                        f"{index}:{layers[index]}" for index in sorted(layers)
                    )
                )
                if timings.get("upload"):
                    print(
                        f"  band array: {timings['upload']['layers']} tiles, "
                        f"capacity {timings['upload']['capacity']} layers, "
                        f"uploads skipped {timings['upload']['skipped']}"
                    )
                if timings.get("divider_tiles") is not None:
                    print(
                        f"  divider drawn per tile for {len(timings['divider_tiles'])}"
                        f"/{len(tiles)} tiles"
                    )
            if seam == "band":
                band_stage_findings[count] = list(divergences)
            rows.append(
                (
                    count,
                    plan_ms,
                    timings["pass_ms"],
                    timings["readback_ms"],
                    matches,
                    total,
                    resampled_note,
                )
            )
            if seam in ("production", "band"):
                _release_production_window(window)
                window = None
                tiles = None

        print()
        if args.cpu_only:
            print("N | plan_ms | pack_ms")
            for count, plan_ms, pack_ms, _p, _r, _m, _n in rows:
                print(f"{count:>3} | {plan_ms:>7.1f} | {pack_ms:>7.2f}")
            return 0

        print("N | plan_ms | pass_ms | readback_ms | exact_matches/N")
        ok = True
        if args.varied_sources:
            print(
                "note: --varied-sources is a planning/packing worst case; its "
                "exact column is informational (the reference re-resolves the "
                "plan's own per-frame proxy)."
            )
        for count, plan_ms, pass_ms, readback_ms, matches, total, note in rows:
            exact = f"{matches}/{total}{note}"
            print(
                f"{count:>3} | {plan_ms:>7.1f} | {pass_ms:>7.2f} | "
                f"{readback_ms:>11.2f} | {exact}"
            )
            if matches != total:
                ok = False
            if count == 17 and pass_ms > BUDGET_MS_17:
                print(f"  over budget: N=17 pass took {pass_ms:.2f} ms (budget {BUDGET_MS_17:.0f} ms)")
                ok = False
        if rows and any(note for *_rest, note in rows):
            print(
                "note: a row marked (resampled) is an OFF-GRID band target: each "
                "cell was compared with its own reference resampled to the cell "
                "(a fractional resample is never byte-identical), with the "
                "tolerance printed above each row's table line."
            )
        if band_stage_findings:
            failing = {
                count: findings
                for count, findings in band_stage_findings.items()
                if findings
            }
            if failing:
                ok = False
                print("\nband stages that diverge from the filmstrip contract:")
                for count in sorted(failing):
                    for finding in failing[count]:
                        print(f"  N={count}: {finding}")
            else:
                print(
                    "\nband stages: every tile is the filmstrip thumbnail "
                    "(the size the cover plan and the proxy pick are keyed on) "
                    "at the band's target size"
                )
        return 0 if ok else 1
    except _PreconditionError as exc:
        print(f"\nprecondition not met: {exc}", file=sys.stderr)
        return 1
    except _NoDisplayError as exc:
        print(f"\nneeds a display: {exc}", file=sys.stderr)
        print(
            "Run this on a real (or Xvfb) display — a batched QRhi pass cannot be "
            "measured under QT_QPA_PLATFORM=offscreen. Use --cpu-only for the "
            "planning/uniform half.",
            file=sys.stderr,
        )
        return 2
    except Exception:
        traceback.print_exc()
        if not args.cpu_only:
            # A display that cannot even probe an RHI makes every GPU failure
            # here a no-display outcome, not a bench mismatch (exit 2, never a
            # "the batched pass is wrong" verdict).
            try:
                preflight_rhi()
            except _NoDisplayError as no_display:
                print(f"\nneeds a display: {no_display}", file=sys.stderr)
                return 2
            except Exception:
                pass
        print("\nbench failed (see traceback above)", file=sys.stderr)
        return 1
    finally:
        if registry is not None and previous_tab is not None:
            try:
                registry._active_session_type = previous_tab
            except Exception:
                pass
        shutil.rmtree(workdir, ignore_errors=True)


def _cols_for(count: int) -> int:
    cols, _rows, _w, _h = plan_tile_grid(count, 1, 1)
    return cols


if __name__ == "__main__":
    sys.exit(main())
