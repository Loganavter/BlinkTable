"""Per-slot plain-texture pipeline/SRB resources for Multi Compare's
base-image pass.

Thin tab-side consumer of ``engine.gpu.plain_resources.
PlainTextureResourcesBase`` (Phase D4 mc-plain decision): the generic
lifetime half -- per-key ``QRhiTexture`` dict, dynamic vertex/uniform
buffers, upload, destroy, release -- lives in the engine; this module
keeps only what is shader-specific to this tab -- the slot graphics
pipeline, the per-(slot-index, tile-key) SRB cache bound to this tab's
own binding layout, and the slot-id key selection. Tile *residency
decisions* stay in ``BaseImagesPass._realize_tile_residency``, which calls
into whichever of this or ``array_resources.py`` owns the tile actually
being uploaded.
"""

from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtGui import (
    QRhi,
    QRhiBuffer,
    QRhiGraphicsPipeline,
    QRhiRenderPassDescriptor,
    QRhiShaderResourceBindings,
    QRhiShaderResourceBinding,
    QRhiShaderStage,
    QRhiTexture,
)

from engine.cache.lod import LevelKey
from engine.gpu.plain_resources import PlainTextureResourcesBase
from tabs.multi_compare.scene.resources import (
    FULLSCREEN_VERTICES,
    SLOT_UNIFORM_SIZE,
    load_shader,
    vertex_input_layout,
)


def _tile_key_slot_id(tile_key: object) -> object:
    if isinstance(tile_key, LevelKey):
        return _tile_key_slot_id(tile_key.base)
    if isinstance(tile_key, tuple):
        return _tile_key_slot_id(tile_key[0])
    return tile_key


def _plain_sampler(renderer):
    """Sampler for a non-array tile texture: NO mip filtering.

    Plain tiles are uploaded as a single-level ``QRhiTexture``; sampling one
    through the array path's mipmapped sampler is mipmap-INCOMPLETE and
    returns black. Measured 2026-11-18 on a leased instance: every slot whose
    grid is 1x1 (any image at or below ``SLOT_LIVE_TILE_EXTENT``, e.g. a
    512x512 pair) painted an opaque black square, while a 2048x2048 slot on
    the array path painted correctly — the same frame, the same source
    QImage (``Format_RGBA8888``, correct pixel). Forcing the array path made
    the 512x512 slot paint, which pinned the plain draw as the broken one.
    """
    return getattr(renderer, "plain_sampler", None) or renderer.sampler


