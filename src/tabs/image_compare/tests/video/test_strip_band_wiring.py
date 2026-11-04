"""Strip atlas phase 2 — band host + recording adapter (CPU, offscreen).

The band's GPU half needs a live window (supervisor proof); everything that
decides *what* it draws is CPU and is pinned here:

* the adapter asks the recording for the frame state and the exporter's proxy
  seam for the pixel pair — a strip tile never decodes on the interaction path;
* the block layout / block->time mapping come from the timeline's own
  ``strip_block_layout`` (rep frames), never from a second formula, while the
  TILE is the filmstrip thumbnail (``strip_tile_size``) — not the block cell's
  aspect, which would letterbox every frame inside its cell;
* one window per view change: the same view does not build twice, a new view
  supersedes the older window, and a stale window is refused at render time;
* a build that raises keeps the previous window (fail-open);
* no timer, settle gate or sleep exists in the strip path.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from PySide6.QtWidgets import QScrollArea
from sli_ui_toolkit.ui.widgets.composite.timeline_widget import (
    viewport as timeline_viewport,
)

from tabs.image_compare.plugins.video_editor.presenter_parts import (
    strip_band as strip_band_module,
)
from tabs.image_compare.plugins.video_editor.presenter_parts.strip_band import (
    StripBandHost,
)
from tabs.image_compare.plugins.video_editor.services import strip_recording
from tabs.image_compare.plugins.video_editor.services.strip_recording import (
    MAX_BLOCKS,
    StripRecordingAdapter,
    proxy_pair_for,
    strip_tile_size,
    timeline_strip_layout,
)
from tabs.image_compare.plugins.video_editor.widgets.timeline import (
    VideoTimelineWidget,
)
from tabs.image_compare.services.video_snapshot_rendering.geometry import (
    THUMBNAIL_TARGET_SIZE,
    is_thumbnail_target_size,
)
from tabs.image_compare.services.video_snapshot_rendering.strip_window import (
    LAYER_PX,
    StripWindowCancelled,
    build_window,
    is_current,
    render_window,
)

_FPS = 10
_FRAMES = 121
_VIEWPORT = (900, 260)
#: Proxy size the exporter seam hands back for a strip tile request.
_PROXY = (160, 90)


# ---------------------------------------------------------------------------
# Harness: real timeline widget + real scroll area (the dialog's own shape).
# ---------------------------------------------------------------------------


class _Recording:
    """Duck-typed recording: frame state per timestamp, no pixels of its own."""

    fps = float(_FPS)

    def __init__(self):
        self.evaluated: list[float] = []

    def evaluate_at(self, timestamp: float):
        self.evaluated.append(float(timestamp))
        return SimpleNamespace(timestamp=float(timestamp))


class _Editor:
    def __init__(self, recording):
        self._recording = recording

    def get_fps(self) -> int:
        return _FPS

    def get_current_recording(self):
        return self._recording


class _Exporter:
    """The exporter seam the strip uses: proxy pair only, never a decode."""

    def __init__(self):
        self.calls: list[tuple] = []

    def _resolve_thumbnail_images(self, snap, auto_crop, target_size):
        self.calls.append((snap, bool(auto_crop), tuple(target_size)))
        image1 = Image.new("RGBA", _PROXY, (20, 40, 60, 255))
        image2 = Image.new("RGBA", _PROXY, (200, 180, 160, 255))
        return ((image1, image2), ("session", "session"))


class _Harness:
    def __init__(self, qapp, *, zoom=1.0):
        self.qapp = qapp
        self.snapshots = [
            SimpleNamespace(timestamp=index / _FPS, frame_index=index)
            for index in range(_FRAMES)
        ]
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(False)
        self.widget = VideoTimelineWidget(self.snapshots, store=None)
        self.scroll.setWidget(self.widget)
        self.scroll.resize(*_VIEWPORT)
        self.scroll.show()
        qapp.processEvents()
        self.widget.set_data(
            self.snapshots, fps=_FPS, duration=(_FRAMES - 1) / _FPS
        )
        self.widget.resize(880, 220)
        self.widget.fit_view()
        if zoom is not None:
            self.widget.set_zoom(float(zoom))
        self.widget.update_layout_width()
        qapp.processEvents()
        self.recording = _Recording()
        self.editor = _Editor(self.recording)
        self.exporter = _Exporter()

    def view(self):
        return SimpleNamespace(timeline=self.widget)

    def host(self, **kwargs):
        # The adjacent-level cache fill uses the same stand-in planner as the
        # window (the production host leaves it to the level cache's planner);
        # a test that counts planner calls injects its own instead.
        kwargs.setdefault("prepare_plan_builder", _plain_plan_builder)
        return StripBandHost(
            view=self.view(),
            editor_service=self.editor,
            export_controller=SimpleNamespace(video_exporter=self.exporter),
            **kwargs,
        )

    def scroll_to(self, value: int) -> None:
        self.scroll.horizontalScrollBar().setValue(int(value))
        self.qapp.processEvents()

    def close(self) -> None:
        self.widget.close()
        self.scroll.close()
        self.scroll.deleteLater()


def _plain_plan_builder(snapshot, request, image1, image2):
    """Stand-in for the production plan builder: a plan carrying the proxies."""
    scene = SimpleNamespace(
        split_position_visual=0.5,
        is_horizontal=False,
        overlay_clip_rect=None,
        feature_overrides={},
        channel_mode_int=0,
        diff_mode_int=0,
    )
    plan = SimpleNamespace(
        render_scene=scene,
        image1=image1,
        image2=image2,
        canvas_w=image1.width,
        canvas_h=image1.height,
        fill_rgba=(0, 0, 0, 0),
        geometry_letterbox=False,
        image_is_padded_composite=False,
    )
    return SimpleNamespace(plan=plan)


def _recording_builder(*args, **kwargs):
    """The real ``build_window`` with the fake plan builder injected."""
    kwargs["plan_builder"] = _plain_plan_builder
    return build_window(*args, **kwargs)


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


def _layout(harness):
    current = timeline_strip_layout(harness.widget, fps=_FPS)
    assert current is not None, "the fitted strip must lay out at least one block"
    return current


def test_layout_is_the_timeline_block_layout(qapp):
    """Block count/time and the CELL come from the timeline; the tile is it x DPR.

    The tile used to be the fixed 160x90 filmstrip thumbnail; it is now the
    block CELL the timeline lays out (``strip_block_layout``'s draw_w plus the
    strip row height) at the target's device pixel ratio — so the same code
    gives 128x72 on a 1x screen and 256x144 on a 2x one.
    """
    harness = _Harness(qapp)
    try:
        layout, first_block, count = _layout(harness)

        assert count >= 2
        cell = (
            int(round(timeline_viewport.get_base_tile_width(harness.widget))),
            int(harness.widget.STRIP_HEIGHT),
        )
        assert tuple(layout.block_size) == cell
        assert layout.dpr == 1.0, "offscreen target: one device pixel per px"
        assert layout.tile_size == strip_tile_size(cell, layout.dpr)
        assert layout.tile_size == cell, "at 1x the tile IS the cell, 1:1"
        assert layout.fps == _FPS
        # One row: tile i lands on block i of the strip.
        assert layout.cols == count
        assert layout.first_block == first_block
        assert layout.block_count == count

        span = timeline_viewport._strip_visible_span(harness.widget)
        blocks = timeline_viewport.strip_block_layout(
            harness.widget,
            content_start_x=span[0],
            start_x=span[1],
            end_x=span[2],
            overscan_blocks=0,
        )
        # The block's TIME is its representative frame over fps — never
        # block/fps (a block spans several frames at the fitted zoom).
        assert [
            layout.block_time(first_block + index) for index in range(count)
        ] == [block[2] / _FPS for block in blocks[:count]]
        first, last = timeline_viewport.visible_strip_block_range(
            harness.widget, overscan_blocks=0
        )
        assert (first_block, first_block + count) == (
            first,
            min(last, first + MAX_BLOCKS),
        )
    finally:
        harness.close()


def test_band_tiles_are_the_block_cell_at_the_target_dpr(qapp):
    """The band plans the CELL it draws into, in device pixels.

    A strip cell is not the old fixed 160x90 thumbnail: it is the timeline's
    block cell (``draw_w`` x strip row height), so at 1x a 128x72 tile lands
    1:1 in a 128x72 cell instead of being downscaled from 160x90 — and on a 2x
    target the same cell asks for 256x144 instead of being upscaled 1.6x.
    """
    harness = _Harness(qapp)
    calls: list = []

    def _builder(recording, first_block, count, tile_w, tile_h, **kwargs):
        calls.append((tile_w, tile_h, kwargs.get("request")))
        return SimpleNamespace(first_block=first_block, count=count)

    host = harness.host(builder=_builder)
    try:
        host.attach()

        cell = (
            int(round(timeline_viewport.get_base_tile_width(harness.widget))),
            int(harness.widget.STRIP_HEIGHT),
        )
        tile_w, tile_h, request = calls[-1]
        assert (tile_w, tile_h) == strip_tile_size(cell, 1.0) == cell
        # The same numbers reach the plan builder: the request target is what
        # keys the cover path, so the plan a tile gets is the cell's.
        assert (
            request.target_surface.width,
            request.target_surface.height,
        ) == cell
        assert (request.auto_crop, request.fit_content) == (False, False)

        # A zoom moves the visible blocks, never the cell: the toolkit's strip
        # blocks are fixed-width NLE cells (measured 2026-11-04), so the tile
        # the band plans is unchanged by it.
        harness.widget.set_zoom(8.0)
        harness.widget.update_layout_width()
        qapp.processEvents()
        host.on_view_changed()
        assert calls[-1][:2] == (tile_w, tile_h)
    finally:
        host.detach()
        harness.close()


def test_the_tile_is_the_cell_times_the_device_pixel_ratio():
    """One tile geometry formula: the cell at the target's DPR, one layer max."""
    cell = (128, 72)
    assert strip_tile_size(cell, 1.0) == cell
    assert strip_tile_size(cell, 2.0) == (256, 144), "the 2x cell IS one layer"
    # A fractional ratio rounds, and stays inside one array layer (LAYER_PX is
    # taken from strip_window, which refuses a larger tile at build time).
    assert strip_tile_size(cell, 1.25) == (160, 90)
    assert strip_tile_size(cell, 1.5) == (192, 108)
    assert strip_tile_size(cell, 2.5) == (256, 144)
    assert max(strip_tile_size(cell, 4.0)) == LAYER_PX
    # Aspect is preserved when the clamp bites (128:72 == 256:144).
    assert strip_tile_size((300, 300), 2.0) == (LAYER_PX, LAYER_PX)
    # No layout in hand (the latency bench's default case): the thumbnail,
    # still one array layer at most (2x 160x90 clamps to 256x144).
    assert strip_tile_size() == THUMBNAIL_TARGET_SIZE
    assert strip_tile_size(None, 2.0) == (LAYER_PX, 144)


