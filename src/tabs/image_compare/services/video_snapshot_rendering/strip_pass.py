"""Strip window GPU half: one ``beginPass``/``endPass`` over N tiles.

Phase 1 of ``plan-video-strip-atlas-2026-10-30``. This is the proven phase-0
bench pass (``src/devtools/bench_strip_batch.py``) extracted as production
code: per-tile viewport + scissor (the ``COMPOSITED_SCISSOR`` Y rule for a
bottom-up/never-shown target), per-tile uniform block, per-tile
texture-array layer pair from ``TileTextureService``, the base array draw and
then the per-tile divider — no readback, no per-tile offscreen target, no
QImage on the GPU path beyond the upload copy, no per-tile worker.

``render_window`` (in ``strip_window``) is the public entry; it guards the
window generation and delegates here. The caller owns the command buffer: a
``QRhiWidget``'s ``render(command_buffer)`` is the normal source, because a
QRhi pass cannot be opened without one.

Resources are per RHI object and survive across windows (buffers and SRBs are
recreated only when the tile count changes, the array only when the layer
geometry/capacity changes); ``release_window_resources`` mirrors a widget's
``releaseResources``. One window superseding another does not tear anything
down — the next window uploads into the same array.
"""

# Audit-Meta: pattern=state-machine size=exempt reason="one strip GPU-resource lifecycle (samplers/array/SRBs/per-tile buffers/divider shell/upload) against one target; the upload guard, the capacity sink and the mip deferral share the array state, so splitting threads that state through every helper"

from __future__ import annotations

from pathlib import Path

from tabs.image_compare.services.video_snapshot_rendering.strip_window import (
    UNIFORM_STRIDE,
    StripWindow,
    StripWindowError,
    pack_tile_divider,
    uniform_block_size,
    wants_divider,
)

_TAB_ROOT = Path(__file__).resolve().parents[2]
_SHADER_DIR = _TAB_ROOT / "canvas" / "shaders"
_DIVIDER_SHADER_DIR = _TAB_ROOT / "canvas" / "features" / "divider" / "shaders"


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _load_ic_shader(name: str):
    """The tab's own compiled shader (base_array.vert/.frag) — no copy."""
    from PySide6.QtGui import QShader

    shader = QShader.fromSerialized((_SHADER_DIR / name).read_bytes())
    if not shader.isValid():
        raise RuntimeError(f"Invalid compiled shader: {name}")
    return shader


def _qimage_from_pil(image):
    """RGBA PIL -> ``QImage`` (one copy; the upload description keeps it)."""
    from PySide6.QtGui import QImage

    rgba = image.convert("RGBA")
    data = rgba.tobytes("raw", "RGBA")
    qimage = QImage(data, rgba.width, rgba.height, QImage.Format.Format_RGBA8888)
    return qimage.copy()


def pad_uniform_block(block: bytes, stride: int = UNIFORM_STRIDE) -> bytes:
    """Pad one packed uniform block to the dynamic-buffer stride."""
    if len(block) > stride:
        raise StripWindowError(
            f"uniform block {len(block)}B exceeds stride {stride}B"
        )
    return bytes(block) + b"\x00" * (stride - len(block))


def scaled_tile_rects(window: StripWindow, width: int, height: int):
    """Per-tile ``(x, y, w, h)`` in DEVICE px, top-down, edge snapped.

    ``framebuffer_rects`` is this plus the framebuffer's Y rule; a readback grab
    is top-down, so a harness slicing a grab uses this one (one geometry rule,
    never a second rounding).

    Snap the EDGES, never the width: rounding ``x * scale`` and ``w * scale``
    independently makes neighbours overlap at a fractional scale (the live
    band: a 1280x90 grid into 1020x72 gives cell 1 [0,128), cell 2 starting at
    128 with width 128, cell 3 at 255 — inside cell 2), so a cell's right edge
    column was painted by the NEXT tile's content. Edge snapping makes adjacent
    cells share an edge exactly: no overlap, no gap, and the last cell ends on
    the target.
    """
    fb_w = max(1, int(width))
    fb_h = max(1, int(height))
    scale_x = fb_w / float(max(1, int(window.target_w)))
    scale_y = fb_h / float(max(1, int(window.target_h)))
    rects = []
    for (x, y, w, h) in window.rects():
        left = int(round(x * scale_x))
        top = int(round(y * scale_y))
        right = int(round((x + w) * scale_x))
        bottom = int(round((y + h) * scale_y))
        rects.append((left, top, max(1, right - left), max(1, bottom - top)))
    return rects


