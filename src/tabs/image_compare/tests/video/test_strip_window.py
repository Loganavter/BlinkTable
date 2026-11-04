"""Strip window phase 1 — CPU-side contracts (no GPU, no display).

Proves the parts of ``strip_window.build_window`` that decide what the one-pass
renderer will draw, without a QRhi surface:

* one plan + one packed uniform block + one packed instance per visible block,
  and the per-tile rects tile the window target exactly (17/32/64 blocks);
* a newer window supersedes an older one and a stale/cancelled window is never
  applied (``render_window`` refuses before any GPU work);
* a block whose frame state / source pair / plan is missing becomes a
  placeholder tile of its own (fail-closed identity) and the window reports
  why — never a silent blank, never a neighbour's pixels;
* texture-array layers come from ``TileTextureService``, never ``index * 2``;
* block layout + tile size have exactly one source (the timeline layout).

The GPU exactness half is the bench's job: ``src/devtools/bench_strip_batch.py
--renderer production`` at N=2/17/32/64 on a display.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PIL import Image

from tabs.image_compare.services.video_snapshot_rendering.strip_window import (
    LAYER_PX,
    PLACEHOLDER_RGBA,
    StripBlockLayout,
    StripWindowCancelled,
    StripWindowError,
    build_window,
    cancel,
    current_generation,
    is_current,
    live_window,
    render_window,
    uniform_block_size,
)

_TILE = (160, 90)


def _plan(block: int):
    """A minimal plan stand-in carrying distinct pixels per block."""
    scene = SimpleNamespace(
        split_position_visual=0.1 + 0.01 * (block % 64),
        is_horizontal=False,
        overlay_clip_rect=None,
        feature_overrides={},
        channel_mode_int=0,
        diff_mode_int=0,
    )
    return SimpleNamespace(
        render_scene=scene,
        image1=_image(block, 1),
        image2=_image(block, 2),
        canvas_w=_TILE[0],
        canvas_h=_TILE[1],
        fill_rgba=(0, 0, 0, 0),
        geometry_letterbox=False,
        image_is_padded_composite=False,
    )


def _image(block: int, side: int):
    image = Image.new("RGBA", _TILE, (0, 0, 0, 255))
    image.putpixel((0, 0), ((block * 7) % 256, (side * 40) % 256, 17, 255))
    return image


class _Recording:
    """Duck-typed recording: frame state + source pair per block."""

    def __init__(self, *, blocks: int = 64, no_state=(), no_source=()):
        self.layout = StripBlockLayout(
            tile_size=_TILE, fps=10.0, block_count=blocks
        )
        self.fps = 10.0
        self.block_count = blocks
        self._no_state = set(no_state)
        self._no_source = set(no_source)
        self.evaluate_calls: list[float] = []

    def evaluate_at(self, timestamp: float):
        block = int(round(float(timestamp) * 10.0))
        self.evaluate_calls.append(float(timestamp))
        if block in self._no_state:
            return None
        return SimpleNamespace(block=block, timestamp=float(timestamp))

    def images_for(self, snapshot, tile_size):
        if snapshot.block in self._no_source:
            return None
        pair = (_image(snapshot.block, 1), _image(snapshot.block, 2))
        assert (pair[0].width, pair[0].height) == _TILE
        return pair


def _plan_builder(snapshot, request, image1, image2):
    assert image1 is not None and image2 is not None
    return SimpleNamespace(plan=_plan(snapshot.block))


def _build(count: int, *, recording=None, **kwargs):
    return build_window(
        recording if recording is not None else _Recording(blocks=max(64, count)),
        0,
        count,
        _TILE[0],
        _TILE[1],
        request=SimpleNamespace(target_surface=SimpleNamespace(width=_TILE[0], height=_TILE[1])),
        plan_builder=_plan_builder,
        **kwargs,
    )


def _one_row(count: int):
    """The timeline's band geometry: every block in ONE row (cols == count)."""
    return build_window(
        _Recording(blocks=max(64, count)),
        0,
        count,
        _TILE[0],
        _TILE[1],
        request=SimpleNamespace(
            target_surface=SimpleNamespace(width=_TILE[0], height=_TILE[1])
        ),
        layout=StripBlockLayout(
            tile_size=_TILE, fps=10.0, block_count=count, cols=count
        ),
        plan_builder=_plan_builder,
    )