def test_the_cover_gate_is_the_tiles_aspect_within_one_layer():
    """A strip tile is cover/full-bleed; an export frame is fit/letterbox.

    The gate used to be the exact 160x90, which put the upper rung (256x144)
    on the fit path and letterboxed every non-16:9 source inside its cell. It
    is now the thumbnail's ASPECT bounded by one array layer — the two things
    that make a target a strip tile. The bound is the export guard: no target
    bigger than one layer can be a tile, so 16:9 export frames keep their
    letterbox semantics.
    """
    assert is_thumbnail_target_size(*THUMBNAIL_TARGET_SIZE)
    assert is_thumbnail_target_size(128, 72), "the 1x block cell"
    assert is_thumbnail_target_size(256, 144), "the 2x block cell, one layer"
    assert is_thumbnail_target_size(192, 108)
    assert is_thumbnail_target_size(160, 89), "a rounded device size still counts"
    # Not tiles: every real export / thumbnail-export target above one layer.
    assert not is_thumbnail_target_size(1920, 1080)
    assert not is_thumbnail_target_size(800, 600)
    assert not is_thumbnail_target_size(3840, 2160)
    # Another aspect is not a block cell and keeps the fit + pad path.
    assert not is_thumbnail_target_size(160, 120)
    assert not is_thumbnail_target_size(200, 200)
    assert not is_thumbnail_target_size(0, 0)