def framebuffer_rects(
    window: StripWindow, target, *, rhi, never_shown: bool = False
):
    """Per-tile ``(x, y, w, h)`` in FRAMEBUFFER pixels, Y-flipped when needed.

    The rects are the window's logical tile rects scaled by the render target's
    own pixel size (never by a widget's logical size — a viewport sized in
    logical px paints into 1/DPR of the framebuffer per axis). A widget that is
    never shown (``WA_DontShowOnScreen``, the bench/export canvases) skips Qt's
    compositing step, so its framebuffer is bottom-up and a top-down rect must
    be mirrored before it reaches QRhi: the engine's ``COMPOSITED_SCISSOR`` rule,
    applied to viewport AND scissor (they live in one framebuffer space). The
    wrong answer does not blank the target — it silently draws each row into
    the mirrored row, which phase 0 mistook for a layer failure.
    """
    from engine.gpu.rt_y_orientation import composited_scissor_needs_y_flip

    pixel_size = target.pixelSize()
    fb_w = max(1, int(pixel_size.width()))
    fb_h = max(1, int(pixel_size.height()))
    rects = scaled_tile_rects(window, fb_w, fb_h)
    if composited_scissor_needs_y_flip(rhi, never_shown=never_shown):
        rects = [(x, fb_h - (y + h), w, h) for (x, y, w, h) in rects]
    return rects
    if composited_scissor_needs_y_flip(rhi, never_shown=never_shown):
        rects = [(x, fb_h - (y + h), w, h) for (x, y, w, h) in rects]
    return rects


# ---------------------------------------------------------------------------
# Update batching
# ---------------------------------------------------------------------------


class _Updates:
    """One resource-update cursor, flushed per tile when a command buffer exists.

    The proven shape: separate batches per tile, because a single batch
    carrying every per-tile buffer update loses some of them on the GL
    backend (those tiles draw nothing). Without a command buffer nothing can
    be flushed at all — the pass then refuses to record (see
    ``render_strip_window``).
    """

    def __init__(self, rhi, command_buffer) -> None:
        self._rhi = rhi
        self._command_buffer = command_buffer
        self.batch = rhi.nextResourceUpdateBatch()

    def submit(self) -> None:
        if self._command_buffer is None:
            return
        self._command_buffer.resourceUpdate(self.batch)
        self.batch = self._rhi.nextResourceUpdateBatch()


# ---------------------------------------------------------------------------
# Per-RHI resources
# ---------------------------------------------------------------------------

_PASSES: dict[int, "StripPassResources"] = {}


def _pass_for(rhi) -> "StripPassResources":
    key = id(rhi)
    resources = _PASSES.get(key)
    if resources is None or resources.rhi is not rhi:
        resources = StripPassResources(rhi)
        _PASSES[key] = resources
    return resources


def release_window_resources(rhi=None) -> None:
    """Destroy the strip pass resources for ``rhi`` (all of them when None)."""
    if rhi is None:
        resources = list(_PASSES.values())
        _PASSES.clear()
    else:
        entry = _PASSES.pop(id(rhi), None)
        resources = [entry] if entry is not None else []
    for entry in resources:
        entry.release()