@pytest.mark.parametrize("count", [17, 32, 64])
def test_window_plans_each_visible_block_once(count):
    window = _build(count)

    assert window.count == count
    assert len(window.tiles) == count
    assert [tile.block for tile in window.tiles] == list(range(count))
    assert [tile.index for tile in window.tiles] == list(range(count))
    assert window.tiles[-1].block == count - 1
    assert window.last_block == count - 1
    assert all(not tile.is_placeholder for tile in window.tiles)
    assert window.placeholder_reasons == {}
    assert window.cols >= 1 and window.rows == -(-count // window.cols)
    assert (window.target_w, window.target_h) == (
        window.cols * _TILE[0],
        window.rows * _TILE[1],
    )


@pytest.mark.parametrize("count", [17, 32, 64])
def test_one_uniform_block_and_instance_per_tile(count):
    window = _build(count)
    block_size = uniform_block_size()

    assert [len(tile.uniforms) for tile in window.tiles] == [block_size] * count
    assert [len(tile.instance) for tile in window.tiles] == [len(window.tiles[0].instance)] * count
    # Distinct frame state -> distinct packed uniforms (the pass must not
    # sample one frame's split for every tile).
    assert len({tile.uniforms for tile in window.tiles}) == count
    # And the instance carries each tile's own allocated layers.
    assert len({tile.instance for tile in window.tiles}) == count


@pytest.mark.parametrize("count", [17, 32, 64])
def test_tile_rects_tile_the_target_without_overlap(count):
    window = _build(count)
    rects = window.rects()

    assert len(rects) == count
    assert all((w, h) == _TILE for _x, _y, w, h in rects)
    assert all(x + w <= window.target_w for x, _y, w, _h in rects)
    assert all(y + h <= window.target_h for _x, y, _w, h in rects)
    cells = {(x // _TILE[0], y // _TILE[1]) for x, y, _w, _h in rects}
    assert len(cells) == count
    assert cells == {(index % window.cols, index // window.cols) for index in range(count)}
    drawn = sum(w * h for _x, _y, w, h in rects)
    assert drawn == count * _TILE[0] * _TILE[1]


def test_layers_come_from_the_production_allocator():
    window = _build(17)
    layers = [layer for tile in window.tiles for layer in (tile.layer1, tile.layer2)]
    service = window.tile_service

    assert service is not None
    assert window.layer_count == 2 * len(window.tiles)
    assert len(set(layers)) == len(layers)
    assert all(0 <= layer < window.layer_count for layer in layers)
    assert all(
        service.is_resident(window.tile_source_key(tile, side), (0, 0))
        for tile in window.tiles
        for side in (1, 2)
    )
    # Deterministic: two windows allocate the same slots.
    replay = _build(17)
    assert [tile.layer1 for tile in replay.tiles] == [tile.layer1 for tile in window.tiles]


def test_newer_window_supersedes_the_older_one():
    first = _build(17)
    assert is_current(first)
    assert live_window() is first

    second = _build(32)
    assert is_current(second)
    assert live_window() is second
    assert not is_current(first)
    assert first.cancelled
    assert "superseded" in first.cancel_reason
    assert second.generation > first.generation
    assert current_generation() == second.generation


def test_a_superseded_window_is_never_applied():
    stale = _build(17)
    _build(17)
    # The generation guard fires before any GPU lookup: no rhi/target needed.
    with pytest.raises(StripWindowCancelled):
        render_window(None, stale, rhi=None, tile_service=None)
    assert stale.applied is False


def test_cancel_abandons_a_window_and_is_idempotent():
    window = _build(8)
    cancel(window, "scrolled away")
    assert window.cancelled
    assert window.cancel_reason == "scrolled away"
    assert live_window() is None
    assert not is_current(window)
    cancel(window, "again")
    assert window.cancel_reason == "scrolled away"
    with pytest.raises(StripWindowCancelled):
        render_window(None, window, rhi=None, tile_service=None)


def test_explicit_generation_is_honoured():
    window = _build(4, generation=4242)
    assert window.generation == 4242
    assert current_generation() >= 4242
    assert is_current(window)


def test_missing_source_pair_becomes_an_own_placeholder_tile():
    recording = _Recording(blocks=64, no_source={3})
    window = _build(17, recording=recording)

    tile = window.tiles[3]
    assert tile.is_placeholder
    assert "source" in tile.placeholder_reason
    assert window.placeholder_reasons == {3: tile.placeholder_reason}
    assert len(window.placeholder_tiles) == 1
    # Fail-closed identity: the placeholder draws its OWN flat image, never a
    # neighbour's pixels, and still owns two layers.
    assert tile.image1.size == _TILE and tile.image2.size == _TILE
    assert tile.image1.getcolors(maxcolors=1) == [(tile.image1.width * tile.image1.height, PLACEHOLDER_RGBA)]
    assert tile.layer1 >= 0 and tile.layer2 >= 0
    assert (tile.layer1, tile.layer2) != (window.tiles[2].layer1, window.tiles[2].layer2)
    assert tile.layer1 != window.tiles[2].layer1
    assert tile.uniforms and tile.instance


def test_missing_frame_state_becomes_a_placeholder():
    window = _build(17, recording=_Recording(blocks=64, no_state={5}))
    tile = window.tiles[5]
    assert tile.is_placeholder
    assert "frame state" in tile.placeholder_reason
    assert window.placeholder_reasons == {5: "no frame state"}


def test_failing_plan_builder_becomes_a_placeholder_not_a_crash():
    def exploding_plan(snapshot, _request, _image1, _image2):
        raise RuntimeError("plan builder exploded")

    window = build_window(
        _Recording(blocks=64),
        0,
        8,
        _TILE[0],
        _TILE[1],
        request=SimpleNamespace(),
        plan_builder=exploding_plan,
    )
    assert len(window.placeholder_tiles) == 8
    assert all("plan failed" in reason for reason in window.placeholder_reasons.values())
    # Every placeholder still packs a uniform + an instance: the pass draws
    # N tiles whatever happened during planning.
    assert all(tile.uniforms and tile.instance for tile in window.tiles)


def test_tile_size_has_one_source_the_timeline_layout():
    recording = _Recording(blocks=64)
    with pytest.raises(StripWindowError):
        build_window(
            recording,
            0,
            4,
            320,
            180,
            request=SimpleNamespace(),
            plan_builder=_plan_builder,
        )


def test_tile_must_fit_one_array_layer():
    with pytest.raises(StripWindowError):
        build_window(
            _Recording(blocks=64),
            0,
            4,
            LAYER_PX + 16,
            90,
            request=SimpleNamespace(),
            plan_builder=_plan_builder,
        )


def test_window_reports_its_own_grid():
    window = _build(32)
    assert window.to_dict()["grid"] == (window.cols, window.rows)
    assert window.to_dict()["target"] == (window.target_w, window.target_h)
    assert window.to_dict()["placeholders"] == {}


class _FakeRhi:
    """Minimal RHI double for the composited-scissor Y rule."""

    def __init__(self, *, y_up: bool) -> None:
        self._y_up = bool(y_up)

    def isYUpInFramebuffer(self):  # noqa: N802 - QRhi API name
        return self._y_up


class _FakeTarget:
    def __init__(self, width: int, height: int) -> None:
        from PySide6.QtCore import QSize

        self._size = QSize(int(width), int(height))

    def pixelSize(self):  # noqa: N802 - QRhi API name
        return self._size


def test_framebuffer_rects_follow_the_target_pixel_size_and_y_rule():
    from tabs.image_compare.services.video_snapshot_rendering.strip_pass import (
        framebuffer_rects,
    )

    window = _build(4)
    assert (window.target_w, window.target_h) == (2 * _TILE[0], 2 * _TILE[1])
    target = _FakeTarget(window.target_w, window.target_h)

    # A shown widget on a top-down framebuffer keeps the top-down rects.
    top_down = framebuffer_rects(
        window, target, rhi=_FakeRhi(y_up=False), never_shown=False
    )
    assert top_down == list(window.rects())

    # A never-shown (grab-only) widget skips Qt's compositing step, so the
    # bottom-up framebuffer mirrors every row: without this the atlas reads
    # back as another tile's frame (the phase-0 row-order bug).
    flipped = framebuffer_rects(
        window, target, rhi=_FakeRhi(y_up=False), never_shown=True
    )
    assert flipped == [(x, window.target_h - (y + h), w, h)
                       for (x, y, w, h) in window.rects()]

    # OpenGL reports a bottom-left origin: flip even for a shown widget.
    gl = framebuffer_rects(window, target, rhi=_FakeRhi(y_up=True), never_shown=False)
    assert gl == flipped

    # Device-pixel ratio 2: every rect scales by the target's own pixel size,
    # never by a logical size.
    hidpi = framebuffer_rects(
        window,
        _FakeTarget(window.target_w * 2, window.target_h * 2),
        rhi=_FakeRhi(y_up=False),
        never_shown=False,
    )
    assert hidpi == [(x * 2, y * 2, w * 2, h * 2) for (x, y, w, h) in window.rects()]
    assert hidpi[-1][0] + hidpi[-1][2] == window.target_w * 2


def test_framebuffer_rects_tile_a_fractional_target_without_overlap():
    """The live band: an 8x160x90 grid drawn into 1020x72 (0.797 x 0.8).

    Rounding each cell's ORIGIN and WIDTH independently makes neighbours
    overlap at a fractional scale (cell 1 [0,128), cell 2 starting at 128 with
    width 128, cell 3 starting at 255 — inside cell 2), so a cell's edge column
    was painted by the next tile's viewport. Edge snapping must make adjacent
    cells share an edge and the last cell end exactly on the target.
    """
    from tabs.image_compare.services.video_snapshot_rendering.strip_pass import (
        framebuffer_rects,
    )

    window = _one_row(8)
    assert (window.target_w, window.target_h) == (8 * _TILE[0], _TILE[1])
    target = _FakeTarget(1020, 72)
    rects = framebuffer_rects(
        window, target, rhi=_FakeRhi(y_up=False), never_shown=False
    )

    assert rects[0][0] == 0
    assert rects[-1][0] + rects[-1][2] == 1020
    for left, right in zip(rects, rects[1:]):
        assert right[0] == left[0] + left[2], f"{left} and {right} do not tile"
    assert all(h == 72 for (_x, _y, _w, h) in rects)
    # Every cell keeps its share of the target (127 or 128 of 1020 px).
    assert all(127 <= w <= 129 for (_x, _y, w, _h) in rects)


# ---------------------------------------------------------------------------
# Planned-tile cache: a scroll tick plans only the newly exposed block
# ---------------------------------------------------------------------------


def _window_with_cache(
    cache, *, first_block=0, count=8, recording=None, tile=_TILE, tile_service=None
):
    recording = recording if recording is not None else _Recording(blocks=64)
    return build_window(
        recording,
        first_block,
        count,
        tile[0],
        tile[1],
        request=SimpleNamespace(
            target_surface=SimpleNamespace(width=tile[0], height=tile[1])
        ),
        layout=StripBlockLayout(tile_size=tile, fps=10.0, block_count=count, cols=count),
        plan_builder=_plan_builder,
        tile_cache=cache,
        tile_service=tile_service,
    )


def test_scroll_tick_plans_only_the_newly_exposed_block():
    """One cache per recording: blocks that stayed visible are not re-planned.

    This is the interaction budget's whole shape — a window build plans every
    visible block (16-25 ms of CPU), and a scroll moves the range by a block.
    """
    from tabs.image_compare.services.video_snapshot_rendering.strip_window import (
        StripTileCache,
    )

    cache = StripTileCache()
    recording = _Recording(blocks=64)
    first = _window_with_cache(cache, first_block=0, recording=recording)
    assert (first.cache_hits, first.cache_misses) == (0, 8)

    scrolled = _window_with_cache(cache, first_block=1, recording=recording)
    assert (scrolled.cache_hits, scrolled.cache_misses) == (7, 1)
    # Content identity: the reused tile still carries ITS block's plan/pixels.
    for tile in scrolled.tiles:
        assert tile.block == tile.index + 1
        pixel = tile.image1.getpixel((0, 0))
        assert pixel[0] == (tile.block * 7) % 256, tile.block
        assert tile.plan.render_scene.split_position_visual == pytest.approx(
            0.1 + 0.01 * (tile.block % 64)
        )


def test_a_new_tile_size_is_a_miss():
    """A zoom re-plans every tile: the geometry, not just the range, changed."""
    from tabs.image_compare.services.video_snapshot_rendering.strip_window import (
        StripTileCache,
    )

    cache = StripTileCache()
    recording = _Recording(blocks=64)
    _window_with_cache(cache, first_block=0, recording=recording)
    zoomed = _window_with_cache(cache, first_block=0, recording=recording, tile=(224, 126))

    assert (zoomed.cache_hits, zoomed.cache_misses) == (0, 8)


def test_a_placeholder_is_never_cached():
    """A missing proxy is transient: caching it would pin a blank cell."""
    from tabs.image_compare.services.video_snapshot_rendering.strip_window import (
        StripTileCache,
    )

    cache = StripTileCache()
    recording = _Recording(blocks=64, no_source=(3,))
    _window_with_cache(cache, first_block=0, recording=recording)
    again = _window_with_cache(cache, first_block=0, recording=recording)

    assert (again.cache_hits, again.cache_misses) == (7, 1)
    assert again.tiles[3].is_placeholder


def test_a_different_recording_starts_from_a_cold_cache():
    """Cut/undo/re-open: the plan belongs to the recording it was made for."""
    from tabs.image_compare.services.video_snapshot_rendering.strip_window import (
        StripTileCache,
    )

    cache = StripTileCache()
    _window_with_cache(cache, first_block=0, recording=_Recording(blocks=64))
    other = _window_with_cache(cache, first_block=0, recording=_Recording(blocks=64))

    assert (other.cache_hits, other.cache_misses) == (0, 8)


def test_the_cache_keeps_one_plan_builder_per_scope():
    """The renderer's caches must outlive a window (a cold first tile is ~13 ms)."""
    from tabs.image_compare.services.video_snapshot_rendering.strip_window import (
        StripTileCache,
    )

    cache = StripTileCache()
    assert cache.planner() is cache.planner()
    cache.clear()
    assert cache.planner() is not None


# ---------------------------------------------------------------------------
# Array-slot reuse: a scroll tick must not re-upload every visible tile
# ---------------------------------------------------------------------------


def _service(capacity: int = 128):
    """The band host's allocator (one per band, ``2 * MAX_BLOCKS`` layers)."""
    from engine.cache.tile_texture_service import TileTextureService

    return TileTextureService(max_array_size=int(capacity))


def test_the_same_block_at_the_same_tile_size_shares_a_source_key():
    """Content identity, not window identity: the key a re-upload hangs on.

    With the generation in the key every scroll tick addressed brand-new slots,
    so the pass had to upload every visible tile again (the measured 5.2/17.4/
    32.4 ms of a tick against 0.8/1.3/2.4 ms for a draw-only frame).
    """
    from tabs.image_compare.services.video_snapshot_rendering.strip_window import (
        StripTileCache,
    )

    recording = _Recording(blocks=64)
    first = _window_with_cache(StripTileCache(), first_block=0, recording=recording)
    scrolled = _window_with_cache(
        StripTileCache(), first_block=1, recording=recording
    )

    stayed = first.tile_source_key(first.tiles[1], 1)
    assert first.tiles[1].block == scrolled.tiles[0].block == 1
    assert stayed == scrolled.tile_source_key(scrolled.tiles[0], 1)
    # ... even though they are different windows (a new generation each).
    assert first.generation != scrolled.generation
    assert stayed[0] == "strip-tile"
    assert stayed[1] == first.source_scope == scrolled.source_scope
    # The two sides of one block and two blocks of one window never collide.
    assert first.tile_source_key(first.tiles[1], 2) != stayed
    assert first.tile_source_key(first.tiles[2], 1) != stayed

    # A zoom (new tile size) is a different key by construction.
    zoomed = _window_with_cache(
        StripTileCache(), first_block=0, recording=recording, tile=(224, 126)
    )
    assert zoomed.tile_source_key(zoomed.tiles[1], 1) != stayed
    # Another recording never shares a block's key, even at the same geometry.
    other = _window_with_cache(
        StripTileCache(), first_block=0, recording=_Recording(blocks=64)
    )
    assert other.tile_source_key(other.tiles[1], 1) != stayed


def test_a_scroll_tick_reuses_the_slot_of_a_block_that_stayed_visible():
    """One allocator across windows: unchanged blocks keep their layers."""
    from tabs.image_compare.services.video_snapshot_rendering.strip_window import (
        StripTileCache,
    )

    service = _service()
    cache = StripTileCache()
    recording = _Recording(blocks=64)
    first = _window_with_cache(
        cache, first_block=0, recording=recording, tile_service=service
    )
    scrolled = _window_with_cache(
        cache, first_block=1, recording=recording, tile_service=service
    )

    stayed_before, stayed_after = first.tiles[1], scrolled.tiles[0]
    assert (stayed_before.block, stayed_after.block) == (1, 1)
    assert (stayed_before.layer1, stayed_before.layer2) == (
        stayed_after.layer1,
        stayed_after.layer2,
    )
    assert service.slot_for(scrolled.tile_source_key(stayed_after, 1), (0, 0)) == (
        0,
        stayed_after.layer1,
    )

    # The block that left the range released its layers through the service, so
    # the allocator can hand them out again instead of opening a second array.
    departed = first.tile_source_key(first.tiles[0], 1)
    assert first.tiles[0].block == 0
    assert service.slot_for(departed, (0, 0)) is None
    assert service.grid_for(departed) is None
    assert not service.is_resident(departed, (0, 0))

    # The newly exposed block owns its own layers (never a departed block's).
    fresh = scrolled.tiles[-1]
    assert fresh.block == 8
    assert service.slot_for(scrolled.tile_source_key(fresh, 1), (0, 0)) is not None
    assert {fresh.layer1, fresh.layer2}.isdisjoint(
        {stayed_after.layer1, stayed_after.layer2}
    )
    # One array, and its capacity addresses every slot in use.
    layers = [layer for tile in scrolled.tiles for layer in (tile.layer1, tile.layer2)]
    assert scrolled.layer_count == 2 * scrolled.count
    assert max(layers) < scrolled.layer_count


def test_a_jump_to_a_whole_new_range_stays_inside_one_array():
    """The departed layers are released BEFORE the new ones are allocated."""
    from tabs.image_compare.services.video_snapshot_rendering.strip_window import (
        StripTileCache,
    )

    service = _service()
    recording = _Recording(blocks=200)
    first = _window_with_cache(
        StripTileCache(), first_block=0, count=64, recording=recording,
        tile_service=service,
    )
    second = _window_with_cache(
        StripTileCache(), first_block=100, count=64, recording=recording,
        tile_service=service,
    )

    assert first.layer_count == second.layer_count == 128
    layers = [layer for tile in second.tiles for layer in (tile.layer1, tile.layer2)]
    assert max(layers) < second.layer_count
    assert service.slot_for(first.tile_source_key(first.tiles[0], 1), (0, 0)) is None


def test_a_placeholder_never_shares_the_key_of_the_frame_it_replaces():
    """A blank cell must not be "already uploaded" when its proxy arrives.

    The placeholder draws its own flat image; if it owned the block's real key,
    the pass would skip the upload of the frame that resolves the cell and the
    strip would keep showing the blank tile.
    """
    from tabs.image_compare.services.video_snapshot_rendering.strip_window import (
        StripTileCache,
    )

    recording = _Recording(blocks=64, no_source={3})
    cache = StripTileCache()
    missing = _window_with_cache(cache, recording=recording)
    assert missing.tiles[3].is_placeholder

    recording._no_source = set()  # the proxy becomes available on the next tick
    resolved = _window_with_cache(cache, recording=recording)
    assert not resolved.tiles[3].is_placeholder
    assert resolved.tile_source_key(
        resolved.tiles[3], 1
    ) != missing.tile_source_key(missing.tiles[3], 1)


def test_the_upload_skip_predicate_is_fail_closed():
    """A skip needs key + layer + content size + allocator to all still match.

    A wrong cell (a tile drawn from another frame's layer) is the worst failure
    this path can ship; a redundant upload is only a cost. So every uncertainty
    is an upload.
    """
    from tabs.image_compare.services.video_snapshot_rendering.strip_pass import (
        UploadedSides,
    )

    uploaded = UploadedSides()
    service = SimpleNamespace()
    key = ("strip-tile", 7, 3, 160, 90, 1)

    # An unknown key is never skipped.
    assert uploaded.needs_upload(key, 4, (160, 90), service) is True

    uploaded.mark_uploaded(key, 4, (160, 90), service)
    assert uploaded.needs_upload(key, 4, (160, 90), service) is False
    # The assigned slot changed: the pixels are somewhere else now.
    assert uploaded.needs_upload(key, 5, (160, 90), service) is True
    # The content size changed under the same key (a different upload shape).
    assert uploaded.needs_upload(key, 4, (224, 126), service) is True
    # A different allocator numbers its layers from zero again.
    assert uploaded.needs_upload(key, 4, (160, 90), SimpleNamespace()) is True
    # Another key now owns that layer: it holds THAT key's pixels.
    borrowed = ("strip-tile", 7, 9, 160, 90, 1)
    uploaded.mark_uploaded(borrowed, 4, (160, 90), service)
    assert uploaded.needs_upload(key, 4, (160, 90), service) is True
    # A cleared map (array rebuilt / released / content changed) uploads all.
    uploaded.clear()
    assert uploaded.needs_upload(borrowed, 4, (160, 90), service) is True


def test_the_band_host_owns_one_allocator_and_drops_it_with_the_content():
    """The host's half of the interface: ONE service per band, dropped on change."""
    from tabs.image_compare.plugins.video_editor.presenter_parts.strip_band import (
        STRIP_ARRAY_CAPACITY,
        StripBandHost,
    )
    from tabs.image_compare.plugins.video_editor.services.strip_recording import (
        MAX_BLOCKS,
    )

    assert STRIP_ARRAY_CAPACITY == 2 * MAX_BLOCKS
    host = StripBandHost(view=None, editor_service=SimpleNamespace())
    service = host.array_service()
    assert host.array_service() is service, "one allocator for the band's life"

    # A cut/undo changes the pixels behind the same tile keys: the allocator and
    # the pass's upload bookkeeping are dropped so nothing is painted from the
    # pre-cut frames.
    host.on_recording_changed()
    assert host.array_service() is not service
    host.detach()
    assert host._service is None