def test_a_tile_at_the_layer_ceiling_is_full_bleed_for_a_4_3_source():
    """256x144 covers a 4:3 source; the fit path (export) would pad it.

    The upper rung is where the old exact-size gate failed: the tile took
    ``fit_source_to_content`` and the plan carried an ``overlay_clip_rect``, so
    the band drew bars inside the cell. Cover crops to the cell instead, which
    is what every lower rung already did.
    """
    from tabs.image_compare.services.video_snapshot_rendering.geometry import (
        cover_source_to_content,
        fit_source_to_content,
    )

    source = Image.new("RGBA", (640, 480), (90, 60, 30, 255))

    covered = cover_source_to_content(source, (256, 144))
    assert (covered.width, covered.height) == (256, 144)
    alpha = covered.read_array((0, 0, 256, 144))[..., 3]
    assert int(alpha.min()) == 255, "a cover plan leaves no transparent bar"

    # The path the export targets keep: fit inside, pads expressed as insets.
    fitted = fit_source_to_content(source, (256, 144))
    assert (fitted.width, fitted.height) == (192, 144), (
        "the fit path is what letterboxes a 4:3 source — the gate keeps it off "
        "strip tiles and on export targets"
    )


def _real_plan(target, source_size):
    """The production plan for one strip tile target, offscreen."""
    from core.store import Store
    from core.store_viewport import SessionData
    from engine.cache import TargetSurfaceSpec
    from tabs.image_compare.plugins.video_editor.services.keyframing.types import (
        FrameSnapshot,
    )
    from tabs.image_compare.plugins.video_editor.services.video_export_models import (
        VideoRenderRequest,
    )
    from tabs.image_compare.services.video_snapshot_rendering.renderer import (
        SnapshotFrameRenderer,
    )
    from tabs.image_compare.state.document import DocumentModel
    from tabs.image_compare.state.models import ImageSessionState, RenderCacheState

    store = Store()
    store.create_workspace_session(session_type="image_compare")
    store.set_session_state_slot("document", DocumentModel())
    store.viewport.session_data = SessionData(
        image_state=ImageSessionState(), render_cache=RenderCacheState()
    )
    document = store.get_session_state_slot("document")
    snapshot = FrameSnapshot(
        timestamp=0.0,
        viewport_state=store.viewport.freeze_for_export(),
        settings_state=store.settings.freeze_for_export(),
        name1=document.get_current_display_name(1),
        name2=document.get_current_display_name(2),
    )
    source = Image.new("RGBA", source_size, (90, 60, 30, 255))
    request = VideoRenderRequest(
        target_surface=TargetSurfaceSpec(
            width=target[0], height=target[1], fill_rgba=(0, 0, 0, 0)
        ),
        font_path=None,
        auto_crop=False,
        fit_content=False,
        global_bounds=None,
    )
    renderer = SnapshotFrameRenderer(image_loader=lambda *_a, **_k: None)
    return renderer.prepare_canvas_frame_from_images(
        snapshot, request, source, source, normalize_snapshot=False
    ).plan