class StripPassResources:
    """Persistent QRhi state for the strip window pass on one RHI object."""

    def __init__(self, rhi) -> None:
        self.rhi = rhi
        self._samplers: dict = {}
        self._array = None
        self._array_layer_px = 0
        self._array_capacity = 0
        self._tile_count = 0
        self._vertex_buffer = None
        self._vertex_bytes: bytes | None = None
        self._uniform_buffers: list = []
        self._instance_buffers: list = []
        self._srbs: list = []
        self._uploaded_generation = -1
        self._upload_qimages: list = []
        self._divider = None
        self._divider_target = None
        self._divider_staged_generation = -1
        self._divider_slots: dict[int, int] = {}
        self._target = None

    # -- lifecycle -----------------------------------------------------
    def _ensure_samplers(self) -> None:
        from PySide6.QtGui import QRhiSampler

        if self._samplers:
            return
        for name, filter_mode, mip_mode in (
            ("nearest", QRhiSampler.Filter.Nearest, QRhiSampler.Filter.None_),
            ("linear", QRhiSampler.Filter.Linear, QRhiSampler.Filter.Linear),
        ):
            sampler = self.rhi.newSampler(
                filter_mode,
                filter_mode,
                mip_mode,
                QRhiSampler.AddressMode.ClampToEdge,
                QRhiSampler.AddressMode.ClampToEdge,
            )
            if not sampler.create():
                raise StripWindowError(f"strip: failed to create {name} sampler")
            self._samplers[name] = sampler

    def _ensure_array(self, window: StripWindow) -> None:
        from tabs.image_compare.canvas.rhi_renderer.array_resources import (
            ArrayResources,
        )

        needs_new = (
            self._array is None
            or self._array_layer_px != int(window.layer_px)
            or self._array_capacity != int(window.layer_count)
        )
        if not needs_new:
            return
        if self._array is not None:
            self._array.release()
        self._array = None
        # SRBs bind the array texture, so they die with it.
        self._srbs = []
        self._array = ArrayResources(
            rhi_getter=lambda: self.rhi,
            sampler_getter=lambda name: self._samplers[name],
            load_shader=_load_ic_shader,
            layer_px=int(window.layer_px),
            array_capacity=int(window.layer_count),
            name_prefix="strip-window",
        )
        # The allocator that handed out the layers must follow a capacity the
        # driver refuses (ArrayResources halves it on a failed probe), or the
        # plan keeps sampling layers this array cannot address
        # (tests/contracts/test_capacity_agreement.py).
        service = window.tile_service
        if service is not None:
            self._array.set_capacity_sink(
                lambda capacity: service.set_max_array_size(capacity)
            )
        self._array._ensure_tile_array(0)
        self._array_layer_px = int(window.layer_px)
        self._array_capacity = int(window.layer_count)

    def _ensure_vertex_buffer(self) -> None:
        from PySide6.QtGui import QRhiBuffer
        from tabs.image_compare.canvas.rhi_renderer.resources import _VERTICES

        if self._vertex_buffer is not None:
            return
        buffer = self.rhi.newBuffer(
            QRhiBuffer.Type.Immutable,
            QRhiBuffer.UsageFlag.VertexBuffer,
            len(_VERTICES),
        )
        if not buffer.create():
            raise StripWindowError("strip: failed to create quad vertex buffer")
        self._vertex_buffer = buffer
        self._vertex_bytes = _VERTICES

    def _ensure_tile_buffers(self, window: StripWindow) -> None:
        from PySide6.QtGui import QRhiBuffer
        from engine.gpu.array_draw import ARRAY_INSTANCE_STRIDE

        count = len(window.tiles)
        if self._tile_count == count and len(self._srbs) == count:
            return
        for buffer in (*self._uniform_buffers, *self._instance_buffers):
            try:
                buffer.destroy()
            except RuntimeError:
                pass
        self._uniform_buffers = []
        self._instance_buffers = []
        self._srbs = []
        for index in range(count):
            uniform_buffer = self.rhi.newBuffer(
                QRhiBuffer.Type.Dynamic,
                QRhiBuffer.UsageFlag.UniformBuffer,
                UNIFORM_STRIDE,
            )
            if not uniform_buffer.create():
                raise StripWindowError(
                    f"strip: failed to create uniform buffer {index}"
                )
            self._uniform_buffers.append(uniform_buffer)
            instance_buffer = self.rhi.newBuffer(
                QRhiBuffer.Type.Dynamic,
                QRhiBuffer.UsageFlag.VertexBuffer,
                ARRAY_INSTANCE_STRIDE,
            )
            if not instance_buffer.create():
                raise StripWindowError(
                    f"strip: failed to create instance buffer {index}"
                )
            self._instance_buffers.append(instance_buffer)
        self._tile_count = count

    def _ensure_srbs(self, window: StripWindow) -> None:
        from PySide6.QtGui import QRhiShaderResourceBinding

        stages = (
            QRhiShaderResourceBinding.StageFlag.VertexStage
            | QRhiShaderResourceBinding.StageFlag.FragmentStage
        )
        fragment = QRhiShaderResourceBinding.StageFlag.FragmentStage
        self._array._ensure_tile_array(0)
        tile_array = self._array.tile_arrays[0]
        sampler = self._samplers["linear"]
        block = uniform_block_size()
        srbs = []
        for index in range(len(window.tiles)):
            srb = self.rhi.newShaderResourceBindings()
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
                raise StripWindowError(f"strip: failed to create SRB {index}")
            srbs.append(srb)
        self._srbs = srbs

    # -- per-frame work ------------------------------------------------
    def _upload(self, cursor: _Updates, window: StripWindow) -> None:
        if self._uploaded_generation == window.generation:
            return
        cursor.batch.uploadStaticBuffer(self._vertex_buffer, self._vertex_bytes)
        cursor.submit()
        # The upload descriptions hold their QImage by reference; a collected
        # source leaves the GPU upload reading freed pixels (observed as
        # blank/garbage tiles in phase 0). Keep them alive until the window
        # changes.
        self._upload_qimages = []
        dirty_layers: dict[int, set[int]] = {}
        for tile in window.tiles:
            for side, image in ((1, tile.image1), (2, tile.image2)):
                qimage = _qimage_from_pil(image)
                self._upload_qimages.append(qimage)
                self._array.upload_tile_to_array(
                    window.tile_service,
                    window.tile_source_key(tile, side),
                    (0, 0),
                    qimage,
                    cursor.batch,
                    dirty_layers,
                )
        cursor.submit()
        # ``upload_tile_to_array`` defers mip generation to its caller (it must
        # not share a batch with the upload it reads). One window uploads once,
        # so the whole bound array is regenerated once here.
        if 0 in dirty_layers:
            self._array._ensure_tile_array(0)
            cursor.batch.generateMips(self._array.tile_arrays[0])
            cursor.submit()
        self._uploaded_generation = window.generation

    def _stage_dividers(self, cursor: _Updates, window: StripWindow) -> None:
        if not any(wants_divider(tile) for tile in window.tiles):
            self._divider_slots = {}
            return
        if self._divider is not None and self._divider_target is not self._target:
            # The overlay shell owns GPU objects bound to the previous target;
            # re-mount on the new one and repack with its clip matrix.
            try:
                self._divider.teardown()
            except Exception:
                pass
            self._divider = None
            self._divider_staged_generation = -1
        if self._divider is None:
            from engine.overlay import DIVIDER_UNIFORM_SIZE, OverlayShell

            shell = OverlayShell(DIVIDER_UNIFORM_SIZE)
            shell.mount(self.rhi, self._target, _DIVIDER_SHADER_DIR, "divider")
            self._divider = shell
            self._divider_target = self._target
            self._divider_staged_generation = -1
            for tile in window.tiles:
                tile.divider = b""
        if self._divider_staged_generation == window.generation:
            return
        blocks = []
        for tile in window.tiles:
            if not tile.divider:
                tile.divider = pack_tile_divider(tile, self._divider.clip_matrix())
            blocks.append(tile.divider)
        present = [index for index, block in enumerate(blocks) if block]
        self._divider_slots = {tile: slot for slot, tile in enumerate(present)}
        self._divider.stage_packed(cursor.batch, [block for block in blocks if block])
        cursor.submit()
        self._divider_staged_generation = window.generation

    def _stage_tile_uniforms(self, cursor: _Updates, window: StripWindow) -> None:
        for index, tile in enumerate(window.tiles):
            cursor.batch.updateDynamicBuffer(
                self._uniform_buffers[index], 0, pad_uniform_block(tile.uniforms)
            )
            cursor.batch.updateDynamicBuffer(
                self._instance_buffers[index], 0, tile.instance
            )
            cursor.submit()

    def _record(
        self, command_buffer, window: StripWindow, target, never_shown: bool
    ) -> None:
        from PySide6.QtGui import QRhiScissor, QRhiViewport

        # Device pixels come from the target itself, never from a logical size:
        # a viewport sized in logical px paints into 1/DPR of the framebuffer
        # per axis (the DPR failure mode this repo has shipped once already).
        pixel_size = target.pixelSize()
        fb_width = max(1, int(pixel_size.width()))
        fb_height = max(1, int(pixel_size.height()))
        rects = framebuffer_rects(
            window, target, rhi=self.rhi, never_shown=never_shown
        )
        command_buffer.setGraphicsPipeline(self._array.array_pipeline)
        for index, (left_fb, top_fb, width_fb, height_fb) in enumerate(rects):
            if width_fb <= 0 or height_fb <= 0:
                continue
            if left_fb >= fb_width or top_fb >= fb_height:
                continue
            command_buffer.setViewport(
                QRhiViewport(
                    float(left_fb), float(top_fb), float(width_fb), float(height_fb)
                )
            )
            # Explicit per-tile scissor: a batched pass may not assume the
            # target's default scissor covers more than the surface Qt painted
            # last (observed: every tile past the top-left quadrant came back
            # clear with viewport alone).
            command_buffer.setScissor(
                QRhiScissor(int(left_fb), int(top_fb), int(width_fb), int(height_fb))
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

        # Divider overlay, per tile, in the SAME open pass and with that tile's
        # viewport/scissor: the reference tile is its own target and the
        # divider shader works in target-relative px, so the tile viewport IS
        # the reference's full-target viewport.
        if self._divider is None or self._divider.pipeline is None:
            return
        for index, (left_fb, top_fb, width_fb, height_fb) in enumerate(rects):
            slot = self._divider_slots.get(index)
            if slot is None:
                continue
            command_buffer.setGraphicsPipeline(self._divider.pipeline)
            command_buffer.setViewport(
                QRhiViewport(
                    float(left_fb), float(top_fb), float(width_fb), float(height_fb)
                )
            )
            command_buffer.setScissor(
                QRhiScissor(int(left_fb), int(top_fb), int(width_fb), int(height_fb))
            )
            command_buffer.setVertexInput(0, [(self._divider.vertex_buffer, 0)])
            command_buffer.setShaderResources(self._divider.slot_bindings[slot])
            command_buffer.draw(4)

    def render(
        self,
        target,
        window: StripWindow,
        tile_service,
        command_buffer,
        never_shown: bool,
    ) -> None:
        from PySide6.QtGui import QColor, QRhiDepthStencilClearValue

        self._target = target
        self._ensure_samplers()
        self._ensure_array(window)
        self._ensure_vertex_buffer()
        self._ensure_tile_buffers(window)
        self._array.ensure_array_pipeline(target)
        if len(self._srbs) != len(window.tiles):
            self._ensure_srbs(window)
        self._array._ensure_tile_array(0)

        cursor = _Updates(self.rhi, command_buffer)
        self._upload(cursor, window)
        self._stage_dividers(cursor, window)
        self._stage_tile_uniforms(cursor, window)

        command_buffer.beginPass(
            target,
            QColor(0, 0, 0, 0),
            QRhiDepthStencilClearValue(1.0, 0),
            cursor.batch,
        )
        try:
            self._record(command_buffer, window, target, never_shown)
        finally:
            command_buffer.endPass()

    # -- teardown ------------------------------------------------------
    def release(self) -> None:
        if self._divider is not None:
            try:
                self._divider.teardown()
            except Exception:
                pass
        self._divider = None
        self._divider_target = None
        if self._array is not None:
            try:
                self._array.release()
            except Exception:
                pass
        self._array = None
        self._array_capacity = 0
        self._array_layer_px = 0
        for buffer in (
            self._vertex_buffer,
            *self._uniform_buffers,
            *self._instance_buffers,
        ):
            if buffer is not None:
                try:
                    buffer.destroy()
                except RuntimeError:
                    pass
        self._vertex_buffer = None
        self._vertex_bytes = None
        self._uniform_buffers = []
        self._instance_buffers = []
        self._srbs = []
        self._tile_count = 0
        self._uploaded_generation = -1
        self._upload_qimages = []
        for sampler in self._samplers.values():
            try:
                sampler.destroy()
            except RuntimeError:
                pass
        self._samplers = {}
        self._target = None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def render_strip_window(
    target,
    window: StripWindow,
    *,
    rhi,
    tile_service=None,
    command_buffer=None,
    never_shown: bool = False,
) -> None:
    """Record the window's single pass into ``target``.

    ``command_buffer`` is required: a QRhi pass cannot be opened without one,
    so the caller's ``render(command_buffer)`` supplies it.
    """
    if rhi is None:
        raise StripWindowError("render_window needs a live QRhi")
    if target is None:
        raise StripWindowError("render_window needs a render target")
    if command_buffer is None:
        raise StripWindowError(
            "render_window needs the command buffer of the pass that owns the "
            "target (a QRhiWidget's render(command_buffer))"
        )
    service = tile_service if tile_service is not None else window.tile_service
    if service is None:
        raise StripWindowError("render_window needs the window's TileTextureService")
    if window.tile_service is not None and service is not window.tile_service:
        raise StripWindowError(
            "render_window got a different TileTextureService than the window "
            "was packed against: layers would be read from the wrong allocator"
        )
    resources = _pass_for(rhi)
    resources.render(target, window, service, command_buffer, never_shown)


__all__ = [
    "StripPassResources",
    "framebuffer_rects",
    "pad_uniform_block",
    "release_window_resources",
    "render_strip_window",
    "scaled_tile_rects",
]
