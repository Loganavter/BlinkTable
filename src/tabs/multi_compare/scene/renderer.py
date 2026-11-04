# Audit-Meta: pattern=thin-owner reason="MultiCompareRhiRenderer thin owner delegates to scene/passes/* + canvas/features/* per CODE_PATTERNS — frame sequencing only"
"""QRhi renderer orchestration for the multi-compare scene.

The widget owns interaction and state.  The renderer owns QRhi resources,
texture uploads, per-frame projection, and command recording.  This mirrors the
main image-compare split where ``RhiCanvasRenderer`` is separate from the
canvas widget and feature passes.
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import QSize
from PySide6.QtGui import (
    QRhi,
    QImage,
    QRhiDepthStencilClearValue,
    QRhiSampler,
    QRhiTexture,
)

from engine.gpu.rt_y_orientation import composited_clip_correction
from engine.store.tiled_pixel_store import TiledPixelStore
from engine.gpu.glass_panel import GlassPanelRenderer
from engine.cache.tile_constants import MIPS_CASCADE_TIME_BUDGET_MS
from engine.cache.tile_texture_service import TileTextureService
from shared.debug_flags import env_value as _env_value
from engine.frame.context import (
    BudgetStats,
    FrameContext,
    make_ids,
    snapshot_last_frame,
    viewport_hash,
)
from engine.frame.trace_export import maybe_dump_trace
from tabs.multi_compare.canvas.registry import registry
from tabs.multi_compare.first_frame_debug import mc_first_frame_debug
from tabs.multi_compare.scene.passes import RENDER_PASSES, BaseImagesPass
from tabs.multi_compare.scene.passes.base_images import _ARRAY_CAPACITY
from tabs.multi_compare.scene.projection import build_render_context
from tabs.multi_compare.scene.resources import SLOT_LIVE_TILE_EXTENT
from ui.canvas_infra.rhi.render_executor import iter_active_render_passes
from ui.canvas_infra.rhi.rhi_backend import query_max_texture_size
from core.log_names import APP_LOGGER_NAME

logger = logging.getLogger(APP_LOGGER_NAME)

# Debug-only bisection toggle (tile flip/shrink investigation): skips the
# manual per-layer mip-downsample cascade entirely, leaving every array
# layer's mip levels 1+ stale/uninitialized while level 0 still uploads
# normally. If the flip/shrink artifact disappears with this set, the bug is
# in the cascade (mip_cascade.py); if it persists, the cascade is innocent.
# Never set outside manual debugging -- coarser LOD levels will look wrong
# (stale or blank) by design when this is on.
_SKIP_MIP_CASCADE = (_env_value("IMGSLI_SKIP_MIP_CASCADE", "") or "").strip().lower() not in (
    "",
    "0",
    "false",
    "no",
    "off",
)


def _manifest_base_images_pass() -> BaseImagesPass:
    """A fresh per-renderer instance of the scene's base-image stage.

    ``RENDER_PASSES`` is the declaration (which pass class stages the base
    image, and its ``stack_role``/``visibility``); the instance itself is
    built here, per renderer, mirroring how feature passes come from
    ``registry().get_render_passes()`` and are instantiated per renderer.
    Returning the module-level manifest instance instead made the live
    canvas and the export renderer share one pass -- and with it one set of
    GPU resources and slot bookkeeping."""
    for pass_ in RENDER_PASSES:
        if isinstance(pass_, BaseImagesPass):
            return type(pass_)()
    raise RuntimeError("RENDER_PASSES must route a BaseImagesPass")


class MultiCompareRhiRenderer:
    """Persistent QRhi renderer for ``MultiCompareCanvasWidget``.

    ``image_pass`` (base image tiles) is routed via the scene's
    ``RENDER_PASSES`` manifest but instantiated per renderer (like the
    feature passes below) and still recorded first — mirrors how
    image_compare's own base-image draw is hardcoded ahead of its feature
    passes (A6: the base image is the frame's substrate — feature passes blend
    over it, so it must be recorded first and cannot be discovered out of
    order). ``feature_passes`` are
    discovered through the shared canvas feature registry, same as
    image_compare (``rhi_renderer/__init__.py``'s ``initialize()``).
    """

    def __init__(self, host) -> None:
        self.host = host
        self.rhi: QRhi | None = None
        self.target = None
        self.sampler: QRhiSampler | None = None
        self.placeholder: QRhiTexture | None = None
        # Same capacity BaseImagesPass sizes its SRB-bound array from: the
        # service must never hand out a layer `tile_arrays[0]` cannot address.
        self.tile_service = TileTextureService(max_array_size=_ARRAY_CAPACITY)
        # Manifest-routed (see _manifest_base_images_pass): a fresh instance
        # owned by this renderer -- the manifest declares the class, it is
        # never shared across renderers. release() resets this instance in
        # place via BaseImagesPass.release().
        self.image_pass = _manifest_base_images_pass()
        # Mirrors image_compare's RhiCanvasRenderer.glass_panel_renderer --
        # draws the blurred/tinted backdrop + text mask for every GlassHUD
        # (ZoomIndicator here) registered on `host.glass_panels`, see the
        # render_backdrops() call at the end of render() below.
        self.glass_panel_renderer = GlassPanelRenderer(name_prefix="multi_compare")
        self.feature_passes: list = []
        self.initialized = False
        # docs/dev/rendering/tile-array-atlas-plan.md Phase 9: mip-cascade
        # groups deferred past MIPS_CASCADE_TIME_BUDGET_MS by
        # generate_all_dirty_mips, carried forward and retried on the next
        # render() call (merged into that frame's own dirty_layers) --
        # mirrors image_compare's RhiCanvasRenderer._pending_dirty_layers.
        self._pending_dirty_layers: dict[int, set[int]] = {}
        # Frame-context ownership (mirrors image_compare's RhiCanvasRenderer):
        # one reused FrameContext per renderer, reset() per render(). Created
        # once -- release() re-runs __init__ for resource reset (see below),
        # which must reattach the existing context, never restart frame_ids
        # (reset() is the only frame_id bump; neither release() nor
        # initialize() resets). Explicit None check (not `or`): FrameContext
        # defines __len__, so an empty context is falsy and `or` would drop
        # it on every reinit.
        _existing_context = getattr(self, "frame_context", None)
        self.frame_context = (
            _existing_context if _existing_context is not None else FrameContext()
        )

    def has_slot_texture(self, slot_id: int) -> bool:
        return self.image_pass.has_slot_texture(slot_id)

    def slot_texture_source(self, slot_id: int):
        """Currently GPU-resident source for ``slot_id``, or ``None``.

        Lets callers (``canvas_widget._sync_textures``) detect a
        preview→full-res swap (``CompareSlot.image`` replaced in place by
        identity, not just presence) and re-queue the upload.
        """
        return self.image_pass.slot_pixel_sources.get(int(slot_id))

    def slot_texture_ids(self) -> list[int]:
        return self.image_pass.slot_texture_ids()

    def queue_upload(self, slot_id: int, source: TiledPixelStore) -> None:
        self.image_pass.queue_upload(slot_id, source)

    def queue_remove(self, slot_id: int) -> None:
        self.image_pass.queue_remove(slot_id)

    def initialize(self, command_buffer) -> None:
        new_rhi = self.host.rhi()
        target = self.host.renderTarget()
        if new_rhi is None or target is None:
            mc_first_frame_debug(
                self.host,
                "renderer.initialize ABORT: rhi=%s renderTarget=%s",
                new_rhi is not None, target is not None,
            )
            logger.warning(
                "[mc-renderer] initialize() aborted: rhi or renderTarget is None "
                "(widget not properly realized yet)"
            )
            return
        # Qt calls initialize() again on every plain resize (its own backing
        # render target needs reallocating), not only on a genuine context
        # loss/backend change — mirrors image_compare's identical guard, see
        # docs/dev/rendering/qrhi-gotchas.md
        # #canvas-resize-tears-down-the-whole-tile-array-on-every-resize.
        # Skipping the rebuild when `rhi` is unchanged also matters for
        # correctness here, not just cost: on a genuine identity change (e.g.
        # observed after a maximize->restore round trip) the old code path
        # rebuilt the tile array/sampler/placeholder against the new `rhi`
        # but left `image_pass.slot_pixel_sources` untouched, so
        # `_sync_textures()`'s change-detection (`slot_texture_source(sid) is
        # source`) saw "nothing changed" and never re-queued the uploads —
        # the array/slot textures stayed bound to the destroyed context and
        # the canvas rendered blank (background clear color only). Routing
        # a real identity change through `self.release()` (which resets this
        # renderer's own manifest-declared `image_pass` in place via
        # `BaseImagesPass.release()`, clearing that bookkeeping) makes the
        # post-rebuild `_sync_textures()` call below
        # see every currently-loaded slot as new and re-upload it.
        if new_rhi is self.rhi and self.initialized:
            self.target = target
            return
        self.release()
        self.rhi = new_rhi
        self.target = target
        # docs/dev/TILED_RENDERING_DESIGN.md Phase 2: fixed tile size
        # (SLOT_LIVE_TILE_EXTENT), clamped by the backend's real max texture
        # size as a defensive floor only.
        self.tile_service = TileTextureService(
            max_tile_extent=min(SLOT_LIVE_TILE_EXTENT, query_max_texture_size(new_rhi)),
            max_array_size=_ARRAY_CAPACITY,
        )

        # Mip-capable (docs/dev/rendering/tile-array-atlas-plan.md Phase 9):
        # the tile array's coarsest pyramid level is still routinely shown
        # well below 1:1 (e.g. a 20000px source's ~1024px-capped pyramid
        # floor rendered at a ~240px on-screen thumbnail), where a mip-less
        # bilinear sample aliases badly on high-frequency content. A texture
        # with only one real mip level (the non-array per-tile path's
        # textures) samples level 0 regardless of this filter, so reusing
        # one sampler everywhere is safe -- see generate_all_dirty_mips for
        # the array texture's own mip chain.
        self.sampler = new_rhi.newSampler(
            QRhiSampler.Filter.Linear,
            QRhiSampler.Filter.Linear,
            QRhiSampler.Filter.Linear,
            QRhiSampler.AddressMode.ClampToEdge,
            QRhiSampler.AddressMode.ClampToEdge,
        )
        self.sampler.create()

        self.placeholder = new_rhi.newTexture(QRhiTexture.Format.RGBA8, QSize(1, 1))
        self.placeholder.create()

        upload = new_rhi.nextResourceUpdateBatch()
        ph = QImage(1, 1, QImage.Format.Format_RGBA8888)
        ph.fill(0)
        upload.uploadTexture(self.placeholder, ph)
        self.image_pass.initialize(self.rhi, target)
        command_buffer.resourceUpdate(upload)

        self.glass_panel_renderer.initialize(self.rhi)

        passes = [type(render_pass)() for render_pass in registry().get_render_passes()]
        # Publish only on full success (mirrors image_compare's initialize):
        # a pass that raises mid-loop must not leave a non-empty
        # ``feature_passes`` behind, or the same-rhi early return above would
        # treat the half-initialized set as "done" and never retry. Release
        # just the passes whose initialize() returned; a failing cleanup never
        # masks the original exception.
        initialized: list = []
        try:
            for render_pass in passes:
                render_pass.initialize(self.rhi, target)
                initialized.append(render_pass)
        except BaseException:
            for done in initialized:
                try:
                    done.release()
                except Exception:
                    pass
            raise
        self.feature_passes = passes

        self.initialized = True
        self.host._sync_textures()

    def release(self) -> None:
        self.image_pass.release()
        self.glass_panel_renderer.release()
        for render_pass in self.feature_passes:
            render_pass.release()
        for res in (
            self.sampler,
            self.placeholder,
        ):
            if res is not None:
                try:
                    res.destroy()
                except RuntimeError:
                    pass
        host = self.host
        self.__init__(host)  # type: ignore[misc]  # resource-reset reinit

    def render(self, command_buffer) -> bool:
        # Frame-context is born here (mirrors image_compare's
        # RhiCanvasRenderer.render): reset the reused collector so every
        # frame starts empty, including the early-skip paths below. Records
        # below are additive (record-only, never instead).
        self.frame_context.reset()
        if not self.initialized or self.rhi is None:
            if not getattr(self, "_logged_not_initialized", False):
                self._logged_not_initialized = True
                mc_first_frame_debug(
                    self.host,
                    "renderer.render SKIPPED: not initialized yet "
                    "(initialized=%s rhi=%s)",
                    self.initialized, self.rhi is not None,
                )
                logger.warning(
                    "[mc-renderer] render() called before initialize() completed "
                    "(initialized=%s rhi=%s)",
                    self.initialized,
                    self.rhi is not None,
                )
            # Perf spans (Wave D): the prepare hop of the wheel-to-photon
            # chain also covers the early-skip paths (mirrors IC's
            # skip-path ic.prepare) — span/record only.
            with self.frame_context.span(
                "mc.prepare", detail=lambda: {"skipped": "not-initialized"}
            ):
                self.frame_context.record(
                    "prepare_frame",
                    "skipped",
                    "render skipped: renderer not initialized",
                    ids=make_ids(self.frame_context.frame_id, "no-init"),
                )
            snapshot_last_frame(self.frame_context)
            return False
        target = self.host.renderTarget()
        if target is None:
            if not getattr(self, "_logged_no_target", False):
                self._logged_no_target = True
                mc_first_frame_debug(
                    self.host, "renderer.render SKIPPED: renderTarget() is None"
                )
                logger.warning("[mc-renderer] render() aborted: renderTarget() is None")
            with self.frame_context.span(
                "mc.prepare", detail=lambda: {"skipped": "no-target"}
            ):
                self.frame_context.record(
                    "prepare_frame",
                    "skipped",
                    "render skipped: renderTarget() is None",
                    ids=make_ids(self.frame_context.frame_id, "no-target"),
                )
            snapshot_last_frame(self.frame_context)
            return False
        target_size = target.pixelSize()
        fb_w = float(target_size.width())
        fb_h = float(target_size.height())

        # Match image_compare: refresh feature pipelines if popup/swapchain
        # restacked the color buffer (stale renderPassDescriptor → wrong
        # present). The base pass ensures its own pipeline in prepare().
        for render_pass in self.feature_passes:
            ensure = getattr(render_pass, "_ensure_pipeline", None)
            if callable(ensure):
                ensure(target)

        # Perf spans (Wave D, plan-engine-perf-debugger-2026-09-20): the
        # prepare hop of the wheel-to-photon chain (span/record only —
        # every call below is unchanged; mirrors IC's ic.prepare).
        with self.frame_context.span(
            "mc.prepare",
            detail=lambda: {"framebuffer": [fb_w, fb_h]},
        ):
            updates = self.rhi.nextResourceUpdateBatch()
            self.host._sync_textures()
            self.image_pass.apply_pending_texture_ops(self, updates)

            composition = self.host._active_composition
            ctx = build_render_context(
                composition=composition,
                framebuffer_size=(fb_w, fb_h),
                clip_matrix=composited_clip_correction(self.rhi),
                available_slot_ids=set(self.image_pass.slot_texture_ids()),
                widget=self.host,
            )
            # Frame bus: prepare fact (record only, never instead). Canvas size
            # comes from the composition when one is resolved, else None (which
            # viewport_hash renders as "none" -- same as IC early-skip atoms).
            try:
                _canvas_size: tuple[float, float] | None = (
                    float(composition.canvas_w),
                    float(composition.canvas_h),
                )
            except (AttributeError, TypeError, ValueError):
                _canvas_size = None
            self.frame_context.record(
                "prepare_frame",
                "ran",
                f"prepare_frame scale={ctx.scale:.6f}"
                f" layers={len(ctx.projected_layers)}",
                ids=make_ids(
                    self.frame_context.frame_id,
                    viewport_hash(
                        (ctx.scale, ctx.scale),
                        tuple(ctx.offset),
                        _canvas_size,
                    ),
                ),
            )
        # NOTE: no frame_context= kwarg on image_pass calls -- BaseImagesPass
        # keeps its exact contract shapes (test_mc_pass_contract_shape.py
        # pins prepare/record; D2/D3 consume). Wave B reaches the context via
        # widget._renderer (same reach-through as prepare()'s own
        # `getattr(widget, "_renderer", widget)` line).
        # Perf spans (Wave D): the realize hop (mirrors IC's
        # ic.realize_main) — span/record only, the prepare call below is
        # unchanged.
        with self.frame_context.span(
            "mc.realize_slots",
            detail=lambda: {"layers": len(ctx.projected_layers)},
        ):
            self.image_pass.prepare(self.host, ctx, updates)

        # Submit this frame's tile uploads now (rather than folding them into
        # the main pass's own resourceUpdates below) so generate_all_dirty_mips
        # can safely sample the just-uploaded pixels -- a write enqueued in
        # the same batch a later read pass consumes is not guaranteed visible
        # to that read (docs/dev/rendering/tile-array-atlas-plan.md Phase 9,
        # mirrors image_compare's RhiCanvasRenderer.render). All dirtied
        # (array_index, layer) pairs are then cascaded together, one extra
        # beginPass/endPass pair per (layer, level), all before the main pass
        # opens below (new render targets/pipelines can't be created inside
        # an open pass).
        command_buffer.resourceUpdate(updates)
        dirty_layers = self.image_pass.take_dirty_layers()
        for array_index, layers in self._pending_dirty_layers.items():
            dirty_layers.setdefault(array_index, set()).update(layers)
        # Frame bus: resourceUpdate fact (record only, never instead --
        # mirrors image_compare's RhiCanvasRenderer.render). Recorded after
        # the pending merge (not at the submit line above) so the batch
        # count covers this frame's full cascade input, like IC's
        # array_index_batches.
        self.frame_context.record(
            "resourceUpdate",
            "ran",
            "resourceUpdate submitted upload batch"
            f" array_index_batches={len(dirty_layers)}",
            ids=make_ids(self.frame_context.frame_id, len(dirty_layers)),
        )
        dirty_layer_count = sum(len(layers) for layers in dirty_layers.values())
        if _SKIP_MIP_CASCADE:
            self._pending_dirty_layers = {}
            mips_cascade_ms = 0.0
        else:
            mips_cascade_start = time.monotonic()
            # Perf spans (Wave D): the mips hop (mirrors IC's ic.mips) —
            # span/record only, the cascade call below is unchanged.
            with self.frame_context.span(
                "mc.mips",
                detail=lambda: {"dirty_layers": dirty_layer_count},
            ):
                self._pending_dirty_layers = self.image_pass.generate_all_dirty_mips(
                    self, command_buffer, dirty_layers, time_budget_ms=MIPS_CASCADE_TIME_BUDGET_MS
                )
            mips_cascade_ms = (time.monotonic() - mips_cascade_start) * 1000.0
        deferred_count = sum(
            len(layers) for layers in self._pending_dirty_layers.values()
        )
        # Frame bus: mips cascade fact (record only, never instead --
        # mirrors image_compare's RhiCanvasRenderer.render mips record).
        # Timing vars only, no behavior change.
        _mips_stats = BudgetStats(
            budget=dirty_layer_count,
            uploaded=dirty_layer_count - deferred_count,
            skipped=deferred_count,
            time_ms=mips_cascade_ms,
            budget_ms=float(MIPS_CASCADE_TIME_BUDGET_MS),
            more_pending=bool(self._pending_dirty_layers),
        )
        self.frame_context.record(
            "mips",
            "ran" if dirty_layer_count else "skipped",
            f"mips dirty_layers={dirty_layer_count}"
            f" deferred={deferred_count} {_mips_stats.describe()}",
            ids=make_ids(
                self.frame_context.frame_id,
                dirty_layer_count,
                _mips_stats.as_tuple(),
            ),
        )
        if self._pending_dirty_layers:
            # Both canvases are on-demand (image_compare's own render_context
            # says so explicitly: "this canvas is purely on-demand"), so a
            # mip cascade group left deferred here by the time budget would
            # otherwise never get a further render() call to finish it in,
            # leaving that array layer's upper mip levels permanently stale
            # (still whatever a previously-evicted, unrelated tile last wrote
            # there). Under the widget's Linear mipmap filter, sampling that
            # layer at anything below 1:1 then trilinear-blends fresh level-0
            # content against that stale upper-level content -- visible as a
            # moire/seam grid at tile boundaries on high-frequency content.
            self.frame_context.record_invalidation(
                "cascade_deferred",
                "mip cascade group deferred by time budget"
                f" array_index_batches={len(self._pending_dirty_layers)}"
                " -- re-kicking render for the next cascade slice",
                ids=make_ids(
                    self.frame_context.frame_id, len(self._pending_dirty_layers)
                ),
            )
            self.host.update()

        updates = self.rhi.nextResourceUpdateBatch()

        active_feature_passes = iter_active_render_passes(ctx, self.feature_passes)
        for render_pass in active_feature_passes:
            render_pass.prepare(
                self.host, ctx, updates, frame_context=self.frame_context
            )

        # Perf spans (Wave D): the submit hop of the wheel-to-photon chain
        # (span/record only — every QRhi call below is unchanged; mirrors
        # IC's ic.submit). None-safe by construction: the context is
        # renderer-owned, never None here.
        with self.frame_context.span(
            "mc.submit",
            detail=lambda: {"entries": len(ctx.projected_layers)},
        ):
            command_buffer.beginPass(
                target,
                self.host._theme_or_palette_bg(),
                QRhiDepthStencilClearValue(1.0, 0),
                updates,
            )
            # ``endPass`` in a ``finally``: a raise from the base or a feature
            # pass record must not leave the pass open (the next frame would
            # render into a never-closed pass — stuck black frame). The
            # exception still propagates.
            try:
                self.image_pass.record(command_buffer, self.host, ctx)
                for render_pass in active_feature_passes:
                    render_pass.record(
                        command_buffer, self.host, ctx, frame_context=self.frame_context
                    )
            finally:
                command_buffer.endPass()

        # Mirrors image_compare's RhiCanvasRenderer.render() tail: draws
        # every GlassHUD (ZoomIndicator) registered on `self.host.glass_panels`
        # -- must run after endPass() above, since render_backdrops() reads
        # back this frame's own just-submitted colorTexture() as backdrop
        # content (see shared.rendering.glass_panel's module docstring for
        # why a panel can never read back its *own* rendering, i.e. why no
        # feature pass draws a glass panel into this same target). Stashed
        # on `self.host` (not `ctx`) for each GlassHUD's own
        # GlassPanelDisplayWidget to read.
        glass_panels = getattr(self.host, "glass_panels", None)
        if glass_panels is not None:
            self.glass_panel_renderer.render_backdrops(
                command_buffer, self.host.colorTexture(), dict(glass_panels.items())
            )
            self.host._glass_panel_sprites = self.glass_panel_renderer.ready_sprites
            self.host._glass_panel_images = self.glass_panel_renderer.ready_images

        snapshot_last_frame(self.frame_context)
        # Perf trace dump (gated inside, mirrors IC renderer): serializes
        # the span ring every Nth frame. Off means no file — zero impact.
        maybe_dump_trace(self.frame_context)
        return True