def test_the_production_plan_for_a_ceiling_tile_carries_no_letterbox_clip():
    """The real plan builder, not just the helper: no bars in a 2x cell.

    ``prepare_miss`` expresses a fit-inside plan as ``overlay_clip_rect`` (the
    content inset the pass paints inside the tile); a cover plan leaves it
    ``None``. A 4:3 source at the 256x144 upper rung must arrive with no clip —
    otherwise the cell shows the frame squeezed into a 192x144 box with black
    bars, which is the bug the aspect gate exists for.
    """
    from tabs.image_compare.services.video_snapshot_rendering import (
        strip_window as strip_window_module,
    )

    plan = _real_plan((256, 144), (640, 480))

    assert plan.image1.size == (256, 144), "the plan canvas is the whole tile"
    assert getattr(plan, "render_scene", None) is not None
    assert plan.render_scene.overlay_clip_rect is None, (
        "a 4:3 source in a 16:9 TILE must be covered, never padded"
    )
    tile = strip_window_module.StripTile(
        index=0,
        block=0,
        timestamp=0.0,
        rect=(0, 0, 256, 144),
        plan=plan,
        image1=plan.image1,
        image2=plan.image2,
        canvas_w=plan.canvas_w,
        canvas_h=plan.canvas_h,
        tile_w=256,
        tile_h=144,
    )
    letterbox, _canvas_letterbox, content_rect = (
        strip_window_module.tile_letterbox_geometry(tile)
    )
    assert letterbox == (0.0, 0.0, 1.0, 1.0)
    assert content_rect == (0, 0, 256, 144)


def test_strip_tile_fits_one_array_layer():
    """A tile is uploaded 1:1 into one array layer, so it never exceeds it.

    Checked at every ratio a real screen reports, not only the default one: the
    clamp has to hold for the 2x/2.5x cells too.
    """
    cell = (128, 72)
    for dpr in (1.0, 1.25, 1.5, 2.0, 2.5, 3.0):
        tile_w, tile_h = strip_tile_size(cell, dpr)
        assert 0 < tile_w <= LAYER_PX and 0 < tile_h <= LAYER_PX
    tile_w, tile_h = strip_tile_size()
    assert 0 < tile_w <= LAYER_PX and 0 < tile_h <= LAYER_PX


def test_band_proxy_is_requested_at_the_levels_tile_size(qapp):
    """The exporter seam gets a LEVEL's tile size — never a size no level has.

    The live window's tiles ask at the current LEVEL's size (the block cell in
    device pixels); the host's ahead-of-time cache fill asks at the NEXT level's
    size — that is the point of preparing it.
    """
    harness = _Harness(qapp)
    host = harness.host(builder=_recording_builder)
    try:
        host.attach()
        window = host.window()
        assert window is not None

        asked = {tuple(size) for _snap, _auto_crop, size in harness.exporter.calls}
        cell = (
            int(round(timeline_viewport.get_base_tile_width(harness.widget))),
            int(harness.widget.STRIP_HEIGHT),
        )
        assert window.level.tile_size == cell
        assert cell in asked
        declared = {tuple(window.level.tile_size)}
        prepared = host.last_preparation
        if prepared is not None:
            declared.add(tuple(prepared.level.tile_size))
        assert asked <= declared, "a tile asked for a size no level declares"
        for _snap, auto_crop, _size in harness.exporter.calls:
            assert auto_crop is False
    finally:
        host.detach()
        harness.close()


def test_band_tile_is_not_letterboxed_for_a_thumbnail_shaped_pair(qapp):
    """A 16:9 proxy fills its thumbnail: no bars inside the cell.

    The uniform letterbox comes from the plan (canvas vs content), so a
    thumbnail-shaped source pair must arrive at full-bleed UVs — the phase-2
    cell-aspect tile reported a sub-unit letterbox on every tile.
    """
    from tabs.image_compare.services.video_snapshot_rendering import (
        strip_window as strip_window_module,
    )

    harness = _Harness(qapp)
    # The harness exporter returns a 160x90 (16:9) pair: the thumbnail shape.
    host = harness.host(builder=_recording_builder)
    try:
        host.attach()
        window = host.window()
        assert window is not None

        for tile in window.tiles:
            letterbox, _canvas_letterbox, content_rect = (
                strip_window_module.tile_letterbox_geometry(tile)
            )
            assert letterbox == (0.0, 0.0, 1.0, 1.0)
            assert content_rect == (0, 0, tile.tile_w, tile.tile_h)
    finally:
        host.detach()
        harness.close()