class SlotResources(PlainTextureResourcesBase):
    """Tab-side plain-path consumer: engine base owns the textures and
    dynamic buffers, this subclass adds the slot graphics pipeline and
    the tile SRB cache -- one instance per ``BaseImagesPass``."""

    def __init__(self, *, uniform_size: int) -> None:
        super().__init__(uniform_stride=uniform_size)
        self.pipeline: QRhiGraphicsPipeline | None = None
        self._render_pass_descriptor: QRhiRenderPassDescriptor | None = None
        self._pipeline_sample_count: int | None = None
        self._tile_srbs: dict[tuple[int, object], tuple[QRhiShaderResourceBindings, QRhiTexture]] = {}

    def release(self) -> None:
        if self.pipeline is not None:
            try:
                self.pipeline.destroy()
            except RuntimeError:
                pass
        for srb, _texture in self._tile_srbs.values():
            if srb is not None:
                try:
                    srb.destroy()
                except RuntimeError:
                    pass
        super().release()
        self.pipeline = None
        self._render_pass_descriptor = None
        self._pipeline_sample_count = None
        self._tile_srbs = {}

    def ensure_pipeline(self, renderer, target) -> bool:
        """Recreate the graphics pipeline when the swapchain RT descriptor
        changes. Mirrors image_compare's canvas-resource pipeline refresh:
        a Qt.Popup parented to the QRhiWidget can restack the color buffer;
        keeping a stale ``renderPassDescriptor``/``sampleCount`` produces a
        present that looks like a zoom nudge even when uniforms stay
        identical."""
        if target is None:
            return False
        descriptor = target.renderPassDescriptor()
        sample_count = int(target.sampleCount())
        if (
            self.pipeline is not None
            and descriptor is self._render_pass_descriptor
            and sample_count == self._pipeline_sample_count
        ):
            return False
        if self.pipeline is not None:
            try:
                self.pipeline.destroy()
            except RuntimeError:
                pass
            self.pipeline = None

        pipeline = renderer.rhi.newGraphicsPipeline()
        pipeline.setShaderStages(
            [
                QRhiShaderStage(
                    QRhiShaderStage.Type.Vertex, load_shader("multi_compare.vert.qsb")
                ),
                QRhiShaderStage(
                    QRhiShaderStage.Type.Fragment, load_shader("multi_compare.frag.qsb")
                ),
            ]
        )
        pipeline.setTopology(QRhiGraphicsPipeline.Topology.TriangleStrip)
        pipeline.setSampleCount(sample_count)
        pipeline.setRenderPassDescriptor(descriptor)
        blend = QRhiGraphicsPipeline.TargetBlend()
        blend.enable = True
        blend.srcColor = QRhiGraphicsPipeline.BlendFactor.SrcAlpha  # type: ignore[assignment]  # PySide6 stub types BlendFactor fields as int
        blend.dstColor = QRhiGraphicsPipeline.BlendFactor.OneMinusSrcAlpha  # type: ignore[assignment]
        blend.srcAlpha = QRhiGraphicsPipeline.BlendFactor.One  # type: ignore[assignment]
        blend.dstAlpha = QRhiGraphicsPipeline.BlendFactor.OneMinusSrcAlpha  # type: ignore[assignment]
        pipeline.setTargetBlends([blend])
        first_srb = self._build_slot_srb(renderer, renderer.placeholder, None)
        pipeline.setShaderResourceBindings(first_srb)
        pipeline.setVertexInputLayout(vertex_input_layout())
        if not pipeline.create():
            raise RuntimeError("Failed to create multi_compare slot pipeline")
        try:
            first_srb.destroy()
        except RuntimeError:
            pass
        self.pipeline = pipeline
        self._render_pass_descriptor = descriptor
        self._pipeline_sample_count = sample_count
        return True

    def _build_slot_srb(self, renderer, texture, uniform):
        srb = renderer.rhi.newShaderResourceBindings()
        stages = (
            QRhiShaderResourceBinding.StageFlag.VertexStage
            | QRhiShaderResourceBinding.StageFlag.FragmentStage
        )
        fragment = QRhiShaderResourceBinding.StageFlag.FragmentStage
        bindings = []
        if uniform is not None:
            bindings.append(
                QRhiShaderResourceBinding.uniformBufferWithDynamicOffset(
                    0, stages, uniform, SLOT_UNIFORM_SIZE
                )
            )
        else:
            placeholder_uniform = renderer.rhi.newBuffer(
                QRhiBuffer.Type.Dynamic,
                QRhiBuffer.UsageFlag.UniformBuffer,
                self._slot_uniform_stride,
            )
            placeholder_uniform.create()
            self.slot_uniform_buffers.append(placeholder_uniform)
            self._slot_uniform_capacity.append(1)
            bindings.append(
                QRhiShaderResourceBinding.uniformBufferWithDynamicOffset(
                    0, stages, placeholder_uniform, SLOT_UNIFORM_SIZE
                )
            )
        bindings.append(
            QRhiShaderResourceBinding.sampledTexture(
                1, fragment, texture, renderer.sampler
            )
        )
        srb.setBindings(bindings)
        if not srb.create():
            raise RuntimeError("Failed to create multi_compare slot SRB")
        return srb

    def ensure_slot_resources(self, renderer, count: int) -> None:
        self.ensure_buffer_count(
            renderer.rhi, count, vertex_bytes=len(FULLSCREEN_VERTICES)
        )

    def ensure_slot_uniform_capacity(self, renderer, index: int, slot_count: int) -> None:
        """Grow ``slot_uniform_buffers[index]`` to hold ``slot_count``
        per-tile uniform blocks at stride-spaced offsets, mirroring
        image_compare's ``RhiResources.ensure_uniform_capacity``. Lets every
        tile item in a multi-tile slot get its own uniform slot written into
        the single pre-pass ``updates`` batch instead of a mid-pass
        ``resourceUpdate()`` per draw call -- the pattern that caused the
        tile desync/blank-tile bug in image_compare (see docs/dev/rendering/
        investigations/multitile-uniform-desync.md). Must be called before
        beginPass: growing recreates the buffer object, which invalidates
        every cached tile SRB bound to it."""
        old_buffer = self.ensure_uniform_capacity(renderer.rhi, index, slot_count)
        if old_buffer is None:
            return
        self._purge_tile_srbs_for_index(index)
        try:
            old_buffer.destroy()
        except RuntimeError:
            pass

    def ensure_tile_srb(self, renderer, index: int, tile_key: object, texture):
        cache_key = (index, tile_key)
        cached = self._tile_srbs.get(cache_key)
        if cached is not None and cached[1] is texture:
            return cached[0]
        if cached is not None:
            try:
                cached[0].destroy()
            except RuntimeError:
                pass
        stages = (
            QRhiShaderResourceBinding.StageFlag.VertexStage
            | QRhiShaderResourceBinding.StageFlag.FragmentStage
        )
        fragment = QRhiShaderResourceBinding.StageFlag.FragmentStage
        srb = renderer.rhi.newShaderResourceBindings()
        srb.setBindings(
            [
                QRhiShaderResourceBinding.uniformBufferWithDynamicOffset(
                    0, stages, self.slot_uniform_buffers[index], SLOT_UNIFORM_SIZE
                ),
                QRhiShaderResourceBinding.sampledTexture(
                    1, fragment, texture, _plain_sampler(renderer)
                ),
            ]
        )
        srb.create()
        self._tile_srbs[cache_key] = (srb, texture)
        return srb

    def upload_tile(self, renderer, tile_key: object, image, updates) -> None:
        def _allocate(size: tuple[int, int]):
            tex = renderer.rhi.newTexture(QRhiTexture.Format.RGBA8, QSize(*size))
            tex.create()
            return tex

        self.upload_texture(tile_key, image, updates, allocate=_allocate)

    def destroy_slot_textures(self, sid: int, *, keep: frozenset | set) -> None:
        stale = [
            key
            for key in self.slot_textures
            if _tile_key_slot_id(key) == sid and key not in keep
        ]
        self.discard_textures(stale)

    def _purge_tile_srbs_for_index(self, index: int) -> None:
        stale = [cache_key for cache_key in self._tile_srbs if cache_key[0] == index]
        for cache_key in stale:
            srb, _texture = self._tile_srbs.pop(cache_key)
            try:
                srb.destroy()
            except RuntimeError:
                pass

    def purge_tile_srbs_for_slot(self, sid: int) -> None:
        stale = [
            cache_key
            for cache_key in self._tile_srbs
            if _tile_key_slot_id(cache_key[1]) == sid
        ]
        for cache_key in stale:
            srb, _texture = self._tile_srbs.pop(cache_key)
            try:
                srb.destroy()
            except RuntimeError:
                pass

    def purge_tile_srb_entry(self, tile_key: object) -> None:
        stale = [cache_key for cache_key in self._tile_srbs if cache_key[1] == tile_key]
        for cache_key in stale:
            srb, _texture = self._tile_srbs.pop(cache_key)
            try:
                srb.destroy()
            except RuntimeError:
                pass