def test_scrolling_moves_the_window_to_later_blocks(qapp):
    """A forward scroll moves the window to later blocks.

    The step is RELATIVE to where the view sits: since toolkit #145 a zoom-in
    keeps its centre, so ``fit_view(); set_zoom(1.0)`` no longer leaves the
    scroll at 0 and an absolute ``scroll_to(400)`` would move the view
    backwards (this test encoded that old collapse).
    """
    harness = _Harness(qapp)
    try:
        _, first_before, _ = _layout(harness)
        bar = harness.scroll.horizontalScrollBar()
        step = max(1, (bar.maximum() - bar.value()) // 4)
        harness.scroll_to(bar.value() + step)
        _, first_after, _ = _layout(harness)
        assert first_after > first_before
    finally:
        harness.close()


def test_block_time_is_the_block_representative_frame(qapp):
    """At the fitted zoom one block spans several frames — not ``block / fps``.

    The discriminating view: ``block / fps`` would agree with the truth when a
    block IS a frame (zoom 1), so the assertion runs where frames_per_block > 1
    (the fitted strip the editor opens on).
    """
    harness = _Harness(qapp, zoom=None)
    try:
        layout, first_block, count = _layout(harness)
        span = timeline_viewport._strip_visible_span(harness.widget)
        blocks = timeline_viewport.strip_block_layout(
            harness.widget,
            content_start_x=span[0],
            start_x=span[1],
            end_x=span[2],
            overscan_blocks=0,
        )
        slot_width = timeline_viewport.get_slot_width(harness.widget)
        frames_per_block = blocks[0][1] / slot_width
        assert frames_per_block > 1.5, "fitted zoom must span frames per block"
        assert count >= 2

        times = [layout.block_time(first_block + index) for index in range(count)]

        assert times == [block[2] / _FPS for block in blocks[:count]]
        assert times[1] != pytest.approx((first_block + 1) / _FPS)
    finally:
        harness.close()


def test_adapter_delegates_the_frame_state_to_the_recording(qapp):
    harness = _Harness(qapp)
    try:
        layout, _first, _count = _layout(harness)
        adapter = StripRecordingAdapter(
            harness.recording,
            proxy_pair=proxy_pair_for(harness.exporter),
            layout=layout,
            fps=layout.fps,
        )

        state = adapter.evaluate_at(0.3)

        assert state.timestamp == pytest.approx(0.3)
        assert harness.recording.evaluated == [pytest.approx(0.3)]
        assert adapter.fps == _FPS
        assert adapter.block_count == layout.block_count
        assert adapter.layout is layout
    finally:
        harness.close()


def test_adapter_returns_the_session_proxy_pair_without_decoding(
    qapp, monkeypatch
):
    """The tile's pixels come from the exporter seam — no decode anywhere."""
    harness = _Harness(qapp)

    def _boom(*_args, **_kwargs):
        raise AssertionError("the strip decoded pixels on the interaction path")

    monkeypatch.setattr(Image, "open", _boom)
    try:
        layout, _first, _count = _layout(harness)
        adapter = StripRecordingAdapter(
            harness.recording,
            proxy_pair=proxy_pair_for(harness.exporter),
            layout=layout,
            fps=layout.fps,
        )
        snapshot = adapter.evaluate_at(0.2)

        pair = adapter.images_for(snapshot, layout.tile_size)

        assert pair is not None
        assert harness.exporter.calls == [
            (snapshot, False, (layout.tile_size[0], layout.tile_size[1]))
        ]
        image1, image2 = pair
        assert (image1.width, image1.height) == _PROXY
        # A miss (no proxy at this size) is a placeholder tile, not a decode.
        harness.exporter.calls.clear()
        assert proxy_pair_for(SimpleNamespace()) is None
        assert proxy_pair_for(harness.exporter)(snapshot, (1, 1))[0] is not None
    finally:
        harness.close()


def test_production_proxy_seam_is_reused_and_never_decodes(monkeypatch):
    """The strip asks the exporter's own seam; a miss is a placeholder."""
    from types import MethodType

    from tabs.image_compare.plugins.video_editor.services.video_export import (
        service as export_service,
    )

    def _boom(*_args, **_kwargs):
        raise AssertionError("fell through to a full-res decode")

    monkeypatch.setattr(export_service, "resolve_thumbnail_proxy_source", _boom)
    proxies = {
        "/missing/a.png": Image.new("RGBA", _PROXY, (10, 20, 30, 255)),
        "/missing/b.png": Image.new("RGBA", _PROXY, (40, 50, 60, 255)),
    }
    stub = SimpleNamespace(
        _thumbnail_proxy_resolver=(
            lambda path, auto_crop, size: (proxies.get(path), "session")
        ),
        _image_repository=None,
        _last_thumbnail_source=None,
    )
    stub._resolve_thumbnail_images = MethodType(
        export_service.VideoExporterService._resolve_thumbnail_images, stub
    )
    snapshot = SimpleNamespace(
        image1_path="/missing/a.png", image2_path="/missing/b.png"
    )

    pair = proxy_pair_for(stub)(snapshot, _PROXY)

    assert pair[0] is proxies["/missing/a.png"]
    assert pair[1] is proxies["/missing/b.png"]
    assert stub._last_thumbnail_source == ("session", "session")


# ---------------------------------------------------------------------------
# The band host
# ---------------------------------------------------------------------------


class _BandStub:
    """Records the callbacks the band would invoke from its GPU frame."""

    def __init__(self):
        self.render = None
        self.release = None
        self.placed: list = []

    def place(self, rect):
        self.placed.append(rect)


def _collecting_builder(calls, window=None):
    def _builder(recording, first_block, count, tile_w, tile_h, **kwargs):
        calls.append((first_block, count, tile_w, tile_h))
        return window if window is not None else SimpleNamespace(
            first_block=first_block, count=count
        )

    return _builder


def test_attach_enables_the_band_and_builds_one_window(qapp):
    harness = _Harness(qapp)
    calls: list = []
    host = harness.host(builder=_collecting_builder(calls))
    try:
        assert host.attach() is True

        band = harness.widget.strip_band()
        assert band is not None
        assert band.renderer() is not None
        assert len(calls) == 1, "attach must plan the current view exactly once"
        assert host.build_count == 1
        # The band sits on the timeline's own strip row, not the whole widget.
        assert band.band_rect() == harness.widget.strip_band_rect()
    finally:
        host.detach()
        harness.close()


def test_one_window_build_per_view_change(qapp):
    harness = _Harness(qapp)
    calls: list = []
    host = harness.host(builder=_collecting_builder(calls))
    try:
        host.attach()
        assert len(calls) == 1

        # Same view: no rebuild, whatever signals fire.
        host.on_view_changed()
        harness.widget.viewportChanged.emit()
        assert len(calls) == 1

        # A real scroll step: one build, from the production signal chain.
        harness.scroll_to(500)
        harness.widget._on_host_scroll()
        assert len(calls) == 2

        # ... and no movement again: still one per change.
        harness.widget._on_host_scroll()
        assert len(calls) == 2
    finally:
        host.detach()
        harness.close()


def test_new_view_supersedes_the_previous_window(qapp):
    harness = _Harness(qapp)
    host = harness.host(builder=_recording_builder)
    try:
        host.attach()
        first = host.window()
        assert first is not None
        assert is_current(first)

        harness.scroll_to(500)
        harness.widget._on_host_scroll()
        second = host.window()

        assert second is not None and second is not first
        assert is_current(second)
        assert first.cancelled, "a newer window must supersede the older one"
        with pytest.raises(StripWindowCancelled):
            # The stale window is refused before any GPU work happens.
            render_window(None, first, rhi=None, command_buffer=None)
    finally:
        host.detach()
        harness.close()


def test_build_failure_keeps_the_previous_window(qapp):
    harness = _Harness(qapp)
    state = {"fail": False}
    calls: list = []

    def _builder(recording, first_block, count, tile_w, tile_h, **kwargs):
        calls.append((first_block, count))
        if state["fail"]:
            raise RuntimeError("no plan builder in this shape")
        return _recording_builder(
            recording, first_block, count, tile_w, tile_h, **kwargs
        )

    host = harness.host(builder=_builder)
    try:
        host.attach()
        window = host.window()
        assert window is not None

        state["fail"] = True
        harness.scroll_to(500)
        harness.widget._on_host_scroll()

        assert len(calls) == 2
        assert host.window() is window, "fail-open: the previous window stays"
        assert host.build_errors == 1
        assert "no plan builder" in (host.last_error or "")
    finally:
        host.detach()
        harness.close()


def test_render_draws_the_built_window_through_the_band(qapp, monkeypatch):
    """The band's render callback hands its own target/RHI to the renderer."""
    harness = _Harness(qapp)
    host = harness.host(builder=_recording_builder)
    drawn: list = []

    def _fake_render(target, window, *, rhi, command_buffer=None, **_kwargs):
        drawn.append((target, window, rhi, command_buffer))

    monkeypatch.setattr(strip_band_module, "render_window", _fake_render)
    try:
        host.attach()
        band = harness.widget.strip_band()
        window = host.window()
        assert window is not None

        sentinel_target = object()
        sentinel_rhi = object()
        monkeypatch.setattr(band, "renderTarget", lambda: sentinel_target)
        monkeypatch.setattr(band, "rhi", lambda: sentinel_rhi)

        host._render("cb", band)

        assert drawn == [(sentinel_target, window, sentinel_rhi, "cb")]
        assert host.render_count == 1
    finally:
        host.detach()
        harness.close()


def test_no_timer_settle_or_sleep_in_the_strip_path():
    """No debounce, no settle gate, no waiting: the strip builds per tick."""
    modules = [
        inspect.getmodule(strip_band_module),
        inspect.getmodule(strip_recording),
    ]
    forbidden = (
        "QTimer",
        "SettleGate",
        "singleShot",
        "time.sleep(",
        "QThread",
        "threading.",
    )
    for module in modules:
        source = Path(inspect.getsourcefile(module)).read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in source, f"{module.__name__} uses {token}"


# ---------------------------------------------------------------------------
# Levels: the host prepares the next one, and the render tick never plans
# ---------------------------------------------------------------------------


def test_the_host_prepares_the_adjacent_level_for_the_next_zoom(qapp):
    """The fill starts from a view change, once the level was first planned."""
    from tabs.image_compare.services.video_snapshot_rendering.strip_window import (
        next_level,
    )

    harness = _Harness(qapp)
    host = harness.host(builder=_recording_builder)
    try:
        host.attach()
        window = host.window()
        assert window is not None
        prepared = host.last_preparation
        assert prepared is not None, "a level's first plan must prepare the next"
        assert prepared.level == next_level(window.level)
        assert prepared.complete
        assert prepared.planned == window.count
        assert (
            host._tile_cache.level_tiles(prepared.level) == window.count
        ), "the prepared level's plans must live in the level cache"
        # The visible window is untouched by the fill.
        assert host.window() is window
        assert window.level_warm is False
    finally:
        host.detach()
        harness.close()


def test_the_render_tick_never_prepares_a_level(qapp, monkeypatch):
    """`_render` is the frame path: it must not plan another level, ever.

    The discriminator is a preparing method that refuses to be called: if the
    render tick ever reached it, this test fails with its own message instead of
    a timing number.
    """
    harness = _Harness(qapp)
    host = harness.host(builder=_recording_builder)
    drawn: list = []

    def _fake_render(target, window, *, rhi, command_buffer=None, **_kwargs):
        drawn.append(window)

    def _boom(**_kwargs):
        raise AssertionError("_render planned another level on the frame path")

    monkeypatch.setattr(strip_band_module, "render_window", _fake_render)
    try:
        host.attach()
        # attach() legitimately prepares the next level; from here on the
        # frame path must never reach it again.
        monkeypatch.setattr(host, "prepare_adjacent_level", _boom)
        band = harness.widget.strip_band()
        monkeypatch.setattr(band, "renderTarget", lambda: object())
        monkeypatch.setattr(band, "rhi", lambda: object())

        host._render("cb", band)
        host._render("cb", band)

        assert len(drawn) == 2
        assert host.render_count == 2
    finally:
        host.detach()
        harness.close()


def test_moving_to_a_hidpi_screen_enters_the_prepared_level_warm(qapp, monkeypatch):
    """A real user gesture changes the level — and lands on the prepared rung.

    The gesture is the target's DPR (the window arriving on a screen with
    another ratio; the band target changes, ``on_view_changed`` re-reads it).
    It changes the level because a tile is the block CELL in device pixels: the
    same 128x72 cell asks for 256x144 on a 2x screen, which is exactly the rung
    the host cache-filled at 1x (``next_level`` up from 128x72). So the tick
    that enters it is a swap: the planner is not called and the window says
    ``level_warm``.

    Zoom cannot be this gesture: the toolkit clamps it at 1.0 and its
    strip blocks keep a constant width (measured 2026-11-04 — see
    ``test_a_zoom_keeps_the_cell_and_the_level``).
    """
    harness = _Harness(qapp)
    host = harness.host(
        builder=_recording_builder,
        # The fill is budget-bounded by contract; a test that wants every tile
        # planned gives it room instead of racing the 20 ms slice.
        prepare_budget_ms=5000.0,
    )
    calls: list = []

    def counting(snapshot, request, image1, image2):
        # ``snapshot`` is the harness recording's frame state (timestamp only).
        calls.append(snapshot.timestamp)
        return _plain_plan_builder(snapshot, request, image1, image2)

    monkeypatch.setattr(host, "_dpr", lambda: 1.0)
    host._prepare_plan_builder = counting
    try:
        host.attach()
        window = host.window()
        assert window is not None
        assert window.level.tile_size == (128, 72)
        prepared = host.last_preparation
        assert prepared is not None
        assert prepared.level.tile_size == (256, 144), (
            "the prepared rung is the next screen's cell, one array layer"
        )
        assert len(calls) == window.count, "the fill plans every visible tile"
        calls.clear()

        # The user moves the window to a 2x screen: the same view, a new level.
        monkeypatch.setattr(host, "_dpr", lambda: 2.0)
        host.on_view_changed()

        zoomed = host.window()
        assert zoomed.level != window.level
        assert zoomed.level.tile_size == (256, 144)
        assert calls == [], "a prepared level change must not call the planner"
        assert zoomed.level_warm is True
        assert zoomed.level_changed is True
        assert host.level_changes == 1
        assert host.warm_level_changes == 1
        assert host.cold_level_changes == 0
        assert zoomed.to_dict()["level_warm"] is True
    finally:
        host.detach()
        harness.close()


def test_a_zoom_keeps_the_cell_and_the_level(qapp):
    """Measured, and pinned so nobody re-assumes it: zoom does not scale a cell.

    ``strip_block_layout`` returns the same block width at the fitted view and at
    the zoom clamp (1.0) — zoom changes FRAMES per block (21 at the fitted
    strip, 1 at 1:1), never a cell's pixel size. So the tile (and the
    level) is zoom-invariant, and since the level does not move, a zoom must not
    start a cache fill either (it is a scroll-class tick, not a level entry).
    """
    harness = _Harness(qapp, zoom=None)
    host = harness.host(builder=_recording_builder)
    try:
        host.attach()
        first = host.window()
        assert first is not None
        prepared_before = host._tile_cache.prepared
        runs_before = host.prepare_runs

        harness.widget.set_zoom(1.0)
        harness.widget.update_layout_width()
        qapp.processEvents()
        host.on_view_changed()

        zoomed = host.window()
        assert zoomed is not first, "a zoom moved the visible blocks (a rebuild)"
        assert zoomed.level == first.level
        assert zoomed.level_changed is False
        assert host.level_changes == 0
        assert host.prepare_runs == runs_before, "no level entry, no fill"
        assert host._tile_cache.prepared == prepared_before
    finally:
        host.detach()
        harness.close()


def test_a_level_change_without_a_prepared_level_is_logged_cold(qapp, monkeypatch):
    """The honest fallback: no prepared rung means the tick planned, and says so."""
    harness = _Harness(qapp)
    host = harness.host(builder=_recording_builder)
    monkeypatch.setattr(host, "_dpr", lambda: 1.0)
    monkeypatch.setattr(
        strip_band_module, "next_level", lambda _level, **_kwargs: None
    )
    try:
        host.attach()
        assert host.last_preparation is None, "no rung, nothing to prepare"

        monkeypatch.setattr(host, "_dpr", lambda: 2.0)
        host.on_view_changed()

        zoomed = host.window()
        assert zoomed.level_changed is True
        assert zoomed.level_warm is False
        assert host.level_changes == 1
        assert host.cold_level_changes == 1
        assert host.warm_level_changes == 0
    finally:
        host.detach()
        harness.close()


def test_a_recording_change_abandons_the_preparation_seam(qapp, monkeypatch):
    """Latest-wins: the fill's token moves when the content changes.

    Asserted on the injected seam (the ``should_continue`` the host hands the
    fill), not on pixels: after a recording change that predicate is False, so a
    fill in progress stops at its next tile instead of finishing work that
    belongs to the pre-cut frames.
    """
    harness = _Harness(qapp)
    host = harness.host(builder=_recording_builder)
    captured: list = []

    def _fake_prepare(*_args, **kwargs):
        captured.append(kwargs.get("should_continue"))
        from tabs.image_compare.services.video_snapshot_rendering.strip_window import (
            LevelPreparation,
        )

        return LevelPreparation(level=_args[1], blocks=1, examined=1)

    monkeypatch.setattr(strip_band_module, "prepare_level", _fake_prepare)
    try:
        host.attach()
        token_before = host._prepare_token
        report = host.prepare_adjacent_level()

        assert report is not None and captured
        assert captured[-1]() is True, "an idle host's fill may continue"

        host.on_recording_changed()

        assert host._prepare_token > token_before
        assert captured[0]() is False, "a recording change must stop the fill"
        assert host._prepare_cursor is None
    finally:
        host.detach()
        harness.close()


def test_a_scroll_tick_does_not_prepare_again(qapp):
    """The fill belongs to a LEVEL entry, never to a scroll tick.

    ``level_warm`` is false on any scroll tick (it exposed a block), so gating
    the fill on "the window was cold" would charge every scroll an extra plan
    pass and quietly blow the interaction budget. The trigger is the level.
    """
    harness = _Harness(qapp)
    host = harness.host(builder=_recording_builder)
    try:
        host.attach()
        first = host.last_preparation
        assert first is not None
        planned_before = host._tile_cache.prepared
        runs_before = host.prepare_runs

        bar = harness.scroll.horizontalScrollBar()
        harness.scroll_to(bar.value() + max(1, (bar.maximum() - bar.value()) // 4))
        harness.widget._on_host_scroll()

        assert host.window().level_warm is False, "a scroll tick did plan a block"
        assert host.prepare_runs == runs_before, "a scroll tick must not prepare"
        assert host.last_preparation is first
        assert host._tile_cache.prepared == planned_before
    finally:
        host.detach()
        harness.close()
